import { formatKolkataTimestamp, type ValidationResult } from "../../../formatting";
import type { BankCatalog } from "./bankWire";

export const exampleMandate = "Let BillPal pay my electricity bill, up to ₹5,000 per payment, ₹10,000 total, 5 payments, valid 7 days, ask me above ₹4,000.";

export type BankDraft = {
  agent: string; payees: string[]; category: string; perTxn: string; lifetime: string;
  count: string; expiry: string; threshold: string; newPayee: boolean;
};

export const blankDraft: BankDraft = {
  agent: "", payees: [], category: "", perTxn: "", lifetime: "", count: "", expiry: "", threshold: "", newPayee: false,
};

export function parseMandateLanguage(text: string, catalog: BankCatalog, now = Date.now()): ValidationResult<BankDraft> {
  const rupees = "(?:₹|INR |Rs\\.? )?([\\d,]+(?:\\.\\d{1,2})?)";
  const pattern = new RegExp(
    `^Let (.+?) pay (?:my )?(.+?),\\s*up to ${rupees} per payment,\\s*${rupees} total,\\s*`
    + `(\\d+) payments?,\\s*valid (\\d+) days?,\\s*ask me above ${rupees}\\.?$`, "i",
  );
  const parts = pattern.exec(text.trim().replace(/\s+/g, " "));
  if (!parts) return { ok: false, error: "Use the example format, or enter the terms in the fields below." };
  const [, agentName, bill, perTxn, lifetime, count, days, threshold] = parts;
  const name = agentName.toLowerCase();
  const matches = catalog.agents.filter((agent) => agent.max_scopes?.includes("bill_pay")
    && [agent.name, agent.name.replace(/ agent$/i, ""), agent.agent_id].some((item) => item.toLowerCase() === name));
  const payees = catalog.payees.filter((payee) => payee.display_name.toLowerCase() === bill.toLowerCase()
    || (bill.toLowerCase() === "electricity bill" && payee.category === "utilities.electricity"));
  if (matches.length !== 1 || payees.length !== 1) {
    return { ok: false, error: "Select one available agent and the intended payee in the fields below." };
  }
  const duration = Number(days);
  const expiry = new Date(now + duration * 86_400_000);
  const money = /^(?:\d+|\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})+,\d{3})(?:\.\d{1,2})?$/;
  if (!Number.isSafeInteger(duration) || duration < 1 || !Number.isFinite(expiry.getTime()) || expiry.getUTCFullYear() > 9999
    || ![perTxn, lifetime, threshold].every((amount) => money.test(amount))) {
    return { ok: false, error: "Check the currency amounts and number of days." };
  }
  return {
    ok: true,
    value: {
      agent: matches[0].agent_id, payees: [payees[0].payee_id], category: payees[0].category ?? "",
      perTxn: perTxn.replaceAll(",", ""), lifetime: lifetime.replaceAll(",", ""),
      count, expiry: formatKolkataTimestamp(expiry.toISOString()).slice(0, 19).replace(" ", "T"),
      threshold: threshold.replaceAll(",", ""), newPayee: false,
    },
  };
}
