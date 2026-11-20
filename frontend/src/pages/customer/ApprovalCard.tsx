import { useRef, type FormEvent } from "react";
import type { ApprovalItem, CustomerDecision } from "../../api/approvalWire";
import { formatInrPaise, formatKolkataTimestamp } from "../../formatting";

export function ApprovalCard({ item, disabled, submitted, decide }: {
  item: ApprovalItem; disabled: boolean; submitted: boolean;
  decide: (choice: CustomerDecision) => void;
}) {
  const { amount, from_account, payee, risk, agent_note } = item.trusted_summary;
  const formRef = useRef<HTMLFormElement>(null);

  function approve(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (disabled || submitted) return;
    const form = event.currentTarget;
    const pin = String(new FormData(form).get("pin") ?? "");
    form.reset();
    if (pin.trim()) decide({ decision: "APPROVE", pin });
  }

  function reject(suspend: boolean) {
    formRef.current?.reset();
    decide(suspend ? { decision: "REJECT", suspend_agent: true } : { decision: "REJECT" });
  }

  return (
    <article className="mandate-record approval-card">
      <h3>{payee?.display_name ?? "Payee unavailable"}</h3>
      <p>Request <code>{item.request_id}</code></p>
      <dl className="mandate-usage">
        <div><dt>Approval state</dt><dd>{item.state}</dd></div>
        <div><dt>Account</dt><dd>{from_account.masked_label.replaceAll(from_account.account_id, "••••")}</dd></div>
        <div><dt>Amount</dt><dd>{formatInrPaise(amount.amount_paise)}</dd></div>
        <div><dt>Deadline</dt><dd><time dateTime={item.expires_at}>{formatKolkataTimestamp(item.expires_at)}</time></dd></div>
      </dl>
      {risk && risk.reasons.length > 0 && (
        <div>
          <h4>Risk reasons</h4>
          <ul>{risk.reasons.slice(0, 3).map((reason, index) => <li key={index}>{reason}</li>)}</ul>
        </div>
      )}
      {agent_note?.text && <div className="approval-note">
        <h4>Unverified agent note</h4>
        <p>{agent_note.text}</p>
      </div>}
      {item.state === "PENDING" && (submitted ? <p>Check the last decision response below for the reported outcome.</p> : (
        <form ref={formRef} method="post" autoComplete="off" onSubmit={approve}>
          <fieldset disabled={disabled}>
            <label>Approval PIN<input name="pin" type="password" inputMode="numeric" required /></label>
            <div className="mandate-buttons">
              <button type="submit">Approve</button>
              <button type="button" className="secondary" onClick={() => reject(false)}>Reject</button>
              <button type="button" className="secondary" onClick={() => reject(true)}>Reject and suspend</button>
            </div>
            <p>Reject and suspend affects only your delegation for this agent.</p>
          </fieldset>
        </form>
      ))}
    </article>
  );
}
