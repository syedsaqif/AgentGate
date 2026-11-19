import type { CustomerBootstrap, RegisteredAgents, Role } from "../api/shellWire";

// These public demo credentials are disposable and are used only in mock mode.
export const demoCredentials = {
  customer: { id: "cust_001", secret: "4821" },
  ops: { id: "ops_demo", secret: "agentgate-demo" },
} satisfies Record<Role, { id: string; secret: string }>;

export const demoIds = { customer: demoCredentials.customer.id, ops: demoCredentials.ops.id };
export const demoSessionTtlMs = 10 * 60_000;

const billPal = { agent_id: "agt_billpal", name: "BillPal", vendor: "BillPal Demo" };

export const customerBootstrap: CustomerBootstrap = {
  accounts: [{ account_id: "ACC-001", masked_label: "Checking •••• 001" }],
  payees: [{ payee_id: "BILL-ELEC", display_name: "Electricity Board" }],
  agents: [{
    ...billPal,
    jkt: "fixture-billpal-key-fingerprint",
    max_scopes: ["bill_pay", "balance_read", "transactions_read"],
  }],
};

export const registeredAgents: RegisteredAgents = {
  items: [
    { ...billPal, status: "ACTIVE" },
    { agent_id: "agt_budget", name: "Budget Assistant", vendor: "AgentGate Demo", status: "SUSPENDED" },
  ],
  next_cursor: null,
};

export const emptyBootstrap: CustomerBootstrap = { accounts: [], payees: [], agents: [] };
export const emptyAgents: RegisteredAgents = { items: [], next_cursor: null };
