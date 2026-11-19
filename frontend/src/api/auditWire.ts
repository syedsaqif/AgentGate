import type { operations, paths } from "./generated/openapi";
import { isGatewayTimestamp, isNonnegativeInteger } from "./mandateWire";
import { isRecord } from "./shellWire";

export const auditEntriesPath = "/v1/audit/entries" satisfies keyof paths;
export const auditVerifyPath = "/v1/audit/verify" satisfies keyof paths;
export const auditEvidencePath = "/v1/audit/evidence/{request_id}" satisfies keyof paths;
export type EvidencePathParams = operations["getEvidence"]["parameters"]["path"];
type AuditPagination = NonNullable<operations["listAuditEntries"]["parameters"]["query"]>;
export type AuditCursor = AuditPagination["cursor"];

// B's unmerged route adds exact-match filters beyond the published pagination parameters.
export const auditFilterKeys = ["event_type", "request_id", "customer_id", "agent_id"] as const;
export type AuditFilters = Partial<Record<typeof auditFilterKeys[number], string>>;

// IC0 omits the 200 schemas. These projections follow B's audit_models.py and
// list_entries implementation. Keep provider-redacted JSON fields intact.
export type AuditEntry = Record<string, unknown> & {
  seq?: number | null;
  event_type?: string | null;
  request_id?: string | null;
  occurred_at?: string | null;
};
export type AuditPage = { items: AuditEntry[]; next_cursor: string | null };

export type VerifyReport = {
  valid: boolean;
  entries_checked: number;
  head_seq: number;
  head_hash: string;
  anchored_through_seq: number;
  first_failure?: { seq: number; reason: string } | null;
};

export type EvidenceBundle = Record<string, unknown> & {
  request_id: string;
  mandate: Record<string, unknown>;
  agent: Record<string, unknown>;
  action: Record<string, unknown>;
  signed_request: Record<string, unknown>;
  decisions: Record<string, unknown>[];
  approval_history: Record<string, unknown>[];
  bank_outcome: Record<string, unknown> | null;
  audit_references: Record<string, unknown>[];
  verification: VerifyReport;
};

function isAuditEntry(value: unknown): value is AuditEntry {
  return isRecord(value)
    && (value.seq == null || (isNonnegativeInteger(value.seq) && value.seq > 0))
    && (value.event_type == null || typeof value.event_type === "string")
    && (value.request_id == null || typeof value.request_id === "string")
    && (value.occurred_at == null || isGatewayTimestamp(value.occurred_at));
}

export function isAuditPage(value: unknown): value is AuditPage {
  return isRecord(value) && (value.next_cursor === null || typeof value.next_cursor === "string")
    && Array.isArray(value.items) && value.items.every(isAuditEntry);
}

export function isVerifyReport(value: unknown): value is VerifyReport {
  if (!isRecord(value)) return false;
  const { valid, entries_checked, head_seq, head_hash, anchored_through_seq, first_failure } = value;
  return typeof valid === "boolean" && isNonnegativeInteger(entries_checked) && isNonnegativeInteger(head_seq)
    && typeof head_hash === "string" && isNonnegativeInteger(anchored_through_seq)
    && (first_failure == null || (isRecord(first_failure) && isNonnegativeInteger(first_failure.seq)
      && first_failure.seq > 0 && typeof first_failure.reason === "string"))
    && (!valid || (first_failure == null && anchored_through_seq <= head_seq));
}

export function isEvidenceBundle(value: unknown): value is EvidenceBundle {
  return isRecord(value) && typeof value.request_id === "string" && value.request_id !== ""
    && isRecord(value.mandate) && isRecord(value.agent) && isRecord(value.action)
    && isRecord(value.signed_request) && typeof value.signed_request.proof_jws === "string"
    && Array.isArray(value.decisions) && value.decisions.every(isRecord)
    && Array.isArray(value.approval_history) && value.approval_history.every(isRecord)
    && (value.bank_outcome === null || isRecord(value.bank_outcome))
    && Array.isArray(value.audit_references) && value.audit_references.every(isRecord)
    && isVerifyReport(value.verification);
}
