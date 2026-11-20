import type { PathForMethod } from "./types";

export type Role = "customer" | "ops";

// Member B — POST /v1/customer/login and POST /v1/ops/login.
// IC0 omits both request bodies and both JSON 200 schemas.
export type CustomerLogin = { customer_id: string; pin: string };
export type OpsLogin = { operator_id: string; password: string };
export type HumanSession = { session_token: string; role: Role; expires_at: string };

// Member A — GET /v1/customer/bootstrap. IC0 describes the content without field names.
type AccountCard = { account_id: string; masked_label: string };
type PayeeCard = { payee_id: string; display_name: string };
type AgentCard = { agent_id: string; name: string; vendor: string };
type DelegableAgent = AgentCard & { jkt?: string | null; max_scopes?: string[] };
export type CustomerBootstrap = { accounts: AccountCard[]; payees: PayeeCard[]; agents: DelegableAgent[] };

// Member B — GET /v1/ops/agents. IC0 omits the JSON 200 schema.
type AgentStatus = "ACTIVE" | "SUSPENDED" | "DISABLED";
export type RegisteredAgent = AgentCard & { status: AgentStatus };
export type RegisteredAgents = { items: RegisteredAgent[]; next_cursor: string | null };

export const shellRoutes = {
  customer: { login: "/v1/customer/login", read: "/v1/customer/bootstrap" },
  ops: { login: "/v1/ops/login", read: "/v1/ops/agents" },
} satisfies Record<Role, { login: PathForMethod<"post">; read: PathForMethod<"get"> }>;

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasStrings(value: unknown, keys: string[]): boolean {
  return isRecord(value) && keys.every((key) => typeof value[key] === "string" && value[key] !== "");
}

export function isHumanSession(value: unknown, role: Role): value is HumanSession {
  return isRecord(value)
    && value.role === role
    && typeof value.session_token === "string"
    && /^[A-Za-z0-9._~+/=-]+$/.test(value.session_token)
    && typeof value.expires_at === "string"
    && Number.isFinite(Date.parse(value.expires_at));
}

function hasCards(value: unknown, fields: string[]): boolean {
  return Array.isArray(value) && value.every((card) => hasStrings(card, fields));
}

export function isCustomerBootstrap(value: unknown): value is CustomerBootstrap {
  return isRecord(value)
    && hasCards(value.accounts, ["account_id", "masked_label"])
    && hasCards(value.payees, ["payee_id", "display_name"])
    && Array.isArray(value.agents)
    && value.agents.every((agent) => isRecord(agent)
      && hasStrings(agent, ["agent_id", "name", "vendor"])
      && (agent.jkt == null || typeof agent.jkt === "string")
      && (agent.max_scopes === undefined || (Array.isArray(agent.max_scopes)
        && agent.max_scopes.every((scope: unknown) => typeof scope === "string"))));
}

export function isRegisteredAgents(value: unknown): value is RegisteredAgents {
  return isRecord(value)
    && (value.next_cursor === null || typeof value.next_cursor === "string")
    && Array.isArray(value.items)
    && value.items.every((agent) => isRecord(agent)
      && hasStrings(agent, ["agent_id", "name", "vendor"])
      && (agent.status === "ACTIVE" || agent.status === "SUSPENDED" || agent.status === "DISABLED"));
}
