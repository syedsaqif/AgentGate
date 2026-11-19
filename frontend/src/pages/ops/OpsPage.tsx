import type { ShellApi } from "../../api/client";
import { RoleWorkspace } from "../../components/RoleWorkspace";

export function OpsPage({ api, demoId }: { api: ShellApi; demoId?: string }) {
  return (
    <RoleWorkspace role="ops" api={api} demoId={demoId} read={api.registeredAgents} renderData={(page) => (
      <section className="panel" aria-labelledby="registered-agents-title">
        <h2 id="registered-agents-title">Registered agents</h2>
        {page.items.length ? (
          <ul className="agents">
            {page.items.map((agent) => (
              <li key={agent.agent_id}>
                <div>{agent.name}<small>{agent.vendor}</small></div>
                <span className="agent-status">{agent.status}</span>
              </li>
            ))}
          </ul>
        ) : <p>No registered agents.</p>}
        {page.next_cursor !== null && <p>Showing the first page of registered agents.</p>}
      </section>
    )} />
  );
}
