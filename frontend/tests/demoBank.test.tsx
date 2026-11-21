import { act, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { readMandateDraft } from "../src/pages/customer/mandateDraft";
import { BankHome, approvalStatus, hasBankConfirmedChange } from "../src/pages/demo/bank/BankHome";
import { addBankNotice, decodeBankNotice, parseOverview, type BankApproval, type BankCatalog, type Overview } from "../src/pages/demo/bank/bankWire";
import { exampleMandate, parseMandateLanguage } from "../src/pages/demo/bank/mandateLanguage";

const now = Date.parse("2026-11-21T00:00:00Z");
const catalog: BankCatalog = {
  accounts: [{ account_id: "ACC-001", masked_label: "•••• 0001" }],
  agents: [{ agent_id: "agt_billpal", name: "BillPal Agent", vendor: "Demo", max_scopes: ["bill_pay"] }],
  payees: [{ payee_id: "BILL-ELEC", display_name: "Electricity Board", category: "utilities.electricity" }],
};
const bank: Overview = { customerId: "cust_001", accountId: "ACC-001", balance: 12500000,
  catalog, ledger: [], approvals: [], mandates: [] };
const pending: BankApproval = {
  escalation_id: "esc-1", request_id: "req-1", mandate_id: "man-1", state: "PENDING", expires_at: "2026-11-21T00:00:02Z",
  trusted_summary: {
    payee: { display_name: "Electricity Board" },
    from_account: { account_id: "ACC-001", masked_label: "•••• 0001" },
    amount: { amount_paise: 420000, currency: "INR" },
  },
};

describe("demo bank", () => {
  it("parses the example into editable fields and validates the resulting signed terms with the Customer validator", () => {
    vi.useFakeTimers();
    vi.setSystemTime(now);
    const parsed = parseMandateLanguage(exampleMandate, catalog);
    expect(parsed).toEqual({ ok: true, value: {
      agent: "agt_billpal", payees: ["BILL-ELEC"], category: "utilities.electricity", perTxn: "5000",
      lifetime: "10000", count: "5", expiry: "2026-11-28T05:30:00", threshold: "4000", newPayee: false,
    } });
    if (!parsed.ok) throw new Error(parsed.error);
    const draft = new FormData();
    draft.set("account", bank.accountId);
    draft.set("payee", parsed.value.payees[0]);
    const { agent, category, perTxn, lifetime, expiry, threshold } = parsed.value;
    for (const [name, value] of Object.entries({ agent, category, perTxn, lifetime, expiry, threshold, count: "6" })) {
      draft.set(name, value);
    }
    expect(readMandateDraft(draft, catalog, ["bill_pay"])).toMatchObject({
      ok: true, value: {
        valid_until: "2026-11-28T00:00:00.000Z",
        grants: [{ payee_ids: ["BILL-ELEC"], max_per_txn_paise: 500000, max_total_paise: 1000000,
          max_count: 6, category: "utilities.electricity" }],
        step_up: { amount_above_paise: 400000, new_payee_requires_approval: false },
      },
    });
    expect(parseMandateLanguage(exampleMandate.replace("5,000", "5,00"), catalog).ok).toBe(false);
  });

  it("ignores ALLOW and foreign-account events and deduplicates confirmed execution replay", () => {
    const makeFrame = (event: string, id: string, account = "ACC-001") => ({
      id, event, data: JSON.stringify({ event_id: id, event_type: event, customer_id: bank.customerId,
        request_id: "req-1", occurred_at: "2026-11-21T00:00:00Z",
        data: { decision: "ALLOW", status: "EXECUTED", from_account: account } }),
    });
    expect(decodeBankNotice(makeFrame("policy.decision", "1"), bank)).toBeUndefined();
    expect(decodeBankNotice(makeFrame("bank.executed", "2", "ACC-002"), bank)).toBeUndefined();
    const once = decodeBankNotice(makeFrame("bank.executed", "2"), bank)!;
    const replayed = decodeBankNotice(makeFrame("bank.executed", "3"), bank)!;
    expect(addBankNotice(addBankNotice([], once), replayed)).toEqual([once]);
  });

  it("requires a new matching provider ledger row before animating a balance change", () => {
    const changed = { ...bank, balance: 12315000 };
    const debit = { entry_id: "entry-1", account_id: "ACC-001", entry_type: "DEBIT" as const, amount_paise: 185000,
      balance_after_paise: 12315000, created_at: "2026-11-21T00:00:00Z" };
    expect(hasBankConfirmedChange(bank, changed)).toBe(false);
    expect(hasBankConfirmedChange(bank, { ...changed, ledger: [debit] })).toBe(true);
    expect(hasBankConfirmedChange({ ...bank, ledger: [debit] }, { ...changed, ledger: [debit] })).toBe(false);
    expect(parseOverview({ ...catalog, customer_id: bank.customerId, account_id: bank.accountId,
      currency: "INR", mandates: [], escalations: [], transactions: [] })).toBeUndefined();
  });

  it("clears the approval PIN on submit and removes controls when the actual deadline passes", () => {
    vi.useFakeTimers();
    vi.setSystemTime(now);
    const mutate = vi.fn(async () => true);
    render(<BankHome overview={{ ...bank, approvals: [pending] }} notices={[]} disabled={false} mutate={mutate} />);
    const pin = screen.getByLabelText("Approval PIN") as HTMLInputElement;
    expect(pin.type).toBe("password");
    pin.value = "4752";
    act(() => screen.getByRole("button", { name: "Approve" }).click());
    expect(pin.value).toBe("");
    expect(mutate).toHaveBeenCalledTimes(1);
    act(() => vi.advanceTimersByTime(2_000));
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(approvalStatus(pending, bank, Date.now())).toBe("EXPIRED");
  });

  it("keeps a revoked approval closed when an old pending notification is replayed", () => {
    const overview: Overview = { ...bank, approvals: [{ ...pending, state: "CANCELLED" }], mandates: [{
      terms: { agent_id: "agt_billpal", mandate_id: "man-1", valid_until: "2026-11-28T00:00:00Z",
        grants: [{ action: "bill_pay", grant_id: "grant-1", account_ids: ["ACC-001"] }] }, usage: [], status: "REVOKED",
    }] };
    render(<BankHome overview={overview} notices={[{ id: "6", type: "escalation.created", at: now, requestId: pending.request_id }]}
      disabled={false} mutate={vi.fn()} />);
    expect(screen.getByText("No payments waiting for approval.")).toBeInTheDocument();
    expect(screen.queryByText("A payment is waiting for your approval.")).not.toBeInTheDocument();
    expect(screen.getByText("Cancelled · mandate revoked")).toBeInTheDocument();
    expect(approvalStatus(pending, overview, now)).toBe("REVOKED");
  });
});
