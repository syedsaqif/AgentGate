import { useEffect, useRef, useState, type FormEvent } from "react";
import type { CustomerDecision } from "../../../api/approvalWire";
import { isRecord } from "../../../api/shellWire";
import { readAndClearPin } from "../../../components/pin";
import { QrButton } from "../../../components/QrDialog";
import { formatInrPaise, formatKolkataTimestamp } from "../../../formatting";
import { bankApi } from "./bankApi";
import { stringField, type BankApproval, type BankNotice, type Overview } from "./bankWire";
import type { Mutation } from "./useBankWorkspace";

export function hasBankConfirmedChange(previous: Overview, next: Overview) {
  const head = next.ledger[0];
  return previous.balance !== next.balance && head?.balance_after_paise === next.balance
    && !previous.ledger.some((entry) => entry.entry_id === head.entry_id);
}

function Balance({ overview }: { overview: Overview }) {
  const [display, setDisplay] = useState(overview.balance);
  const before = useRef(overview);
  const balance = overview.balance;
  const newestEntry = overview.ledger[0]?.entry_id;
  useEffect(() => {
    const previous = before.current;
    before.current = overview;
    if (!hasBankConfirmedChange(previous, overview) || window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
      setDisplay(overview.balance);
      return;
    }
    const start = performance.now();
    let frame = requestAnimationFrame(function tick(now) {
      const progress = Math.min(1, (now - start) / 480);
      const delta = overview.balance - previous.balance;
      setDisplay(Math.round(previous.balance + (1 - (1 - progress) ** 3) * delta));
      if (progress < 1) frame = requestAnimationFrame(tick);
    });
    return () => cancelAnimationFrame(frame);
    // Approval and notification refreshes can leave both bank observations unchanged.
  }, [balance, newestEntry]);

  return <strong className="bank-balance" aria-label={formatInrPaise(overview.balance)} data-balance-paise={overview.balance}>
    <span aria-hidden="true">{formatInrPaise(display)}</span>
  </strong>;
}

const labels: Record<BankApproval["state"], string> = {
  PENDING: "Pending", APPROVED: "Approved", EXECUTED: "Approved · paid", REJECTED: "Rejected",
  EXPIRED: "Expired", CANCELLED: "Cancelled", FAILED: "Execution failed",
};

export function approvalStatus(item: BankApproval, overview: Overview, now: number) {
  const summary: unknown = item.trusted_summary;
  const mandateId = item.mandate_id ?? (isRecord(summary) ? stringField(summary.mandate, "mandate_id") : undefined);
  if (["PENDING", "APPROVED", "CANCELLED"].includes(item.state) && overview.mandates.some(({ terms, status }) =>
    terms.mandate_id === mandateId && status === "REVOKED")) return "REVOKED";
  if (item.state === "PENDING" && Date.parse(item.expires_at) <= now) return "EXPIRED";
  return item.state;
}

function Approval({ item, now, overview, disabled, mutate, recorded }: {
  recorded?: boolean;
  item: BankApproval; now: number; overview: Overview; disabled: boolean; mutate: Mutation;
}) {
  const formRef = useRef<HTMLFormElement>(null);
  const { amount, payee, risk } = item.trusted_summary;
  const state = approvalStatus(item, overview, now);
  const left = Math.max(0, Math.ceil((Date.parse(item.expires_at) - now) / 1000));
  const payeeId = stringField(payee, "payee_id");
  const stateText = state === "REVOKED" ? "Cancelled · mandate revoked" : labels[state];

  function decide(choice: CustomerDecision) {
    if (state !== "PENDING" || Date.now() >= Date.parse(item.expires_at) || disabled) return;
    void mutate(async (signal) => {
      const result = await bankApi.decide(item.escalation_id, item.request_id, choice, signal);
      if (result.state === "REJECTED") return "Payment rejected.";
      if (result.result?.status === "EXECUTED") return "Payment confirmed by the bank.";
      if (result.state === "APPROVED" || result.processing) return "Approval received. Checking the bank for execution.";
    });
  }

  function approve(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const pin = readAndClearPin(event.currentTarget);
    if (pin.trim()) decide({ decision: "APPROVE", pin });
  }

  return (
    <article className="bank-card bank-approval-card" data-approval-id={item.escalation_id}>
      <div className="bank-section-title">
        <h3>{payee?.display_name ?? payeeId ?? "Payment request"}</h3>
        <span className="bank-tag" data-state={state}>{stateText}</span>
      </div>
      <div className="bank-section-title bank-approval-amount">
        <strong>{formatInrPaise(amount.amount_paise)}</strong>
        {state === "PENDING" && (
          <span className="bank-clock" aria-label={`${left} seconds remaining`}>{Math.floor(left / 60)}:{String(left % 60).padStart(2, "0")}</span>
        )}
      </div>
      {state === "APPROVED" && <p className="bank-muted">Waiting for bank confirmation.</p>}
      {state === "EXECUTED" ? (!recorded && <ReceiptQr requestId={item.request_id} />) : <>
        <QrButton disabled load={(signal) => bankApi.receiptQr(item.request_id, signal)} />
        {(state === "PENDING" || state === "REJECTED" || state === "REVOKED" || state === "EXPIRED")
          && <p className="bank-muted">No payment was made, so there is nothing to prove.</p>}
      </>}
      {(state === "PENDING" || state === "EXPIRED") && (
        <p className="bank-muted">Deadline <time dateTime={item.expires_at}>{formatKolkataTimestamp(item.expires_at)}</time></p>
      )}
      {state === "PENDING" && !recorded && <>
        <ul className="bank-reasons">
          {risk?.reasons.length ? risk.reasons.slice(0, 3).map((reason, i) => <li key={i}>{reason}</li>)
            : <li>This payment requires your approval.</li>}
        </ul>
        <form className="bank-form" ref={formRef} method="post" autoComplete="off" onSubmit={approve}>
          <fieldset disabled={disabled}>
            <label>Approval PIN<input name="pin" type="password" inputMode="numeric" minLength={4} maxLength={16} required /></label>
            <div className="bank-actions">
              <button type="submit">Approve</button>
              <button type="button" className="bank-secondary" onClick={() => {
                if (formRef.current) readAndClearPin(formRef.current);
                decide({ decision: "REJECT" });
              }}>Reject</button>
            </div>
          </fieldset>
        </form>
      </>}
    </article>
  );
}

function ReceiptQr({ requestId }: { requestId: string }) {
  return <QrButton load={(signal) => bankApi.receiptQr(requestId, signal)} />;
}

function noticeText(notice: BankNotice, overview: Overview, now: number) {
  const agent = overview.catalog.agents.find(({ agent_id }) => agent_id === notice.agentId)?.name;
  const payee = overview.catalog.payees.find(({ payee_id }) => payee_id === notice.payeeId)?.display_name ?? notice.payeeId;
  const item = overview.approvals.find(({ request_id }) => request_id === notice.requestId);
  switch (notice.type) {
    case "bank.executed": {
      const amount = notice.amount ?? item?.trusted_summary.amount.amount_paise;
      const recipient = payee ?? item?.trusted_summary.payee?.display_name;
      return amount === undefined ? "The bank confirmed a payment."
        : `${agent ?? "An agent"} paid ${formatInrPaise(amount)}${recipient ? ` to ${recipient}` : ""}.`;
    }
    case "escalation.created":
    case "escalation.resolved":
      return item && approvalStatus(item, overview, now) === "PENDING" ? "A payment is waiting for your approval." : undefined;
    case "action.denied":
      return agent ? `Blocked a payment attempt by ${agent}.` : "Blocked a payment attempt.";
    case "agent.suspended":
      return agent ? `${agent} was suspended.` : "An agent was suspended.";
    case "mandate.revoked":
      return overview.mandates.some(({ terms, status }) => terms.mandate_id === notice.mandateId && status === "REVOKED")
        ? "A mandate was revoked." : undefined;
  }
}

export function BankHome({ overview, notices, disabled, mutate, recordedAt }: {
  recordedAt?: number;
  overview: Overview; notices: BankNotice[]; disabled: boolean; mutate: Mutation;
}) {
  const [now, setNow] = useState(() => Date.now());
  const tick = recordedAt === undefined && overview.approvals.some((item) => item.state === "PENDING" && Date.parse(item.expires_at) > now);
  useEffect(() => {
    if (!tick) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [tick]);

  const snapshotNow = recordedAt ?? Math.max(now, Date.now());
  const ordered = [...overview.approvals].sort((a, b) => b.expires_at.localeCompare(a.expires_at));
  const pending = ordered.filter((item) => approvalStatus(item, overview, snapshotNow) === "PENDING");
  const closed = ordered.filter((item) => approvalStatus(item, overview, snapshotNow) !== "PENDING");
  const visibleNotices = notices.flatMap((notice) => {
    const message = noticeText(notice, overview, snapshotNow);
    return message ? [{ ...notice, message }] : [];
  });

  return (
    <div className="bank-stack">
      <section className="bank-account" aria-label="Account balance">
        <p>Available balance</p>
        <Balance overview={overview} />
        <p className="bank-account-id">{overview.accountId}</p>
      </section>
      {visibleNotices.length > 0 && (
        <section className="bank-card" aria-labelledby="bank-notifications">
          <h2 id="bank-notifications">Notifications</h2>
          <ul className="bank-notices" aria-live="polite">
            {visibleNotices.slice(0, 5).map((notice) =>
              <li key={`${notice.id}-${notice.at}`} data-kind={notice.type}>
                <p>{notice.message}</p>
                {recordedAt === undefined && notice.type === "bank.executed" && notice.requestId && <ReceiptQr requestId={notice.requestId} />}
                {notice.type === "action.denied" && <>
                  <QrButton disabled load={(signal) => bankApi.receiptQr(notice.requestId!, signal)} />
                  <p>No payment was made, so there is nothing to prove.</p>
                </>}
              </li>)}
          </ul>
        </section>
      )}
      <section className="bank-stack" aria-labelledby="bank-approvals">
        <div className="bank-section-title"><h2 id="bank-approvals">Approvals</h2>
          {pending.length > 0 && <span className="bank-tag" data-state="PENDING">{pending.length} pending</span>}
        </div>
        {pending.length === 0 && <p className="bank-card bank-muted">No payments waiting for approval.</p>}
        {pending.map((item) => <Approval recorded={recordedAt !== undefined} key={item.escalation_id} {...{ item, overview, now: snapshotNow, disabled, mutate }} />)}
        {closed.length > 0 && (
          <details className="bank-closed">
            <summary>Recent approval decisions</summary>
            {closed.slice(0, 8).map((item) => <Approval recorded={recordedAt !== undefined} key={item.escalation_id} {...{ item, overview, now: snapshotNow, disabled, mutate }} />)}
          </details>
        )}
      </section>
      {!!overview.receipts?.length && (
        <section className="bank-card" aria-labelledby="bank-payment-receipts">
          <h2 id="bank-payment-receipts">Payment receipts</h2>
          <ul className="bank-ledger">
            {overview.receipts.slice(0, 8).map((receipt) => (
              <li key={receipt.requestId}>
                <div><strong>{formatInrPaise(receipt.amount)}</strong><span>
                  {overview.catalog.payees.find((payee) => payee.payee_id === receipt.payeeId)?.display_name
                    ?? receipt.payeeId ?? "Payment"}</span><code>{receipt.requestId}</code></div>
                {recordedAt === undefined && <ReceiptQr requestId={receipt.requestId} />}
              </li>
            ))}
          </ul>
        </section>
      )}
      <section className="bank-card" aria-labelledby="bank-ledger">
        <h2 id="bank-ledger">Recent transactions</h2>
        {overview.ledger.length === 0 ? <p className="bank-muted">No transactions yet.</p> : (
          <ul className="bank-ledger">
            {overview.ledger.slice(0, 8).map((entry) => (
              <li key={entry.entry_id} data-ledger-entry={entry.entry_id} data-entry-type={entry.entry_type}>
                <div><strong>{entry.entry_type === "DEBIT" ? "Payment" : "Credit"}</strong>
                  {entry.reference && <span>{entry.reference}</span>}
                  <time dateTime={entry.created_at}>{formatKolkataTimestamp(entry.created_at)}</time>
                </div>
                <div><strong>{entry.entry_type === "DEBIT" ? "−" : "+"}{formatInrPaise(entry.amount_paise)}</strong>
                  <span>Balance {formatInrPaise(entry.balance_after_paise)}</span></div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
