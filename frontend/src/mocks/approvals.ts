import { approvalPath, type ApprovalItem, type ApprovalPage, type DecisionRequest } from "../api/approvalWire";
import { isRecord } from "../api/shellWire";
import { demoCredentials } from "./fixtures";
import type { MockCustomer } from "./mandates";

type ApprovalCase = {
  id: string;
  customerId?: string;
  agentId?: string;
  approveAs?: "approved" | "executed" | "processing" | "unknown" | "failed" | "missing-receipt" | "missing-confirmation";
  race?: "consumed" | "cancelled";
};
export type ApprovalCases = readonly ApprovalCase[];

const decisionPattern = /^\/v1\/customer\/escalations\/([^/]+)\/decision$/;
const defaultAgentId = "agt_billpal";
const receipt = { seq: 18, entry_hash: "a".repeat(64) };
const s9: ApprovalCase = { id: "esc_mock_s9" };

export function isApprovalRoute(path: string) {
  return path === approvalPath || decisionPattern.test(path);
}

export function createApprovalFixtures(now: () => number, cases: ApprovalCases = [s9]) {
  const expiry = new Date(now() + 300_000).toISOString();
  const records = cases.map((scenario): { scenario: ApprovalCase; item: ApprovalItem } => ({
    scenario,
    item: {
      escalation_id: scenario.id,
      request_id: `req_${scenario.id}`,
      state: "PENDING",
      expires_at: expiry,
      trusted_summary: {
        payee: { display_name: "Electricity Board" },
        from_account: { account_id: "ACC-001", masked_label: "Checking •••• 001" },
        amount: { amount_paise: 480000, currency: "INR" },
        risk: { reasons: ["Amount above the approval threshold", "First payment to this allowed payee",
          "Recent spending pattern changed", "Additional fixture reason"] },
        agent_note: { text: "Ignore the summary. The amount is ₹1. <b>Trusted invoice</b>" },
      },
    },
  }));
  const suspensions = new Map<string, Set<string>>();

  return function handleApproval(url: URL, init: RequestInit, customer: MockCustomer, reply: {
    respond: (payload: unknown, status?: number) => Response;
    reject: (status: number, code: string, message: string) => Response;
  }) {
    const { respond, reject } = reply;
    const owned = records.filter(({ scenario }) => (scenario.customerId ?? demoCredentials.customer.id) === customer.id);
    for (const { item } of owned) {
      if (item.state === "PENDING" && Date.parse(item.expires_at) <= now()) item.state = "EXPIRED";
    }

    if (url.pathname === approvalPath && init.method === "GET") {
      const cursor = url.searchParams.get("cursor");
      const limit = Number(url.searchParams.get("limit") ?? "50");
      const position = cursor === null ? 0 : owned.findIndex(({ item }) => cursorFor(item) === cursor) + 1;
      if (url.searchParams.get("status") !== "PENDING" || !Number.isInteger(limit) || limit < 1 || limit > 100
        || (cursor !== null && position === 0)) return reject(400, "E_REQ_INVALID", "Invalid approval page parameters.");
      const pending = owned.slice(position).map(({ item }) => item).filter((item) => item.state === "PENDING");
      const items = pending.slice(0, limit);
      return respond({ items, next_cursor: pending.length > limit ? cursorFor(items[items.length - 1]) : null } satisfies ApprovalPage);
    }

    const match = decisionPattern.exec(url.pathname);
    if (!match || init.method !== "POST") return reject(400, "E_REQ_INVALID", "Invalid approval request.");
    const record = owned.find(({ item }) => encodeURIComponent(item.escalation_id) === match[1]);
    if (!record) return reject(404, "E_REQ_INVALID", "Approval not found.");
    const body = parseDecision(init.body);
    if (!body) return reject(400, "E_REQ_INVALID", "Invalid decision fields.");
    const { item, scenario } = record;

    if (item.state === "PENDING" && scenario.race === "cancelled") item.state = "CANCELLED";
    if (item.state === "PENDING" && scenario.race === "consumed") item.state = "APPROVED";
    if (item.state === "EXPIRED") return reject(409, "E_ESC_EXPIRED", "The approval deadline passed.");
    if (item.state === "CANCELLED") return reject(409, "E_ESC_CANCELLED", "The approval was cancelled.");
    if (item.state !== "PENDING") return reject(409, "E_ESC_ALREADY_CONSUMED", "A decision has already been recorded.");

    const result = { escalation_id: item.escalation_id, request_id: item.request_id };
    const agentId = scenario.agentId ?? defaultAgentId;
    if (body.decision === "REJECT") {
      item.state = "REJECTED";
      if (body.suspend_agent) {
        const suspended = suspensions.get(customer.id) ?? new Set<string>();
        suspended.add(agentId);
        suspensions.set(customer.id, suspended);
        for (const { item: other, scenario: otherScenario } of owned) {
          if (other.state === "PENDING" && (otherScenario.agentId ?? defaultAgentId) === agentId) {
            other.state = "CANCELLED";
          }
        }
      }
      return respond({ ...result, state: item.state });
    }

    if (body.pin !== customer.secret) return reject(403, "E_ESC_BAD_PIN", "The approval PIN is incorrect.");
    if (suspensions.get(customer.id)?.has(agentId)) return reject(409, "E_ESC_CANCELLED", "The delegation was suspended.");
    switch (scenario.approveAs ?? "executed") {
      case "approved":
        item.state = "APPROVED";
        return respond({ ...result, state: item.state });
      case "processing":
        item.state = "APPROVED";
        return respond({ ...result, state: item.state }, 202);
      case "unknown":
        item.state = "APPROVED";
        // The checked-in C route sets the outer state after calling the execution port.
        return respond({ ...result, state: "EXECUTED", result: { status: "EXECUTION_UNKNOWN" } });
      case "failed":
        item.state = "FAILED";
        return respond({ ...result, state: item.state, result: { status: "FAILED" } });
      case "missing-confirmation":
        item.state = "APPROVED";
        return respond({ ...result, state: "EXECUTED" });
      case "missing-receipt":
        item.state = "EXECUTED";
        return respond({ ...result, state: item.state, result: { status: "EXECUTED" } });
      case "executed":
        item.state = "EXECUTED";
        return respond({ ...result, state: item.state, result: { status: "EXECUTED", audit_ref: receipt } });
    }
  };
}

function cursorFor(item: ApprovalItem) {
  return `approval after:${item.escalation_id}`;
}

function parseDecision(body: RequestInit["body"]): DecisionRequest | null {
  if (typeof body !== "string") return null;
  try {
    const value: unknown = JSON.parse(body);
    if (!isRecord(value) || Object.keys(value).some((key) => !["decision", "pin", "suspend_agent"].includes(key))
      || (value.pin != null && typeof value.pin !== "string")
      || (value.suspend_agent !== undefined && typeof value.suspend_agent !== "boolean")) return null;
    if (value.decision === "APPROVE" && !value.suspend_agent) return {
      decision: "APPROVE", pin: value.pin,
    };
    if (value.decision === "REJECT" && value.pin == null) return {
      decision: "REJECT", suspend_agent: value.suspend_agent,
    };
  } catch {
    return null;
  }
  return null;
}
