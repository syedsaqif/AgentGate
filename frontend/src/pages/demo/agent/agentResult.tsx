import { formatInrPaise } from "../../../formatting";
import type { Scenario, Step } from "./agentApi";

export function summarize(steps: Step[]) {
  const end = steps.filter((step) => step.name === "completed").at(-1);
  const result = steps.filter((step) => step.name === "tool_result").at(-1);
  const before = steps.find((step) => step.name === "refusal" || step.name === "clarification");
  const reason = result?.reason ?? end?.reason;
  const verdict = end?.verdict;
  const stop = before ? "Agent review before dispatch" : result?.cached ? "Idempotency cache hit"
    : result?.status === "EXECUTED" ? "Bank execution"
      : result?.status === "PENDING_APPROVAL" ? "Customer approval"
        : reason === "E_POLICY_PAYEE_NOT_ALLOWED" ? "Policy payee allow-list"
          : reason === "E_AUTH_AGENT_SUSPENDED" ? "Agent registry"
            : reason === "E_MANDATE_REVOKED" ? "Mandate verification"
              : reason === "E_RISK_BREAKER_OPEN" ? "Risk circuit breaker" : undefined;
  return { end, result, before, reason, verdict, stop };
}

export function Comparison({ card, steps }: { card: Scenario; steps: Step[] }) {
  const { end, verdict, reason } = summarize(steps);
  if (!end || !verdict || verdict === "ERROR") return null;
  const mismatch = verdict !== card.expected_verdict
    || !!(card.expected_reason_code && reason && reason !== card.expected_reason_code);
  return <p className="agent-comparison" data-match={!mismatch}>
    <strong>{mismatch ? "Expected vs actual · differs" : "Expected vs actual · decision matched"}</strong>
    <br />Expected {card.expected_verdict}{card.expected_reason_code && ` (${card.expected_reason_code})`}.
    Observed {verdict}{reason && ` (${reason})`}.
  </p>;
}

const label = {
  thinking: "Reading your request", reading_document: "Reviewed the attachment", plan: "Prepared payment details",
  tool_call: "Sent to AgentGate", tool_result: "Gateway response", error: "Could not confirm the result",
} as Record<string, string>;

export function AgentResult({ steps, card, stage }: { steps: Step[]; card?: Scenario; stage?: "live" | "recorded" }) {
  const { end, result, before, reason, verdict, stop } = summarize(steps);
  const ids = [...new Set(steps.flatMap(({ requestId }) => requestId ? [requestId] : []))];
  const reasonText = reason === "E_POLICY_PAYEE_NOT_ALLOWED" ? "The mandate does not include this payee."
    : reason === "E_AUTH_AGENT_SUSPENDED" ? "The agent is suspended."
      : reason === "E_MANDATE_REVOKED" ? "The mandate was revoked." : undefined;
  const resultTitle = before ? before.name === "refusal" ? "Refused before dispatch" : "Clarification before dispatch"
    : result?.cached ? "Original payment confirmed" : result?.status === "EXECUTED" ? "Paid"
      : result?.status === "PENDING_APPROVAL" ? "Waiting for your approval"
        : verdict === "DENY" ? "Payment blocked" : "Outcome unconfirmed";
  return <>
    <ol className="agent-steps" aria-label="Scripted agent steps">
      {steps.filter((step) => label[step.name]).map((step) => (
        <li key={step.seq}>
          <strong>{label[step.name]}{step.attempt && ` · Attempt ${step.attempt}`}</strong>
          {step.name === "tool_call" && stage && <details><summary>{step.tool} arguments</summary>
            <pre className="stage-args">{JSON.stringify(step.args, null, 2)}</pre></details>}
          {step.name === "tool_call" && !stage && <div className="agent-tool"><code>{step.tool}</code>
            <pre>{JSON.stringify(step.args, null, 2)}</pre></div>}
          {step.name === "tool_result" && (step.status || step.verdict) && <span> {step.status ?? step.verdict}</span>}
        </li>
      ))}
    </ol>
    {end && <section className="agent-result" aria-label="Run result">
      <p className="agent-tag">{before ? "No gateway call" : ids.length ? (stage === "recorded" ? "Recorded result" : "Live result") : "Unconfirmed result"}</p>
      <h2>{resultTitle}</h2>
      {before?.message && <p>{before.message}</p>}
      {reasonText && <p>{reasonText}</p>}
      <dl>
        {verdict && verdict !== "ERROR" && <><dt>Observed decision</dt><dd>{verdict}</dd></>}
        {stop && <><dt>Observed stop point</dt><dd>{stop}</dd></>}
        {reason && <><dt>Reason code</dt><dd><code>{reason}</code></dd></>}
        {end.agentStatus && <><dt>Breaker / agent status</dt><dd>{end.suspended ? "Agent suspended during run" : end.agentStatus}</dd></>}
        {(end.delta !== undefined || before) && <><dt>{result?.status === "PENDING_APPROVAL" ? "Account change at handoff" : "Observed account change"}</dt><dd>
          {end.delta ? `${end.delta < 0 ? "−" : "+"}${formatInrPaise(Math.abs(end.delta))}` : "₹0"}
        </dd></>}
      </dl>
      {!stage && result?.status === "PENDING_APPROVAL" && <a href="/demo/bank">Open Demo Bank</a>}
      {!stage && ids.map((id) => <p key={id}><a href={`/demo/gate?request_id=${encodeURIComponent(id)}`}>View Gate run · {id}</a></p>)}
      {card && <Comparison card={card} steps={steps} />}
    </section>}
  </>;
}
