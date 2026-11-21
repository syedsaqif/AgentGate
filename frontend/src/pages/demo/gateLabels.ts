import { formatInrPaise } from "../../formatting";
import { integer, type GateStep } from "./gateWire";

const labels: Record<string, string> = {
  bill_pay: "Bill payment",
  idempotency_key: "Retry key",
  request_body: "Request format",
  authenticate_agent: "Agent authentication",
  agent_status: "Agent registry status",
  build_action: "Verified request",
  claim: "Duplicate request check",
  stored_result: "Stored result",
  active_binding: "Active mandate and ownership",
  mandate_status: "Mandate status",
  action_binding: "Mandate and request match",
  auth_binding: "Mandate and identity match",
  action_grant: "Allowed action",
  account_allowlist: "Allowed account",
  payee_allowlist: "Payee allow-list",
  per_txn_cap: "Per-transaction cap",
  total_cap: "Mandate budget",
  count_cap: "Payment count limit",
  amount_above_threshold: "Approval threshold",
  new_payee: "New payee",
  near_limit_structuring: "Amount close to the cap",
  amount_outlier: "Unusually large amount",
  intent_mismatch: "Category mismatch",
  off_pattern_time: "Unusual time",
  probing: "Repeated denied attempts",
  recommendation: "Breaker assessment",
  suspend_agent: "Suspend agent",
  write_ahead_audit: "Decision audit committed",
  reservation_and_dispatch_audit_committed: "Budget reserved and dispatch recorded",
  create_escalation: "Approval created",
  awaiting_approval: "Waiting for approval",
  execute_approved: "Execute approved payment",
  bank_response: "Bank response received",
  outcome: "Execution result",
  reject: "Request blocked",
  verdict: "Policy result",
  dispute_receipt_v1: "Payment receipt",
};

export function gateLabel(value: string) {
  const words = value.replaceAll(".", "_");
  return labels[words] ?? words.replaceAll("_", " ").replace(/^./, (first) => first.toUpperCase());
}

const reasons: Record<string, string> = {
  E_AUTH_MISSING: "The request was missing authentication.",
  E_AUTH_INVALID_TOKEN: "The access token could not be verified.",
  E_AUTH_EXPIRED: "The authorization had expired.",
  E_AUTH_TOKEN_REVOKED: "The access token had been revoked.",
  E_AUTH_PROOF_INVALID: "The signed request proof could not be verified.",
  E_AUTH_KEY_MISMATCH: "The signing key did not match the registered key.",
  E_AUTH_REPLAY: "This signed proof had already been used.",
  E_AUTH_CLOCK_SKEW: "The proof timestamp was outside the allowed window.",
  E_AUTH_AGENT_SUSPENDED: "The agent was already suspended.",
  E_MANDATE_REVOKED: "The customer had revoked the mandate.",
  E_REQ_IDEMPOTENCY_CONFLICT: "The retry key was already used with different payment details.",
  E_POLICY_PAYEE_NOT_ALLOWED: "The mandate did not allow this payee.",
  E_RISK_HIGH: "The risk engine classified the request as high risk.",
  E_RISK_BREAKER_OPEN: "The circuit breaker blocked execution.",
  E_ESC_BAD_PIN: "The approval PIN could not be verified.",
  E_ESC_EXPIRED: "The deadline for approval had passed.",
  E_BANK_INSUFFICIENT_FUNDS: "The account had insufficient funds.",
};

export const explainReason = (code?: string) => code ? reasons[code] : undefined;

export function explainCheck(step: GateStep, payee?: string): string | undefined {
  const { check, detail } = step;
  if (check === "payee_allowlist" && step.status === "fail") {
    return payee ? `The mandate did not allow payments to ${payee}.` : "The mandate did not allow this payee.";
  }
  const requested = integer(detail.requested_paise ?? detail.projected_paise);
  const limit = integer(detail.limit_paise ?? detail.threshold_paise);
  if (requested !== undefined && limit !== undefined) {
    return `${formatInrPaise(requested)} ${check === "total_cap" ? "projected" : "requested"} · ${formatInrPaise(limit)} limit.`;
  }
  if (check === "count_cap" && typeof detail.projected_count === "number" && typeof detail.limit === "number") {
    return `${detail.projected_count} payments including this one · ${detail.limit} allowed.`;
  }
  return explainReason(typeof detail.reason_code === "string" ? detail.reason_code : undefined);
}
