import { useEffect, useMemo, useState, type CSSProperties } from "react";
import { formatInrPaise, formatKolkataTimestamp } from "../../formatting";
import { explainCheck, explainReason, gateLabel } from "./gateLabels";
import { recordings, type RecordingName } from "./gateRecordings";
import { buildPlayback, pipeline, projectGate, type GateView, type RequestTimeline } from "./gateTimeline";
import type { RequestEvidence } from "./gateWire";
import { useGateStream } from "./useGateStream";
import "./demoGate.css";

const speeds = [0.5, 1, 2] as const;
type Speed = (typeof speeds)[number];
const bandMarks = { balanced: [35, 70], conservative: [25, 60], permissive: [45, 80] };
const statusText = { reached: "Checked", failed: "Failed", skipped: "Not reached" };

function useReducedMotion() {
  const query = "(prefers-reduced-motion: reduce)";
  const [reduced, setReduced] = useState(() => window.matchMedia?.(query).matches ?? false);
  useEffect(() => {
    const media = window.matchMedia?.(query);
    if (!media) return;
    const update = () => setReduced(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  return reduced;
}

function RequestCard({ summary, requestId }: { summary: GateView["summary"]; requestId?: string }) {
  const { agentName, agentId, fingerprint, action, payee, amount, category, idempotencyKey } = summary;
  const fields = [
    ["Agent", agentName ?? agentId], ["Payee", payee], ["Category", category],
    ["Request ID", requestId], ["Key fingerprint", fingerprint], ["Retry key", idempotencyKey],
  ];
  return (
    <section className="gate-panel gate-request" aria-label="Request summary">
      <div className="gate-request-title">
        <h2>{action ? gateLabel(action) : "Payment request"}</h2>
        <strong>{amount === undefined ? "—" : formatInrPaise(amount)}</strong>
      </div>
      <dl className="gate-fields">
        {fields.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd title={value}>{value ?? "—"}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function RiskPanel({ risk, profile, skipped }: { risk: GateView["risk"]; profile: GateView["summary"]["profile"]; skipped: boolean }) {
  const markers = profile ? bandMarks[profile] : undefined;
  return (
    <section className="gate-panel gate-risk" aria-label="Risk and fired signals">
      <h2>Risk score</h2>
      <div className="gate-score">
        <strong>{risk?.score ?? "—"}</strong><span> / 100</span>
        {risk?.band && <span className="gate-band" data-band={risk.band}>{gateLabel(risk.band.toLowerCase())}</span>}
      </div>
      <div className="gate-meter" aria-hidden="true" data-bands={!!markers}
        style={markers && { "--gate-low": `${markers[0]}%`, "--gate-high": `${markers[1]}%` } as CSSProperties}>
        {risk && <i className="gate-needle" style={{ left: `${risk.score}%` }} />}
        {markers?.map((mark) => <b key={mark} style={{ left: `${mark}%` }} />)}
      </div>
      <div className="gate-scale" aria-hidden="true">
        <span>0</span>
        {markers?.map((mark) => <span key={mark} style={{ left: `${mark}%` }}>{mark}</span>)}
        <span>100</span>
      </div>
      <h3>Fired signals</h3>
      {!risk && <p className="gate-muted">{skipped ? "Risk scoring was skipped." : "Waiting for risk evidence."}</p>}
      {risk?.signals.length === 0 && <p className="gate-muted">No fired signals were reported.</p>}
      {!!risk?.signals.length && (
        <ul className="gate-signals">
          {risk.signals.map(({ name, severity, weight }, index) => (
            <li key={`${name}-${index}`}>
              <div><strong>{gateLabel(name)}</strong><span>{severity} × {weight}</span></div>
              <div className="gate-signal-bar" aria-hidden="true">
                <i style={{ "--gate-contribution": `${severity * weight}%` } as CSSProperties} />
              </div>
            </li>
          ))}
        </ul>
      )}
      {risk?.noisyOr && !!risk.signals.length && (
        <p className="gate-formula" aria-label="The gateway combines signal severity and weight with noisy OR">
          P = 1 − ∏(1 − wᵢ × sᵢ / 100)
        </p>
      )}
    </section>
  );
}

function ApprovalBay({ approval, recorded }: { approval: GateView["approval"]; recorded: boolean }) {
  const [now, setNow] = useState(() => Date.now());
  const expiresAt = approval?.expiresAt;
  const ticking = !recorded && expiresAt !== undefined && approval?.state === "waiting";
  useEffect(() => {
    if (!ticking) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [ticking, expiresAt]);

  if (!approval) return null;
  const { state, createdAt } = approval;
  const left = expiresAt === undefined ? undefined : Math.max(0, Math.ceil((expiresAt - now) / 1_000));
  const ttl = expiresAt !== undefined && createdAt !== undefined && expiresAt > createdAt ? (expiresAt - createdAt) / 1_000 : undefined;
  const deadlinePassed = state === "waiting" && !recorded && left === 0;
  const labels = {
    waiting: "Waiting for approval", approved: "Customer approved", rejected: "Customer rejected",
    expired: "Approval expired", cancelled: "Approval cancelled",
  };
  return (
    <div className="gate-approval" data-state={deadlinePassed ? "expired" : state}>
      {state === "waiting" && !recorded && left !== undefined && left > 0 && (
        <span className="gate-countdown" style={{ "--gate-ttl": ttl ? `${Math.min(100, 100 * left / ttl)}%` : "0%" } as CSSProperties}>
          {Math.floor(left / 60)}:{String(left % 60).padStart(2, "0")}
        </span>
      )}
      <div>
        <strong aria-live="polite">{deadlinePassed ? "Approval deadline passed" : labels[state]}</strong>
        {state === "waiting" && (
          <p>
            {left === undefined ? "Expiry is unavailable in the stream."
              : recorded ? `Recorded deadline · ${formatKolkataTimestamp(new Date(expiresAt!).toISOString())}`
                : deadlinePassed ? "Waiting for gateway confirmation."
                  : "Time remaining on the real approval deadline."}
          </p>
        )}
      </div>
    </div>
  );
}

function GatePipeline({ view, recorded }: { view: GateView; recorded: boolean }) {
  const { active, lanes } = view;
  const checks = view.steps.filter((step) => step.status !== "skip" && step.stage !== "risk");
  const latest = active?.steps.filter((step) => step.status !== "skip").at(-1);
  const policyCheck = view.failed?.stage === "policy" ? view.failed : undefined;
  const laneIndex = active?.lane === "approval" ? pipeline.length - 1 : pipeline.findIndex(([lane]) => lane === active?.lane);
  const position = 100 * (laneIndex + 0.5) / pipeline.length;
  const activeTitle = active?.moment === "policy-fail" && policyCheck ? gateLabel(policyCheck.check)
    : active?.moment === "suspension" ? "Circuit breaker tripped"
      : active?.lane === "risk" ? "Risk assessment"
        : latest && active?.lane !== "approval" ? gateLabel(latest.check) : undefined;
  const showCurrent = view.policyFailed || view.suspended || view.failed || activeTitle || !active;
  return (
    <section className="gate-panel gate-pipeline" aria-label="Payment pipeline">
      <h2>Payment pipeline</h2>
      <div className="gate-map-scroll">
        <div className="gate-map">
          <ol className="gate-lanes" aria-label="Gateway stages">
            {pipeline.map(([lane, label]) => {
              const status = lanes[lane];
              const suspended = lane === "breaker" && view.suspended;
              const executed = lane === "execution" && view.outcome === "executed" && !view.cached;
              return (
                <li key={lane} data-active={active?.lane === lane} data-status={suspended ? "suspended" : status ?? "waiting"}>
                  <span className="gate-door" aria-hidden="true">{status === "failed" ? "×" : status === "reached" ? "✓" : "·"}</span>
                  <strong>{label}</strong>
                  <small>{suspended ? "Tripped" : executed ? "Executed" : status ? statusText[status] : "—"}</small>
                </li>
              );
            })}
          </ol>
          <ApprovalBay approval={view.approval} recorded={recorded} />
          <div
            className="gate-packet"
            data-visible={!!active}
            data-parked={active?.lane === "approval" && view.approval?.state === "waiting"}
            data-suspended={active?.moment === "suspension"}
            data-failed={active?.moment === "policy-fail" || (!!active && lanes[active.lane] === "failed")}
            style={{ "--gate-packet-left": `${position}%` } as CSSProperties}
            aria-hidden="true"
          ><span key={active?.key} /></div>
        </div>
      </div>
      {showCurrent && <div className="gate-current">
        <div aria-live="polite">
          {view.policyFailed && <span className="gate-flag gate-policy-flag">Policy failed</span>}
          {view.suspended && <span className="gate-flag gate-suspension-flag">Agent suspended</span>}
          {!view.policyFailed && view.failed && <span className="gate-flag gate-policy-flag">{gateLabel(view.failed.stage)} failed</span>}
        </div>
        {(view.failed || activeTitle) && (
          <p><strong>{view.failed ? gateLabel(view.failed.check) : activeTitle}</strong>
            {view.failed && <span> {explainCheck(view.failed, view.summary.payee)}</span>}
          </p>
        )}
        {!active && <p className="gate-muted">The packet appears when a request reaches the gateway.</p>}
      </div>}
      {!!checks.length && (
        <details className="gate-checks">
          <summary>Checks observed <span>{checks.length}</span></summary>
          <ul>
            {checks.map((step) => (
              <li key={step.seq} data-status={step.status}>
                <span>{gateLabel(step.check)}</span>
                <strong>{step.status === "step_up" ? "Approval needed" : gateLabel(step.status)}</strong>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

function AuditPanel({ view }: { view: GateView }) {
  return (
    <section className="gate-panel gate-audit" aria-label="Audit chain">
      <h2>Audit chain</h2>
      {view.audit.length === 0 && (
        <p className="gate-muted">{view.auditCommitted ? "Decision recorded. No hash was supplied."
          : view.outcome === "rejected" ? "No audit hash was supplied." : "Awaiting audit evidence."}</p>
      )}
      <ol className="gate-chain">
        {view.audit.map((entry, index) => (
          <li key={entry.seq} data-linked={entry.previous === view.audit[index - 1]?.hash && !!entry.previous}>
            <details>
              <summary><strong>#{entry.seq}</strong> {gateLabel(entry.label)} <code>{entry.hash.slice(0, 10)}…</code></summary>
              <dl>
                <dt>Entry hash</dt><dd><code>{entry.hash}</code></dd>
                {entry.previous && <><dt>Previous hash</dt><dd><code>{entry.previous}</code></dd></>}
              </dl>
            </details>
          </li>
        ))}
      </ol>
    </section>
  );
}

function ResultPanel({ view }: { view: GateView }) {
  const { decision, outcome, reason, approval, cached, summary } = view;
  const result = {
    pending: ["Pending approval", "₹0 moved"],
    executed: [cached ? "Stored bank result" : "Bank executed",
      cached ? "₹0 moved on this retry" : summary.amount === undefined ? "Money moved" : `${formatInrPaise(summary.amount)} paid`],
    denied: ["Request denied", "₹0 moved"],
    rejected: [approval ? "Approval closed" : "Request rejected", "₹0 moved"],
    failed: ["Bank execution failed", "Debit unconfirmed"],
    unknown: ["Bank outcome unknown", "Debit unconfirmed"],
  };
  return (
    <section className="gate-panel gate-result" data-outcome={outcome} aria-label="Decision and execution result" aria-live="polite">
      <h2>Execution result {decision && <span className="gate-decision" data-decision={decision}>{decision}</span>}</h2>
      {outcome ? (
        <>
          <p>{result[outcome][0]}</p>
          <strong>{result[outcome][1]}</strong>
          {cached && <p>Result from the original request.</p>}
        </>
      ) : (
        <p className="gate-muted">{approval?.state === "approved" || decision === "ALLOW"
          ? "Waiting for the bank result." : "Awaiting the gateway result."}</p>
      )}
      {reason && <p className="gate-reason"><code>{reason}</code>{!view.failed && explainReason(reason) && <span>{explainReason(reason)}</span>}</p>}
    </section>
  );
}

const emptyRequest: RequestTimeline = { requestId: "", steps: [], notices: [] };

export function Playback({ request = emptyRequest, evidence, recorded, controls }: {
  controls?: { speed: Speed; skip: boolean; position?: number };
  request?: RequestTimeline; evidence?: RequestEvidence; recorded: boolean;
}) {
  const [localSpeed, setSpeed] = useState<Speed>(1);
  const speed = controls?.speed ?? localSpeed;
  const [shown, setShown] = useState(0);
  const [localSkip, setSkipped] = useState(false);
  const skipped = controls?.skip ?? localSkip;
  const reduced = useReducedMotion();
  const beats = useMemo(() => buildPlayback(request), [request]);
  const view = projectGate(request, beats, skipped || reduced ? beats.length : controls?.position ?? shown, evidence);

  useEffect(() => {
    if (controls?.position !== undefined || reduced || skipped || shown >= beats.length) return;
    const prior = beats[shown - 1];
    const delay = shown === 0 ? (request.steps.length ? 100 : 450)
      : prior?.moment === "waiting" ? 1_450
        : prior?.moment ? 700
          : prior?.lane === "risk" ? 850
            : 330;
    const timer = window.setTimeout(() => setShown(shown + 1), delay / speed);
    return () => window.clearTimeout(timer);
  }, [controls?.position, beats, reduced, request.steps.length, shown, skipped, speed]);

  return (
    <>
      <RequestCard summary={view.summary} requestId={request.requestId || undefined} />
      <div className="gate-playback">
        <div className="gate-playback-label">
          <strong>Slow-motion playback</strong>
          {view.decisionMs !== undefined && (
            <span>Real decision · {view.decisionMs < 1 ? "<1" : view.decisionMs} ms</span>
          )}
        </div>
        {!controls && <div className="gate-controls">
          <div className="gate-speeds" role="group" aria-label="Playback speed">
            {speeds.map((value) => (
              <button key={value} type="button" aria-pressed={value === speed} onClick={() => setSpeed(value)}>
                {value}×
              </button>
            ))}
          </div>
          <button type="button" onClick={() => { setSkipped(false); setShown(0); }} disabled={!beats.length || reduced}>Replay</button>
          <button className="gate-skip" type="button" onClick={() => setSkipped(true)} disabled={!beats.length || skipped || reduced}>Skip to result</button>
        </div>}
      </div>
      <div className="gate-middle" style={{ "--gate-motion": `${230 / speed}ms` } as CSSProperties}>
        <GatePipeline view={view} recorded={recorded} />
        <RiskPanel risk={view.risk} profile={view.summary.profile} skipped={view.lanes.risk === "skipped"} />
      </div>
      <div className="gate-bottom">
        <AuditPanel view={view} />
        <ResultPanel view={view} />
      </div>
    </>
  );
}

type Source = "live" | RecordingName;
const connectionLabels = { connecting: "Connecting", live: "Live stream", reconnecting: "Reconnecting", "sign-in": "Presenter sign-in required" };

export default function DemoGatePage() {
  const [source, setSource] = useState<Source>("live");
  const [pinned, setPinned] = useState(() => new URLSearchParams(window.location.search).get("request_id") ?? "");
  const { feed, connection, reconnect } = useGateStream(source === "live");
  const recording = source === "live" ? undefined : recordings[source];
  const lastRequest = [...feed.requests.keys()].at(-1);
  const request = recording?.timeline ?? (source === "live" ? feed.requests.get(pinned || lastRequest || "") : undefined);

  return (
    <main className="gate-demo">
      <header className="gate-header">
        <div><p>AgentGate</p><h1>Payment gate</h1></div>
        <div className="gate-source">
          <span className="gate-source-status" data-connection={recording ? "recorded" : connection} role="status">
            {recording?.source ?? connectionLabels[connection]}
          </span>
          {source === "live" && connection === "sign-in" && <button type="button" onClick={reconnect}>Reconnect</button>}
          <label>
            <span>Trace source</span>
            <select value={source} onChange={(event) => setSource(event.target.value as Source)}>
              <option value="live">Live requests</option>
              {Object.entries(recordings).map(([name, entry]) => <option key={name} value={name}>{entry.title}</option>)}
            </select>
          </label>
          {source === "live" && (feed.requests.size > 1 || pinned) && (
            <label>
              <span>Request</span>
              <select value={pinned} onChange={(event) => setPinned(event.target.value)}>
                <option value="">Latest request</option>
                {pinned && !feed.requests.has(pinned) && <option value={pinned}>{pinned} (waiting)</option>}
                {[...feed.requests.keys()].reverse().map((id) => <option key={id} value={id}>{id}</option>)}
              </select>
            </label>
          )}
        </div>
      </header>
      {source === "live" && feed.historyGap && <p className="gate-gap" role="status">Some events expired from the replay buffer. This request history may be incomplete.</p>}
      <Playback key={`${source}-${request?.requestId ?? ""}`} request={request} recorded={!!recording} evidence={recording?.evidence} />
    </main>
  );
}
