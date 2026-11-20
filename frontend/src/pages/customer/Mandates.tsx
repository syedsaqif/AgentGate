import { useCallback, useEffect, useRef, useState } from "react";
import type { ShellApi } from "../../api/client";
import { ApiError } from "../../api/errors";
import type { GrantUsage, MandateListItem, MandatePage } from "../../api/mandateWire";
import type { CustomerBootstrap } from "../../api/shellWire";
import { ErrorNotice } from "../../components/ErrorNotice";
import { formatInrPaise, formatKolkataTimestamp } from "../../formatting";
import { MandateForm } from "./MandateForm";
import { actionLabels, maskedAccount } from "./mandateDraft";

type MandatesProps = {
  api: ShellApi;
  bootstrap: CustomerBootstrap;
  token: string;
  endSession: (error?: ApiError) => void;
};

type Mutation = { perform: (signal: AbortSignal) => Promise<void>; message: string };

export function Mandates({ api, bootstrap, token, endSession }: MandatesProps) {
  const [page, setPage] = useState<MandatePage | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [readFailure, setReadFailure] = useState<{ error: ApiError; cursor: string | null }>();
  const [mutationFailure, setMutationFailure] = useState<ApiError>();
  const [notice, setNotice] = useState("");
  const active = useRef<AbortController | null>(null);

  const run = useCallback(async (requestedCursor: string | null, mutation?: Mutation): Promise<boolean> => {
    if (active.current) return false;
    const controller = new AbortController();
    const { signal } = controller;
    active.current = controller;
    setBusy(true);

    function showFailure(failure: unknown, onError: (error: ApiError) => void) {
      if (signal.aborted) return;
      const error = failure instanceof ApiError
        ? failure
        : new ApiError("The mandate request could not be completed.", "UI_REQUEST_FAILED");
      if (error.status === 401) endSession(error);
      else onError(error);
    }

    try {
      if (mutation) {
        setMutationFailure(undefined);
        setNotice("");
        try {
          await mutation.perform(signal);
        } catch (failure) {
          showFailure(failure, setMutationFailure);
          return false;
        }
        if (signal.aborted) return false;
        setNotice(mutation.message);
      }
      try {
        const result = await api.customerMandates(token, requestedCursor, signal);
        if (signal.aborted) return false;
        setPage(result);
        setCursor(requestedCursor);
        setReadFailure(undefined);
      } catch (failure) {
        showFailure(failure, (error) => setReadFailure({ error, cursor: requestedCursor }));
      }
      return mutation !== undefined && !signal.aborted;
    } finally {
      if (active.current === controller) {
        active.current = null;
        setBusy(false);
      }
    }
  }, [api, token, endSession]);

  useEffect(() => {
    let cancelled = false;
    queueMicrotask(() => {
      if (!cancelled) void run(null);
    });
    return () => {
      cancelled = true;
      active.current?.abort();
      active.current = null;
    };
  }, [run]);

  const actionsDisabled = busy || Boolean(readFailure);
  return (
    <div className="mandate-flow">
      <MandateForm bootstrap={bootstrap} disabled={actionsDisabled} create={(terms, pin) => run(null, {
        perform: (signal) => api.createMandate(token, { ...terms, pin }, signal),
        message: "Mandate created.",
      })} />
      <section className="panel" aria-labelledby="mandates-title" aria-busy={busy}>
        <h2 id="mandates-title">Your mandates</h2>
        {mutationFailure && <ErrorNotice error={mutationFailure} />}
        {notice && <p role="status">{notice}{readFailure && " The displayed list needs refreshing."}</p>}
        {readFailure && (
          <>
            <ErrorNotice error={readFailure.error} />
            <button type="button" disabled={busy} onClick={() => void run(readFailure.cursor)}>Retry mandate list</button>
          </>
        )}
        {busy && <p role="status">Loading mandates…</p>}
        {!busy && !readFailure && page?.items.length === 0 && <p>No mandates yet.</p>}
        {page && (
          <>
            <div className="mandate-records">
              {page.items.map((item) => (
                <MandateRecord key={item.terms.mandate_id} item={item} bootstrap={bootstrap}
                  disabled={actionsDisabled} revoke={() => void run(cursor, {
                    perform: (signal) => api.revokeMandate(token, item.terms.mandate_id, signal),
                    message: "Mandate revoked.",
                  })} />
              ))}
            </div>
            <div className="mandate-buttons">
              {cursor !== null && <button type="button" className="secondary" disabled={actionsDisabled}
                onClick={() => void run(null)}>First page</button>}
              {page.next_cursor !== null && <button type="button" className="secondary" disabled={actionsDisabled}
                onClick={() => void run(page.next_cursor)}>Next page</button>}
            </div>
          </>
        )}
      </section>
    </div>
  );
}

function MandateRecord({ item, bootstrap, disabled, revoke }: {
  item: MandateListItem; bootstrap: CustomerBootstrap; disabled: boolean; revoke: () => void;
}) {
  const { terms, status, usage } = item;
  const agent = bootstrap.agents.find((card) => card.agent_id === terms.agent_id);
  return (
    <article className="mandate-record">
      <h3>{agent ? `${agent.name} · ${agent.vendor}` : terms.agent_id}</h3>
      <p>Mandate <code>{terms.mandate_id}</code></p>
      <p>{status} · Expires <time dateTime={terms.valid_until}>{formatKolkataTimestamp(terms.valid_until)}</time></p>
      <ul>
        {terms.grants.map((grant) => {
          const accounts = grant.account_ids.map((id) => bootstrap.accounts.find((account) => account.account_id === id));
          const used = usage.find((record) => record.grant_id === grant.grant_id);
          return (
            <li key={grant.grant_id}>
              <strong>{actionLabels[grant.action]}</strong>
              {" · "}{accounts.map((account) => account ? maskedAccount(account) : "Account ••••").join(", ")}
              {used ? <Usage usage={used} /> : <p>Usage unavailable.</p>}
            </li>
          );
        })}
      </ul>
      {status !== "REVOKED" && <button type="button" className="secondary" disabled={disabled}
        onClick={revoke}>Revoke mandate</button>}
    </article>
  );
}

function Usage({ usage }: { usage: GrantUsage }) {
  const text = (amount: number | null, count: number | null) => [
    amount === null ? null : formatInrPaise(amount),
    count === null ? null : `${count} ${count === 1 ? "use" : "uses"}`,
  ].filter((part) => part !== null).join(" · ") || "Unlimited";
  return (
    <dl className="mandate-usage">
      <div><dt>Committed</dt><dd>{text(usage.committed_paise, usage.committed_count)}</dd></div>
      <div><dt>Reserved</dt><dd>{text(usage.reserved_paise, usage.reserved_count)}</dd></div>
      <div><dt>Remaining</dt><dd>{text(usage.remaining_paise, usage.remaining_count)}</dd></div>
    </dl>
  );
}
