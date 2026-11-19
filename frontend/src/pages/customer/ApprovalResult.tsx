import { useState } from "react";
import type { AuditReceipt, DecisionObservation } from "../../api/approvalWire";

export type ObservedDecision = {
  id: string;
  requestId: string;
  observation?: DecisionObservation;
  conflictCode?: string;
};

export function ApprovalResult({ response }: { response: ObservedDecision }) {
  const { observation, conflictCode } = response;
  const state = observation?.state;
  const result = observation?.result;
  const confirmed = !observation?.processing && result?.status === "EXECUTED"
    && (state === undefined || state === "APPROVED" || state === "EXECUTED");
  const unresolved = !confirmed && (state === "PENDING" || state === "APPROVED" || state === "EXECUTED" || !state);
  const conflictState = conflictCode === "E_ESC_EXPIRED" ? "EXPIRED"
    : conflictCode === "E_ESC_CANCELLED" ? "CANCELLED" : undefined;
  const showState = state && (!observation?.processing || state === "APPROVED" || state === "PENDING");

  return (
    <li className="approval-result">
      <p>Request <code>{observation?.requestId ?? response.requestId}</code></p>
      {showState && <p>Reported approval state <strong>{state}</strong></p>}
      {conflictState && <p>Provider conflict <strong>{conflictState}</strong></p>}
      {observation?.processing ? (
        <p>Processing. Final execution and receipt are unconfirmed.</p>
      ) : (
        <>
          {result && <p>Execution outcome <strong>{result.status}</strong></p>}
          {result?.status === "EXECUTION_UNKNOWN" ? (
            <p>The execution outcome is unknown. A receipt is unconfirmed.</p>
          ) : confirmed ? (
            result.audit_ref ? <Receipt receipt={result.audit_ref} /> : <p>Execution confirmed. The receipt is unavailable.</p>
          ) : unresolved && (
            <p>{observation ? "Execution and receipt are unconfirmed." : "The final approval state and receipt are unconfirmed."}</p>
          )}
        </>
      )}
    </li>
  );
}

function Receipt({ receipt }: { receipt: AuditReceipt }) {
  const [feedback, setFeedback] = useState("");
  const text = `audit_ref.seq: ${receipt.seq}\naudit_ref.entry_hash: ${receipt.entry_hash}`;
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setFeedback("Receipt copied.");
    } catch {
      setFeedback("Copy failed. Select the receipt text to copy it manually.");
    }
  }

  return (
    <div className="approval-receipt">
      <h4>Execution receipt</h4>
      <pre>{text}</pre>
      <button type="button" className="secondary" onClick={() => void copy()}>Copy receipt</button>
      {feedback && <p role="status">{feedback}</p>}
    </div>
  );
}
