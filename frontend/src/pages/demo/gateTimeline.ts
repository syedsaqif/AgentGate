import { isReceipt } from "../../api/approvalWire";
import { isRecord } from "../../api/shellWire";
import {
  bandValue, decisionValue, instant, integer, numberValue, readEvidence, reasonCode, textValue,
  type AuditEntry, type Band, type Decision, type GateEvent, type GateNotice, type GateStep, type RequestEvidence,
} from "./gateWire";

export const pipeline = [
  ["edge", "Intake"], ["identity", "Identity"], ["registry", "Agent"], ["canonical", "Request"],
  ["mandate", "Mandate"], ["policy", "Policy"], ["risk", "Risk"], ["breaker", "Breaker"],
  ["decision", "Decision"], ["execution", "Bank"],
] as const;
export type Lane = (typeof pipeline)[number][0] | "approval";

export type RequestTimeline = { requestId: string; steps: GateStep[]; notices: GateNotice[] };
export type GateFeed = { requests: Map<string, RequestTimeline>; cursor?: string; historyGap: boolean };
export const emptyFeed: GateFeed = { requests: new Map(), historyGap: false };
export type FeedAction = { type: "frame"; cursor?: string; event?: GateEvent } | { type: "resync" } | { type: "clear" };

export function gateFeedReducer(state: GateFeed, action: FeedAction): GateFeed {
  if (action.type === "clear") return emptyFeed;
  if (action.type === "resync") return { ...state, cursor: undefined, historyGap: true };
  const { event, cursor } = action;
  // Global SSE IDs are replay bookmarks. Only gate.step seq orders a request's checks.
  const nextCursor = cursor && /^\d{1,20}$/.test(cursor) && (!state.cursor || BigInt(cursor) > BigInt(state.cursor))
    ? cursor : state.cursor;
  if (!event) return nextCursor === state.cursor ? state : { ...state, cursor: nextCursor };

  const request = state.requests.get(event.requestId) ?? { requestId: event.requestId, steps: [], notices: [] };
  const next = nextCursor === state.cursor ? state : { ...state, cursor: nextCursor };
  if (event.type === "gate.step") {
    if (request.steps.some((step) => step.seq === event.seq || step.id === event.id)) return next;
    const requests = new Map(state.requests).set(event.requestId, {
      ...request, steps: [...request.steps, event].sort((left, right) => left.seq - right.seq),
    });
    return { ...next, requests };
  } else {
    if (request.notices.some((notice) => notice.type === event.type && notice.id === event.id)) return next;
    const requests = new Map(state.requests).set(event.requestId, {
      ...request, notices: [...request.notices, event],
    });
    return { ...next, requests };
  }
}

type Moment = "policy-fail" | "suspension" | "waiting" | "approval" | "bank-result";
type Beat = { key: string; lane: Lane; steps: GateStep[]; notices: GateNotice[]; moment?: Moment };
type Signal = { name: string; severity: number; weight: number };
type Risk = { score: number; band?: Band; signals: Signal[]; noisyOr: boolean };
type Outcome = "pending" | "executed" | "denied" | "rejected" | "failed" | "unknown";
export type GateView = {
  active?: Beat;
  summary: RequestEvidence["summary"];
  steps: GateStep[];
  lanes: Partial<Record<Lane, "reached" | "failed" | "skipped">>;
  failed?: GateStep;
  policyFailed: boolean;
  suspended: boolean;
  risk?: Risk;
  decision?: Decision;
  reason?: string;
  decisionMs?: number;
  auditCommitted: boolean;
  audit: AuditEntry[];
  approval?: { state: "waiting" | "approved" | "rejected" | "expired" | "cancelled"; expiresAt?: number; createdAt?: number };
  outcome?: Outcome;
  cached: boolean;
};

function stepMoment(step: GateStep): Moment | undefined {
  if (step.stage === "policy" && step.status === "fail" && step.detail.decisive === true) return "policy-fail";
  if (step.stage === "breaker" && step.check === "suspend_agent" && step.status === "info") return "suspension";
  if (step.check === "awaiting_approval") return "waiting";
  if (step.stage === "approval") return "approval";
  if (step.stage === "execution" && step.check === "outcome") return "bank-result";
}

function isApprovalResolution(step: GateStep) {
  return step.stage === "approval" && step.check === "customer_decision" && step.status === "pass";
}

export function buildPlayback(request: RequestTimeline): Beat[] {
  const beats: Beat[] = [];
  for (const step of request.steps) {
    // Skipped stages still render, but do not move the packet or occupy their own beat.
    if (step.status === "skip" && beats.length) {
      beats.at(-1)?.steps.push(step);
      continue;
    }
    const moment = stepMoment(step);
    const lane = step.stage === "idempotency" ? "canonical" : step.check === "awaiting_approval" ? "approval" : step.stage;
    const previous = beats.at(-1);
    // Continue the policy or breaker beat after its decisive check without replaying that stage.
    const sameStage = previous?.lane === lane && (!moment || previous.moment === moment);
    if (sameStage) previous.steps.push(step);
    else beats.push({ key: `step-${step.seq}`, lane, steps: [step], notices: [], moment });
  }

  for (const notice of request.notices) {
    // Request scope is required even for a lifecycle notification. Agent ID alone cannot correlate it.
    let target: Beat | undefined;
    let lane: Lane;
    let moment: Moment | undefined;
    switch (notice.type) {
      case "policy.decision": {
        lane = "decision";
        target = beats.find((beat) => beat.steps.some((step) => step.check === "final_decision"));
        if (!target && request.steps.length) continue;
        break;
      }
      case "action.denied":
        lane = "decision";
        target = beats.find((beat) => beat.steps.some((step) => step.check === "reject"));
        if (!target && request.steps.length) continue;
        break;
      case "agent.suspended":
        lane = "breaker";
        moment = "suspension";
        target = beats.find((beat) => beat.moment === moment);
        if (!target && request.steps.length && !beats.some((beat) => beat.lane === "decision")) continue;
        if (!target) {
          const decisionIndex = beats.findIndex((beat) => beat.lane === "decision");
          if (decisionIndex >= 0) {
            beats.splice(decisionIndex, 0, { key: `notice-${notice.id}`, lane, moment, steps: [], notices: [notice] });
            continue;
          }
        }
        break;
      case "escalation.created":
        lane = "approval";
        moment = "waiting";
        target = beats.find((beat) => beat.moment === moment);
        break;
      case "escalation.resolved":
        lane = "approval";
        moment = "approval";
        target = beats.find((beat) => beat.steps.some(isApprovalResolution));
        break;
      case "bank.executed":
      case "bank.failed":
      case "bank.unknown":
        lane = "execution";
        moment = "bank-result";
        target = beats.find((beat) => beat.moment === moment);
        break;
    }
    if (target) target.notices.push(notice);
    else beats.push({ key: `notice-${notice.id}`, lane, moment, steps: [], notices: [notice] });
  }
  return beats;
}

const bankOutcome = (value: unknown): Outcome | undefined => {
  if (value === "EXECUTED") return "executed";
  if (value === "EXECUTION_UNKNOWN" || value === "UNKNOWN") return "unknown";
  if (value === "FAILED" || value === "DECLINED") return "failed";
  if (value === "REJECTED") return "rejected";
};

export function projectGate(request: RequestTimeline, beats: Beat[], shown: number, evidence?: RequestEvidence): GateView {
  const visible = beats.slice(0, shown);
  const steps = visible.flatMap((beat) => beat.steps);
  const notices = visible.flatMap((beat) => beat.notices);
  const policyNotice = request.notices.find((notice) => notice.type === "policy.decision");
  const declaredRisk = policyNotice?.data.risk;
  const riskNotice = isRecord(declaredRisk) ? declaredRisk : undefined;
  const lanes: GateView["lanes"] = {};
  const audit = new Map<number, AuditEntry>();
  const view: GateView = {
    active: visible.at(-1), steps, lanes, policyFailed: false, suspended: false, audit: [],
    auditCommitted: false, cached: false, summary: {
      ...evidence?.summary,
      agentId: evidence?.summary.agentId ?? request.notices.find((notice) => notice.agentId)?.agentId,
    },
  };
  const earliest = steps.find((step) => step.stage === "edge" && step.seq === 1)?.at;
  const signalRows: Signal[] = [];
  view.summary.amount ??= request.steps.map((step) => integer(step.detail.requested_paise)).find((amount) => amount !== undefined);
  const summaryEvidence = request.notices.map((notice) => readEvidence(notice.data).summary);
  for (const summary of summaryEvidence) {
    for (const key of Object.keys(summary) as (keyof typeof summary)[]) {
      if (view.summary[key] === undefined && summary[key] !== undefined) Object.assign(view.summary, { [key]: summary[key] });
    }
  }

  function markApproval(state: NonNullable<GateView["approval"]>["state"]) {
    view.approval = { ...view.approval, state };
    if (state === "waiting") view.outcome ??= "pending";
    if (state === "approved" && view.outcome === "pending") view.outcome = undefined;
    if (state === "rejected" || state === "expired" || state === "cancelled") view.outcome = "rejected";
  }

  for (const beat of visible) {
    for (const step of beat.steps) {
      const lane = step.stage === "idempotency" ? "canonical" : step.check === "awaiting_approval" ? "approval" : step.stage;
      const { check, stage, detail, status } = step;
      if (status === "skip") {
        lanes[lane] ??= "skipped";
        continue;
      }
      if (lanes[lane] !== "failed") lanes[lane] = "reached";
      if (status === "fail" && (stage !== "policy" || detail.decisive === true)) {
        if (stage !== "approval") {
          lanes[lane] = "failed";
          view.failed ??= step;
        }
        const code = reasonCode(detail.reason_code);
        if (code) view.reason = code;
        if (stage === "approval" && code === "E_ESC_EXPIRED") markApproval("expired");
        if (stage === "approval" && code === "E_ESC_CANCELLED") markApproval("cancelled");
      }
      if (stage === "policy" && (detail.verdict === "DENY" || (status === "fail" && detail.decisive === true))) {
        view.policyFailed = true;
        lanes.policy = "failed";
      }
      if (stage === "risk") {
        const severity = numberValue(detail.severity, 100);
        const weight = numberValue(detail.weight, 1);
        if (severity !== undefined && weight !== undefined) signalRows.push({ name: check, severity, weight });
        const score = numberValue(detail.score, 100);
        if (check === "score" && score !== undefined) view.risk = {
          score, band: bandValue(detail.band), signals: [...signalRows],
          noisyOr: riskNotice?.model_version === "1.0.0-noisy_or",
        };
      }
      if (beat.moment === "suspension") view.suspended = true;
      if (check === "final_decision") {
        view.decision = decisionValue(detail.verdict);
        if (earliest !== undefined && step.at !== undefined && step.at >= earliest) view.decisionMs = step.at - earliest;
        if (view.decision === "DENY") view.outcome = "denied";
      }
      if (check === "write_ahead_audit" && status === "pass") view.auditCommitted = true;
      if (check === "awaiting_approval") markApproval("waiting");
      if (isApprovalResolution(step)) {
        if (detail.decision === "APPROVE") markApproval("approved");
        if (detail.decision === "REJECT") markApproval("rejected");
      }
      if (check === "stored_result") {
        view.cached = true;
        if (detail.outcome === "PENDING_APPROVAL") markApproval("waiting");
        else if (bankOutcome(detail.outcome)) view.outcome = bankOutcome(detail.outcome);
      }
      if (check === "outcome" && bankOutcome(detail.outcome)) view.outcome = bankOutcome(detail.outcome);
      if (check === "reject") view.reason = reasonCode(detail.reason_code) ?? view.reason;
    }

    for (const notice of beat.notices) {
      const { data, type } = notice;
      lanes[beat.lane] ??= "reached";
      if (type === "policy.decision") {
        view.decision ??= decisionValue(data.decision);
        if (Array.isArray(data.reason_codes)) view.reason ??= data.reason_codes.map(reasonCode).find(Boolean);
        if (view.decision === "DENY") view.outcome = "denied";
        view.auditCommitted = true;
        const score = numberValue(riskNotice?.score, 100);
        if (!view.risk && score !== undefined) {
          const signals = Array.isArray(riskNotice?.signals) ? riskNotice.signals.flatMap((signal: unknown) => {
            if (!isRecord(signal)) return [];
            const name = textValue(signal.name);
            const severity = numberValue(signal.severity, 100);
            const weight = numberValue(signal.weight, 1);
            return name && severity !== undefined && weight !== undefined ? [{ name, severity, weight }] : [];
          }) : [];
          view.risk = { score, signals, band: bandValue(riskNotice?.level), noisyOr: riskNotice?.model_version === "1.0.0-noisy_or" };
        }
      }
      if (type === "action.denied") {
        view.decision ??= "DENY";
        view.reason = reasonCode(data.reason_code) ?? view.reason;
        view.outcome = "denied";
      }
      if (type === "agent.suspended" && data.status === "SUSPENDED") view.suspended = true;
      if (type === "escalation.created") {
        markApproval("waiting");
        view.approval = { state: "waiting", expiresAt: instant(data.expires_at), createdAt: notice.occurredAt };
      }
      if (type === "escalation.resolved") {
        if (data.state === "APPROVED" || data.state === "EXECUTED" || data.decision === "APPROVE") markApproval("approved");
        if (data.state === "REJECTED" || data.decision === "REJECT") markApproval("rejected");
        if (data.state === "EXPIRED") markApproval("expired");
        if (data.state === "CANCELLED") markApproval("cancelled");
      }
      if (type === "bank.executed" && data.status === "EXECUTED") {
        if (view.approval?.state === "waiting") markApproval("approved");
        view.outcome = "executed";
      }
      if (type === "bank.failed") view.outcome = "failed";
      if (type === "bank.unknown") view.outcome = "unknown";
      if (isReceipt(data.audit_ref)) {
        audit.set(data.audit_ref.seq, { seq: data.audit_ref.seq, hash: data.audit_ref.entry_hash, label: type });
      }
    }
  }
  if (!view.outcome && view.failed && lanes.execution === "skipped") view.outcome = "rejected";

  const entries = [...(evidence?.audit ?? []), ...notices.flatMap((notice) => readEvidence(notice.data).audit)];
  for (const entry of entries) {
    if (audit.has(entry.seq)
      || (entry.label === "policy.decision" && view.auditCommitted)
      || (entry.label === "escalation.approved" && view.approval?.state === "approved")
      || (entry.label === "action.dispatched" && steps.some((step) => step.check === "reservation_and_dispatch_audit_committed"))
      || (["bank.executed", "dispute.receipt.v1"].includes(entry.label) && view.outcome === "executed")) {
      audit.set(entry.seq, entry);
    }
  }
  view.audit = [...audit.values()].sort((left, right) => left.seq - right.seq);
  return view;
}
