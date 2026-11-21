import { isApprovalPage, type ApprovalItem } from "../../../api/approvalWire";
import { isGatewayTimestamp, isMandatePage, isNonnegativeInteger, type MandateListItem } from "../../../api/mandateWire";
import { isCustomerBootstrap, isRecord, type CustomerBootstrap } from "../../../api/shellWire";
import type { SseFrame } from "../../../api/sse";

export type LedgerItem = {
  entry_id: string; account_id: string; entry_type: "DEBIT" | "CREDIT"; amount_paise: number;
  balance_after_paise: number; created_at: string; reference?: string | null;
};

export type BankApproval = ApprovalItem & { agent_id?: string; mandate_id?: string };
export type BankCatalog = Omit<CustomerBootstrap, "payees"> & {
  payees: (CustomerBootstrap["payees"][number] & { category?: string })[];
};

export type Overview = {
  customerId: string; accountId: string; balance: number; ledger: LedgerItem[];
  catalog: BankCatalog; mandates: MandateListItem[]; approvals: BankApproval[];
  receipts?: { requestId: string; amount: number; payeeId?: string }[];
};

export function stringField(record: unknown, key: string): string | undefined {
  if (!isRecord(record)) return;
  const value = record[key];
  return typeof value === "string" && value.trim() ? value : undefined;
}

export function amountField(record: unknown, key: string): number | undefined {
  return isRecord(record) && isNonnegativeInteger(record[key]) ? record[key] : undefined;
}

export function parseOverview(raw: unknown): Overview | undefined {
  if (!isRecord(raw) || raw.currency !== "INR") return;
  const { balance_paise, transactions } = raw;
  const receipts = Array.isArray(raw.receipts) ? raw.receipts.flatMap((row: unknown) => {
    const requestId = stringField(row, "request_id");
    const amount = amountField(row, "amount_paise");
    return requestId && amount !== undefined && stringField(row, "from_account") === raw.account_id
      ? [{ requestId, amount, payeeId: stringField(row, "payee_id") }] : [];
  }) : [];
  const customerId = stringField(raw, "customer_id");
  const accountId = stringField(raw, "account_id");
  const mandates = { items: raw.mandates, next_cursor: null };
  const approvals = { items: raw.escalations, next_cursor: null };
  if (!customerId || !accountId || !isNonnegativeInteger(balance_paise) || !Array.isArray(transactions)
    || !isCustomerBootstrap(raw) || !raw.accounts.some((account) => account.account_id === accountId)
    || !isMandatePage(mandates) || !isApprovalPage(approvals)) return;
  if (!transactions.every((entry: unknown): entry is LedgerItem => isRecord(entry)
    && typeof entry.entry_id === "string" && entry.account_id === accountId
    && (entry.entry_type === "DEBIT" || entry.entry_type === "CREDIT")
    && isNonnegativeInteger(entry.amount_paise) && isNonnegativeInteger(entry.balance_after_paise)
    && isGatewayTimestamp(entry.created_at)
    && (entry.reference == null || typeof entry.reference === "string"))) return;

  return {
    customerId, accountId, balance: balance_paise, ledger: transactions, receipts,
    catalog: { accounts: raw.accounts, agents: raw.agents,
      payees: raw.payees.map((payee) => ({ ...payee, category: stringField(payee, "category") })) },
    mandates: mandates.items.filter(({ terms }) => terms.grants.some(({ account_ids }) => account_ids.includes(accountId))),
    approvals: approvals.items.filter(({ trusted_summary }) => trusted_summary.from_account.account_id === accountId),
  };
}

export function signingMetadata(value: unknown) {
  return {
    hash: stringField(value, "mandate_hash"), keyId: stringField(value, "key_id"), signature: stringField(value, "sig"),
  };
}

const noticeTypes = ["bank.executed", "escalation.created", "escalation.resolved", "action.denied",
  "agent.suspended", "mandate.revoked"] as const;
type NoticeType = (typeof noticeTypes)[number];
export type BankNotice = {
  id: string; type: NoticeType; at: number; requestId?: string; agentId?: string; escalationId?: string;
  mandateId?: string; amount?: number; payeeId?: string;
};

export function decodeBankNotice(frame: SseFrame, overview: Pick<Overview, "customerId" | "accountId">): BankNotice | undefined {
  const type = noticeTypes.find((name) => name === frame.event);
  if (!type || !frame.id) return;
  let wire: unknown;
  try { wire = JSON.parse(frame.data); } catch { return; }
  if (!isRecord(wire) || !isRecord(wire.data) || wire.event_id !== frame.id
    || wire.event_type !== type || wire.customer_id !== overview.customerId) return;
  const { data } = wire;
  if (data.from_account != null && data.from_account !== overview.accountId) return;
  if (type === "bank.executed" && data.status !== "EXECUTED") return;
  const requestId = stringField(wire, "request_id");
  if (["bank.executed", "escalation.created", "escalation.resolved", "action.denied"].includes(type) && !requestId) return;
  const at = Date.parse(stringField(wire, "occurred_at") ?? "");
  if (!Number.isFinite(at)) return;
  return {
    id: frame.id, type, at, requestId, agentId: stringField(wire, "agent_id"),
    escalationId: stringField(data, "escalation_id"), mandateId: stringField(data, "mandate_id"),
    amount: amountField(data, "amount_paise"), payeeId: stringField(data, "payee_id"),
  };
}

export function addBankNotice(current: BankNotice[], notice: BankNotice) {
  if (current.some((item) => item.id === notice.id && item.at === notice.at
    || notice.requestId && item.requestId === notice.requestId && item.type === notice.type)) return current;
  return [...current, notice].sort((left, right) => right.at - left.at).slice(0, 20);
}
