import { useCallback, useEffect, useRef, useState } from "react";
import type { ApprovalItem, ApprovalPage, CustomerDecision } from "../../api/approvalWire";
import type { ShellApi } from "../../api/client";
import { ApiError } from "../../api/errors";
import { ErrorNotice } from "../../components/ErrorNotice";
import { ApprovalCard } from "./ApprovalCard";
import { ApprovalResult, type ObservedDecision } from "./ApprovalResult";

type ApprovalInboxProps = { api: ShellApi; token: string; endSession: (error: ApiError) => void };
type Decision = { item: ApprovalItem; choice: CustomerDecision };

export function ApprovalInbox({ api, token, endSession }: ApprovalInboxProps) {
  const [page, setPage] = useState<ApprovalPage | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [readFailure, setReadFailure] = useState<{ error: ApiError; cursor: string | null }>();
  const [decisionFailure, setDecisionFailure] = useState<ApiError>();
  const [observed, setObserved] = useState<ObservedDecision[]>([]);
  const active = useRef<AbortController | null>(null);

  const run = useCallback(async (requestedCursor: string | null, decision?: Decision) => {
    if (active.current) return;
    const controller = new AbortController();
    const { signal } = controller;
    active.current = controller;
    setBusy(true);

    function asApiError(error: unknown) {
      return error instanceof ApiError ? error
        : new ApiError("The approval request could not be completed.", "UI_REQUEST_FAILED");
    }

    try {
      if (decision) {
        setDecisionFailure(undefined);
        const { item, choice } = decision;
        const response = { id: item.escalation_id, requestId: item.request_id };
        try {
          const observation = await api.decideApproval(token, item.escalation_id, choice, signal);
          if (signal.aborted) return;
          setObserved((previous) => [{ ...response, observation }, ...previous]);
        } catch (failure) {
          if (signal.aborted) return;
          const error = asApiError(failure);
          if (error.status === 401) return endSession(error);
          setDecisionFailure(error);
          // Conflicts and ambiguous failures get a read-only refresh. The POST
          // may have been consumed even if its outcome is unavailable.
          if (error.status !== 409 && error.status !== undefined && error.status < 500) return;
          setObserved((previous) => [{ ...response, conflictCode: error.status === 409 ? error.code : undefined }, ...previous]);
        }
      }
      try {
        const next = await api.pendingApprovals(token, requestedCursor, signal);
        if (signal.aborted) return;
        setPage(next);
        setCursor(requestedCursor);
        setReadFailure(undefined);
      } catch (failure) {
        if (signal.aborted) return;
        const error = asApiError(failure);
        if (error.status === 401) return endSession(error);
        setReadFailure({ error, cursor: requestedCursor });
      }
    } finally {
      if (active.current === controller) {
        active.current = null;
        setBusy(false);
      }
    }
  }, [api, token, endSession]);

  useEffect(() => {
    let cancelled = false;
    queueMicrotask(() => { if (!cancelled) void run(null); });
    return () => {
      cancelled = true;
      active.current?.abort();
      active.current = null;
    };
  }, [run]);

  return (
    <section className="panel approval-inbox" aria-labelledby="approval-inbox-title" aria-busy={busy}>
      <h2 id="approval-inbox-title">Approval inbox</h2>
      {decisionFailure && <ErrorNotice error={decisionFailure} />}
      {readFailure && (
        <>
          <ErrorNotice error={readFailure.error} />
          {page && <p>The displayed inbox needs refreshing.</p>}
        </>
      )}
      {busy && <p role="status">Loading approvals…</p>}
      {!busy && !readFailure && page?.items.length === 0 && <p>No pending approvals on this page.</p>}
      {page?.items.map((item) => (
        <ApprovalCard key={item.escalation_id} item={item} disabled={busy || Boolean(readFailure)}
          submitted={observed.some((record) => record.id === item.escalation_id)}
          decide={(choice) => void run(null, { item, choice })} />
      ))}
      <div className="mandate-buttons">
        <button type="button" className="secondary" disabled={busy} onClick={() => void run(readFailure ? readFailure.cursor : cursor)}>
          {readFailure ? "Retry inbox refresh" : "Refresh inbox"}
        </button>
        {cursor !== null && <button type="button" className="secondary" disabled={busy || Boolean(readFailure)}
          onClick={() => void run(null)}>First approval page</button>}
        {page && page.next_cursor !== null && <button type="button" className="secondary" disabled={busy || Boolean(readFailure)}
          onClick={() => void run(page.next_cursor)}>Next approval page</button>}
      </div>
      {observed.length > 0 && (
        <section className="approval-results" aria-labelledby="approval-results-title" aria-live="polite">
          <h3 id="approval-results-title">Last decision responses</h3>
          <p>The pending inbox only shows which requests are awaiting a decision. It cannot confirm a final result.</p>
          <ul>{observed.map((response) => <ApprovalResult key={response.id} response={response} />)}</ul>
        </section>
      )}
    </section>
  );
}
