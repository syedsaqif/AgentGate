import { describe, expect, it } from "vitest";
import { recordings } from "../src/pages/demo/gateRecordings";
import { buildPlayback, emptyFeed, gateFeedReducer, projectGate, type RequestTimeline } from "../src/pages/demo/gateTimeline";
import { decodeGateFrame, type GateEvent } from "../src/pages/demo/gateWire";
import approvalCapture from "../src/pages/demo/traces/approval.json";

function replay(name: keyof typeof recordings) {
  const { timeline, evidence } = recordings[name];
  const beats = buildPlayback(timeline);
  const at = (index: number) => projectGate(timeline, beats, index + 1, evidence);
  return { beats, at, result: at(beats.length - 1) };
}

describe("payment gate request timeline", () => {
  it("plays the recorded ALLOW and distinguishes decision from the committed bank result", () => {
    const { beats, at, result } = replay("allow");
    const decision = beats.findIndex((beat) => beat.lane === "decision");
    const bankResponse = beats.findIndex((beat) => beat.steps.some((step) => step.check === "bank_response"));
    expect(decision).toBeLessThan(bankResponse);
    expect(at(decision)).toMatchObject({ decision: "ALLOW", risk: { score: 0, band: "LOW", signals: [] } });
    expect(at(decision).decisionMs).toBeGreaterThan(0);
    expect(at(bankResponse).outcome).toBeUndefined();
    expect(result).toMatchObject({ outcome: "executed", summary: { amount: 185000, payee: "BILL-ELEC" } });
    expect(result.audit.some((entry) => entry.label === "bank.executed" && entry.hash.length === 64)).toBe(true);
    expect(result.audit.slice(1).every((entry, i) => entry.previous === result.audit[i].hash)).toBe(true);
    expect(beats.length).toBeLessThan(recordings.allow.timeline.steps.length / 2);
  });

  it("parks at the supplied escalation expiry, then resolves approval before execution", () => {
    const { beats, at, result } = replay("approval");
    const waiting = beats.findIndex((beat) => beat.moment === "waiting");
    const approved = beats.findIndex((beat) => beat.moment === "approval");
    const expiresAt = approvalCapture.events.find((event) => event.event_type === "escalation.created")?.data.expires_at;
    expect(waiting).toBeLessThan(approved);
    expect(at(waiting)).toMatchObject({
      decision: "ESCALATE", outcome: "pending", approval: { state: "waiting", expiresAt: Date.parse(expiresAt!) },
    });
    expect(at(approved).approval?.state).toBe("approved");
    expect(at(approved).outcome).toBeUndefined();
    expect(result).toMatchObject({ decision: "ESCALATE", outcome: "executed", approval: { state: "approved" } });
  });

  it("plays the decisive payee failure, scoring and suspension in that order", () => {
    const { beats, at, result } = replay("breaker");
    const failed = beats.findIndex((beat) => beat.moment === "policy-fail");
    const risk = beats.findIndex((beat) => beat.lane === "risk");
    const suspension = beats.findIndex((beat) => beat.moment === "suspension");
    expect(failed).toBeGreaterThan(-1);
    expect(failed).toBeLessThan(risk);
    expect(risk).toBeLessThan(suspension);
    expect(at(failed)).toMatchObject({ failed: { check: "payee_allowlist" }, policyFailed: true, suspended: false });
    expect(at(failed).risk).toBeUndefined();
    expect(at(failed).decision).toBeUndefined();
    expect(at(risk)).toMatchObject({ risk: { score: 78, band: "HIGH" }, suspended: false });
    expect(at(suspension).suspended).toBe(true);
    expect(at(suspension).decision).toBeUndefined();
    expect(result).toMatchObject({
      decision: "DENY", reason: "E_POLICY_PAYEE_NOT_ALLOWED", outcome: "denied", lanes: { execution: "skipped" },
    });
  });

  it("uses the labeled identity fixture without manufacturing downstream evidence", () => {
    const { result } = replay("identity");
    expect(recordings.identity.source).toBe("Identity test fixture");
    expect(result).toMatchObject({
      failed: { stage: "identity", check: "authenticate_agent" }, reason: "E_AUTH_PROOF_INVALID",
      outcome: "rejected", audit: [], lanes: { risk: "skipped", decision: "skipped", execution: "skipped" },
    });
    expect(result.risk).toBeUndefined();
    expect(result.decision).toBeUndefined();
    expect(result.decisionMs).toBeUndefined();
  });

  it("deduplicates replay and keeps interleaved requests separate across a resync", () => {
    const first = recordings.allow.timeline.steps[0];
    const second = recordings.allow.timeline.steps[1];
    const feed = gateFeedReducer(emptyFeed, { type: "frame", event: second, cursor: "7" });
    const other = { ...first, id: "8", requestId: "other-request" };
    const after = [
      { event: other, cursor: "8" },
      { event: first, cursor: "6" },
      { event: { ...second, id: "9" }, cursor: "9" },
    ].reduce((state, frame) => gateFeedReducer(state, { type: "frame", ...frame }), feed);
    expect(after.cursor).toBe("9");
    expect(after.requests.get(first.requestId)?.steps).toEqual([first, second]);
    expect(after.requests.get(other.requestId)?.steps).toEqual([other]);
    const resynced = gateFeedReducer(after, { type: "resync" });
    expect(resynced).toMatchObject({ cursor: undefined, historyGap: true });
    expect(resynced.requests).toBe(after.requests);
    const replayed = gateFeedReducer(resynced, { type: "frame", event: other, cursor: "8" });
    expect(replayed.cursor).toBe("8");
    expect(replayed.requests).toBe(resynced.requests);
  });

  it("accepts the redacted relay envelope and rejects uncorrelated suspensions", () => {
    const { id, requestId, seq, stage, check, status, detail } = recordings.allow.timeline.steps[0];
    expect(decodeGateFrame({ event: "gate.step", id, data: JSON.stringify({
      event_type: "gate.step", request_id: requestId, data: { seq, stage, check, status, detail },
    }) })).toMatchObject({ type: "gate.step", requestId, seq });
    expect(decodeGateFrame({ event: "agent.suspended", id: "40", data: JSON.stringify({
      event_id: "40", event_type: "agent.suspended", request_id: null, agent_id: "agt_billpal",
      data: { status: "SUSPENDED" },
    }) })).toBeUndefined();
  });

  it("does not treat a bank response as a debit, or invent approval expiry after a gap", () => {
    const request: RequestTimeline = { requestId: "partial", steps: [], notices: [] };
    const pending: GateEvent = {
      type: "escalation.created", requestId: "partial", id: "50", data: { status: "PENDING_APPROVAL" },
    };
    const resolved: GateEvent = {
      type: "escalation.resolved", requestId: "partial", id: "51", data: { state: "EXPIRED" },
    };
    const timeline = { ...request, notices: [pending, resolved] };
    const beats = buildPlayback(timeline);
    expect(projectGate(timeline, beats, 1).approval).toMatchObject({ state: "waiting", expiresAt: undefined });
    expect(projectGate(timeline, beats, 2)).toMatchObject({ outcome: "rejected", approval: { state: "expired" }, audit: [] });
  });
});
