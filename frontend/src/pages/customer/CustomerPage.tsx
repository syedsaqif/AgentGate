import type { ShellApi } from "../../api/client";
import { RoleWorkspace } from "../../components/RoleWorkspace";

export function CustomerPage({ api, demoId }: { api: ShellApi; demoId?: string }) {
  return (
    <RoleWorkspace role="customer" api={api} demoId={demoId} read={api.customerBootstrap} renderData={(data) => (
      <div className="cards">
        <section className="panel" aria-labelledby="accounts-title">
          <h2 id="accounts-title">Your accounts</h2>
          {data.accounts.length ? (
            <ul>{data.accounts.map((account) => (
              <li key={account.account_id}>{account.masked_label.replaceAll(account.account_id, "••••")}</li>
            ))}</ul>
          ) : <p>No accounts available.</p>}
        </section>
        <section className="panel" aria-labelledby="billers-title">
          <h2 id="billers-title">Biller catalog</h2>
          {data.payees.length ? (
            <ul>{data.payees.map((payee) => <li key={payee.payee_id}>{payee.display_name}</li>)}</ul>
          ) : <p>No billers available.</p>}
        </section>
        <section className="panel" aria-labelledby="customer-agents-title">
          <h2 id="customer-agents-title">Available agents</h2>
          {data.agents.length ? (
            <ul>{data.agents.map((agent) => (
              <li key={agent.agent_id}>{agent.name}<small>{agent.vendor}</small></li>
            ))}</ul>
          ) : <p>No agents available.</p>}
        </section>
      </div>
    )} />
  );
}
