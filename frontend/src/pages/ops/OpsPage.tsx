import { useCallback, useRef } from "react";
import type { ShellApi } from "../../api/client";
import type { ApiError } from "../../api/errors";
import { ErrorNotice } from "../../components/ErrorNotice";
import { RoleWorkspace } from "../../components/RoleWorkspace";
import { formatKolkataTimestamp } from "../../formatting";
import { useOpsFeed } from "./useOpsFeed";
import { useRegisteredAgents } from "./useRegisteredAgents";

export function OpsPage({ api, demoId }: { api: ShellApi; demoId?: string }) {
  return (
    <RoleWorkspace role="ops" api={api} demoId={demoId}
      read={() => Promise.resolve(undefined)}
      renderData={(_, session, endSession) => (
        <OpsWorkspace api={api} token={session.session_token} endSession={endSession} />
      )} />
  );
}

function OpsWorkspace({ api, token, endSession }: { api: ShellApi; token: string; endSession: (error: ApiError) => void }) {
  // The agent read and SSE connection have independent recovery paths.
  const ended = useRef(false);
  const closeSession = useCallback((error: ApiError) => {
    if (ended.current) return;
    ended.current = true;
    endSession(error);
  }, [endSession]);
  const agents = useRegisteredAgents(api, token, closeSession);
  const feed = useOpsFeed(api, token, closeSession, agents.refresh);
  return (
    <div className="ops-panels">
      <section className="panel" aria-labelledby="ops-decisions-title">
        <h2 id="ops-decisions-title">Live decision feed</h2>
        <p role="status">{{
          connecting: "Connecting to the Ops stream…",
          live: "Live stream connected.",
          recovering: "Stream disconnected. Reconnecting and polling agent status every two seconds.",
        }[feed.connection]}</p>
        {feed.error && <ErrorNotice error={feed.error} />}
        {feed.historyGap && <p>Decision history may be incomplete. The feed shows available events.</p>}
        {!feed.decisions.length && <p>No decisions received.</p>}
        <ol className="ops-decisions">
          {feed.decisions.map((event) => (
            <li key={event.id}>
              <p><strong>{event.decision}</strong>{" · "}
                <time dateTime={event.occurredAt}>{formatKolkataTimestamp(event.occurredAt)}</time></p>
              <dl className="ops-decision-details">
                <div><dt>Request</dt><dd>{event.requestId}</dd></div>
                <div><dt>Agent</dt><dd>{event.agentId}</dd></div>
                <div><dt>Risk score</dt><dd>{event.score ?? "Unavailable"}</dd></div>
              </dl>
              {event.reasons.length > 0 && <ul aria-label="Decision reasons">
                {event.reasons.map((reason, index) => <li key={index}>{reason}</li>)}
              </ul>}
            </li>
          ))}
        </ol>
      </section>

      <section className="panel" aria-labelledby="registered-agents-title" aria-busy={agents.loading}>
        <h2 id="registered-agents-title">Registered agents</h2>
        <p>Suspension applies to this agent across all customers. Reinstatement allows future actions under valid authority. Old tokens and approvals remain invalid.</p>
        {agents.message && <p role="status">{agents.message}</p>}
        {agents.mutationError && <ErrorNotice error={agents.mutationError} />}
        {agents.readError && <ErrorNotice error={agents.readError} />}
        {agents.loading && <p role="status">Refreshing agent status…</p>}
        {agents.page?.items.length === 0 && <p>No registered agents.</p>}
        {agents.page && <ul className="agents">
          {agents.page.items.map((agent) => {
            const stale = agents.stale.has(agent.agent_id);
            const pending = agents.pendingAgent === agent.agent_id;
            const action = agent.status === "ACTIVE" ? "suspend" : "reinstate";
            const actionLabel = action === "suspend" ? "Suspend globally" : "Reinstate";
            return (
              <li key={agent.agent_id}>
                <div>{agent.name}<small>{agent.vendor}</small><small>{agent.agent_id}</small></div>
                <div className="ops-agent-controls">
                  {pending ? <span>Update in progress</span>
                    : stale ? <span>Stale status · Last listed {agent.status}</span>
                    : <span className="agent-status">{agent.status}</span>}
                  {agent.status !== "DISABLED" && (
                    <button type="button" className="secondary" aria-label={`${actionLabel} ${agent.name}`}
                      disabled={stale || Boolean(agents.pendingAgent)}
                      onClick={() => void agents.update(agent.agent_id, agent.name, action)}>
                      {actionLabel}
                    </button>
                  )}
                </div>
              </li>
            );
          })}
        </ul>}
        <div className="mandate-buttons ops-agent-actions">
          <button type="button" className="secondary" disabled={agents.loading || Boolean(agents.pendingAgent)}
            onClick={() => agents.refresh()}>{agents.readError ? "Retry agent refresh" : "Refresh agents"}</button>
          {agents.cursor !== null && <button type="button" className="secondary"
            disabled={agents.loading || Boolean(agents.pendingAgent)} onClick={() => void agents.load(null)}>First agent page</button>}
          {agents.page?.next_cursor != null && <button type="button" className="secondary"
            disabled={agents.loading || Boolean(agents.pendingAgent)}
            onClick={() => void agents.load(agents.page?.next_cursor ?? null)}>Next agent page</button>}
        </div>
      </section>
    </div>
  );
}
