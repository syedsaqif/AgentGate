import { formatKolkataTimestamp } from "../formatting";
import type { operations, paths } from "./generated/openapi";
import { isRecord } from "./shellWire";

export const mandatePath = "/v1/customer/mandates" satisfies keyof paths;
export const revokePath = "/v1/customer/mandates/{id}/revoke" satisfies keyof paths;
export type RevokePathParams = operations["revokeMandate"]["parameters"]["path"];

// Member A: IC0 omits the create body. This is the customer-selectable projection
// of contracts/core_models.py and the example in tests/a/test_mandates.py.
export const mandateActions = ["bill_pay", "transfer", "balance_read", "transactions_read"] as const;
export type MandateAction = typeof mandateActions[number];

export function isMoneyAction(action: MandateAction): boolean {
  return action === "bill_pay" || action === "transfer";
}

export type TimeWindow = {
  timezone: "Asia/Kolkata";
  weekdays: number[];
  start_local: string;
  end_local: string;
};

export type GrantInput = {
  grant_id: string;
  action: MandateAction;
  account_ids: string[];
  payee_ids?: string[];
  currency?: "INR";
  max_per_txn_paise?: number;
  max_total_paise?: number;
  max_count: number;
  window: "mandate";
  time_window?: TimeWindow;
};

export type MandateTermsInput = {
  agent_id: string;
  valid_from: string;
  valid_until: string;
  grants: GrantInput[];
  risk_profile: "balanced";
  step_up: {
    amount_above_paise?: number;
    new_payee_requires_approval: boolean;
    always_ask_actions: MandateAction[];
  };
};
export type CreateMandateBody = MandateTermsInput & { pin: string };

// Member A: IC0 omits list query parameters and the 200 body. Use the common
// Page with a MandateSnapshot projection and UsageSnapshot entries per item.
type GrantSummary = Pick<GrantInput, "grant_id" | "action" | "account_ids">;
export type GrantUsage = {
  grant_id: string;
  committed_paise: number;
  reserved_paise: number;
  remaining_paise: number | null;
  committed_count: number;
  reserved_count: number;
  remaining_count: number | null;
};
type MandateStatus = "ACTIVE" | "REVOKED" | "SUSPENDED" | "EXPIRED";

export type MandateListItem = {
  terms: { mandate_id: string; agent_id: string; valid_until: string; grants: GrantSummary[] };
  status: MandateStatus;
  usage: GrantUsage[];
};
export type MandatePage = { items: MandateListItem[]; next_cursor: string | null };

export function isNonnegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

export function isMandateAction(value: unknown): value is MandateAction {
  return mandateActions.some((action) => action === value);
}

export function isGatewayTimestamp(value: unknown): value is string {
  if (typeof value !== "string") return false;
  try {
    formatKolkataTimestamp(value);
    return true;
  } catch {
    return false;
  }
}

function isGrantSummary(value: unknown): value is GrantSummary {
  return isRecord(value) && typeof value.grant_id === "string" && isMandateAction(value.action)
    && Array.isArray(value.account_ids) && value.account_ids.length > 0
    && value.account_ids.every((id: unknown) => typeof id === "string" && id.length > 0);
}

function isGrantUsage(value: unknown): value is GrantUsage {
  return isRecord(value) && typeof value.grant_id === "string"
    && isNonnegativeInteger(value.committed_paise) && isNonnegativeInteger(value.reserved_paise)
    && (value.remaining_paise === null || isNonnegativeInteger(value.remaining_paise))
    && isNonnegativeInteger(value.committed_count) && isNonnegativeInteger(value.reserved_count)
    && (value.remaining_count === null || isNonnegativeInteger(value.remaining_count));
}

function isMandateListItem(value: unknown): value is MandateListItem {
  if (!isRecord(value) || !isRecord(value.terms)) return false;
  const { terms, status, usage } = value;
  return typeof terms.mandate_id === "string" && typeof terms.agent_id === "string"
    && isGatewayTimestamp(terms.valid_until)
    && Array.isArray(terms.grants) && terms.grants.length > 0 && terms.grants.every(isGrantSummary)
    && (status === "ACTIVE" || status === "REVOKED" || status === "SUSPENDED" || status === "EXPIRED")
    && Array.isArray(usage) && usage.every(isGrantUsage);
}

export function isMandatePage(value: unknown): value is MandatePage {
  return isRecord(value) && (value.next_cursor === null || typeof value.next_cursor === "string")
    && Array.isArray(value.items) && value.items.every(isMandateListItem);
}
