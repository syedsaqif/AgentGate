import type { operations, paths } from "./generated/openapi";
import { isGatewayTimestamp, isNonnegativeInteger } from "./mandateWire";
import { isRecord } from "./shellWire";

export const approvalPath = "/v1/customer/escalations" satisfies keyof paths;
export const decisionPath = "/v1/customer/escalations/{id}/decision" satisfies keyof paths;
export type ApprovalQuery = NonNullable<operations["listEscalations"]["parameters"]["query"]>;
export type DecisionPathParams = operations["decideEscalation"]["parameters"]["path"];
export type DecisionRequest = operations["decideEscalation"]["requestBody"]["content"]["application/json"];
export type CustomerDecision =
  | { decision: Extract<DecisionRequest["decision"], "APPROVE">; pin: NonNullable<DecisionRequest["pin"]>; suspend_agent?: never }
  | { decision: Extract<DecisionRequest["decision"], "REJECT">; suspend_agent?: true; pin?: never };

// Member C: IC0 omits the response schemas. Project Escalation, the trusted
// summary in gateway/escalation/summary.py, and the decision response in routes.py.
const states = ["PENDING", "APPROVED", "EXECUTED", "REJECTED", "EXPIRED", "CANCELLED", "FAILED"] as const;
export type ApprovalState = typeof states[number];
type ActionStatus = "RECEIVED" | "PENDING_APPROVAL" | "DISPATCHED" | "EXECUTED" | "REJECTED"
  | "EXECUTION_UNKNOWN" | "FAILED";
export type AuditReceipt = { seq: number; entry_hash: string };
type ActionResult = { status: ActionStatus; audit_ref?: AuditReceipt };

export type ApprovalItem = {
  escalation_id: string;
  request_id: string;
  state: ApprovalState;
  expires_at: string;
  trusted_summary: {
    payee: { display_name: string } | null;
    from_account: { account_id: string; masked_label: string };
    amount: { amount_paise: number; currency: "INR" };
    risk?: { reasons: string[] } | null;
    agent_note?: { text: string } | null;
  };
};
export type ApprovalPage = { items: ApprovalItem[]; next_cursor: string | null };

// Only fields present and valid in the provider reply are shown as observations.
export type DecisionObservation = {
  processing: boolean;
  requestId?: string;
  state?: ApprovalState;
  result?: ActionResult;
};

function isApprovalState(value: unknown): value is ApprovalState {
  return states.some((state) => state === value);
}

function isActionStatus(value: unknown): value is ActionStatus {
  return value === "RECEIVED" || value === "PENDING_APPROVAL" || value === "DISPATCHED"
    || value === "EXECUTED" || value === "REJECTED" || value === "EXECUTION_UNKNOWN" || value === "FAILED";
}

function isReceipt(value: unknown): value is AuditReceipt {
  return isRecord(value) && isNonnegativeInteger(value.seq) && value.seq > 0
    && typeof value.entry_hash === "string" && /^[0-9a-f]{64}$/.test(value.entry_hash);
}

export function observeDecision(payload: unknown, status: number, expectedId: string): DecisionObservation {
  const processing = status === 202;
  if (!isRecord(payload) || (payload.escalation_id !== undefined && payload.escalation_id !== expectedId)) {
    return { processing };
  }
  const result = isRecord(payload.result) && isActionStatus(payload.result.status)
    ? { status: payload.result.status, audit_ref: isReceipt(payload.result.audit_ref) ? payload.result.audit_ref : undefined }
    : undefined;
  return {
    processing, result,
    requestId: typeof payload.request_id === "string" ? payload.request_id : undefined,
    state: isApprovalState(payload.state) ? payload.state : undefined,
  };
}

function isApprovalItem(value: unknown): value is ApprovalItem {
  if (!isRecord(value) || !isRecord(value.trusted_summary)) return false;
  const { payee, from_account, amount, risk, agent_note } = value.trusted_summary;
  return typeof value.escalation_id === "string" && value.escalation_id !== ""
    && typeof value.request_id === "string" && isApprovalState(value.state) && isGatewayTimestamp(value.expires_at)
    && (payee === null || (isRecord(payee) && typeof payee.display_name === "string"))
    && isRecord(from_account) && typeof from_account.account_id === "string" && from_account.account_id !== ""
    && typeof from_account.masked_label === "string" && from_account.masked_label !== ""
    && isRecord(amount) && amount.currency === "INR" && isNonnegativeInteger(amount.amount_paise)
    && (risk == null || (isRecord(risk) && Array.isArray(risk.reasons)
      && risk.reasons.every((reason: unknown) => typeof reason === "string")))
    && (agent_note == null || (isRecord(agent_note) && typeof agent_note.text === "string"));
}

export function isApprovalPage(value: unknown): value is ApprovalPage {
  return isRecord(value) && (value.next_cursor === null || typeof value.next_cursor === "string")
    && Array.isArray(value.items) && value.items.every(isApprovalItem);
}
