import {
  auditEntriesPath, auditFilterKeys, auditVerifyPath,
  type AuditEntry, type AuditPage, type EvidenceBundle, type VerifyReport,
} from "../api/auditWire";

const evidenceRoute = /^\/v1\/audit\/evidence\/([^/]+)$/;

export function isAuditRoute(path: string) {
  return path === auditEntriesPath || path === auditVerifyPath || evidenceRoute.test(path);
}

const partial: VerifyReport = {
  valid: true, entries_checked: 8, head_seq: 8, head_hash: "8".repeat(64),
  anchored_through_seq: 5, first_failure: null,
};
export const auditVerifyFixtures = {
  partial,
  edited: {
    valid: false, entries_checked: 2, head_seq: 2, head_hash: "2".repeat(64),
    anchored_through_seq: 0, first_failure: { seq: 2, reason: "entry_hash_mismatch" },
  },
  gap: {
    valid: false, entries_checked: 3, head_seq: 5, head_hash: "5".repeat(64),
    anchored_through_seq: 0, first_failure: { seq: 3, reason: "sequence_gap: expected seq 3, found 4" },
  },
  signature: {
    valid: false, entries_checked: 2, head_seq: 2, head_hash: "2".repeat(64),
    anchored_through_seq: 0, first_failure: { seq: 2, reason: "invalid_signature" },
  },
  truncated: {
    valid: false, entries_checked: 4, head_seq: 4, head_hash: "4".repeat(64),
    anchored_through_seq: 0, first_failure: { seq: 5, reason: "tail_truncation: head_seq 4 is below anchored seq 5" },
  },
} satisfies Record<string, VerifyReport>;
export type AuditVerificationFixture = keyof typeof auditVerifyFixtures;

const firstRequest = "req_mock_s11";
const secondRequest = "req_mock_s11_followup";
const occurredAt = "2026-11-17T04:02:00Z";

const entries: AuditEntry[] = [
  { seq: 1, event_type: "agent.registered", agent_id: "agt_billpal", occurred_at: "2026-11-17T04:00:00Z" },
  { seq: 2, event_type: "policy.decision", request_id: firstRequest, decision: "ESCALATE" },
  { seq: 3, event_type: "risk.scored", request_id: firstRequest },
  { seq: 4, event_type: "escalation.approved", request_id: firstRequest },
  { seq: 5, event_type: "bank.executed", request_id: firstRequest, safe_payload: { reference: "[REDACTED]" } },
  { seq: 6, event_type: "policy.decision", request_id: secondRequest, decision: "ALLOW" },
  { seq: 7, event_type: "bank.executed", request_id: secondRequest },
  { seq: 8, event_type: "agent.registered", agent_id: "agt_budget" },
].map((entry) => ({ occurred_at: occurredAt, agent_id: "agt_billpal", customer_id: "cust_001", ...entry }));

function evidence(requestId: string, name: string, seq: number, approval: boolean): EvidenceBundle {
  const mandateId = "mnd_mock_historical";
  const mandateHash = `sha256:${"1".repeat(64)}`;
  return {
    request_id: requestId,
    mandate: {
      terms: {
        mandate_id: mandateId, version: 1, agent_id: "agt_billpal", customer_id: "cust_001",
        valid_from: "2026-11-17T04:00:00Z", valid_until: "2026-12-17T04:00:00Z",
        grants: [{ grant_id: "grant_bill_pay", action: "bill_pay", account_ids: ["ACC-001"],
          payee_ids: ["BILL-ELEC"], currency: "INR", window: "mandate", max_per_txn_paise: 480000,
          max_total_paise: 1000000, max_count: 3 }],
        step_up: { amount_above_paise: 400000, new_payee_requires_approval: true, always_ask_actions: [] },
        risk_profile: "balanced",
      },
      status: "ACTIVE", mandate_hash: mandateHash, sig: "fixture-mandate-signature", key_id: "fixture-mandate-key",
    },
    agent: {
      agent_id: "agt_billpal", name, vendor: "BillPal Demo",
      public_jwk: { kty: "OKP", crv: "Ed25519", x: "fixture-public-key" },
      jkt: "fixture-historical-fingerprint", max_scopes: ["bill_pay"], status: "ACTIVE",
    },
    action: {
      request_id: requestId, customer_id: "cust_001", agent_id: "agt_billpal", mandate_id: mandateId,
      mandate_hash: mandateHash, action_type: "bill_pay", received_at: occurredAt,
      params: { from_account: "ACC-001", payee_id: "BILL-ELEC", amount_paise: 480000, currency: "INR" },
      intent: { task: "pay_electricity_bill", category: "utilities.electricity" },
      idempotency_key: `fixture-bill-${seq}`,
    },
    signed_request: {
      proof_jws: "fixture-header.fixture-payload.fixture-signature", proof_jti: `fixture-proof-${seq}`,
      body_sha256: "fixture-body-digest", action_hash: `sha256:${"2".repeat(64)}`,
    },
    decisions: [{ event_type: "policy.decision", decision: approval ? "ESCALATE" : "ALLOW",
      risk: { score: approval ? 50 : 10, level: approval ? "MEDIUM" : "LOW" } }],
    approval_history: approval ? [{ event_type: "escalation.approved", occurred_at: occurredAt,
      safe_payload: { escalation_id: "esc_mock_s9" } }] : [],
    bank_outcome: { status: "EXECUTED", executed_at: occurredAt, bank_operation_id: `fixture-bank-operation-${seq}`,
      bank_idempotency_key: `fixture-bank-key-${seq}` },
    audit_references: [{ seq, entry_hash: String(seq).repeat(64) }],
    verification: partial,
  };
}

export const evidenceFixtures = {
  [firstRequest]: evidence(firstRequest, "BillPal v1 (historical)", 5, true),
  [secondRequest]: evidence(secondRequest, "BillPal v2 (historical)", 7, false),
};

export function createAuditFixtures(verification: AuditVerificationFixture, empty: boolean) {
  const records = empty ? [] : entries;
  return function handleAudit(url: URL, init: RequestInit, { respond, reject }: {
    respond: (payload: unknown, status?: number) => Response;
    reject: (status: number, code: string, message: string) => Response;
  }) {
    if (init.method !== "GET") return reject(400, "E_REQ_INVALID", "Invalid audit request.");
    if (url.pathname === auditVerifyPath) return respond(auditVerifyFixtures[verification]);
    if (url.pathname === auditEntriesPath) {
      const cursor = url.searchParams.get("cursor");
      const limit = Number(url.searchParams.get("limit") ?? 50);
      const start = cursor === null ? 0 : records.findIndex((record) => auditCursor(record.seq) === cursor) + 1;
      if (!Number.isInteger(limit) || limit < 1 || limit > 100 || (cursor !== null && start === 0)) {
        return reject(400, "E_REQ_INVALID", "Invalid audit pagination.");
      }
      const selected = records.slice(start).filter((record) => auditFilterKeys.every((filter) =>
        !url.searchParams.has(filter) || record[filter] === url.searchParams.get(filter)));
      const items = selected.slice(0, limit);
      return respond({
        items, next_cursor: selected.length > limit ? auditCursor(items[items.length - 1].seq) : null,
      } satisfies AuditPage);
    }
    const match = evidenceRoute.exec(url.pathname);
    const selected = Object.values(evidenceFixtures).find((bundle) =>
      !empty && encodeURIComponent(bundle.request_id) === match?.[1]);
    return selected ? respond(selected) : reject(404, "E_REQ_INVALID", "Audit evidence not found.");
  };
}

function auditCursor(seq: AuditEntry["seq"]) {
  return `audit after:${seq}`;
}
