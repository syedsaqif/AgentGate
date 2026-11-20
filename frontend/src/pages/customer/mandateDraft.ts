import {
  isMoneyAction, type GrantInput, type MandateAction, type MandateTermsInput, type TimeWindow,
} from "../../api/mandateWire";
import type { CustomerBootstrap } from "../../api/shellWire";
import { parseKolkataExpiry, parseMandateCapPaise, parseRupeePaise, type ValidationResult } from "../../formatting";

export const actionLabels: Record<MandateAction, string> = {
  bill_pay: "Pay bills",
  transfer: "Transfer funds",
  balance_read: "Read balance",
  transactions_read: "Read transactions",
};

export const weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export function maskedAccount(account: CustomerBootstrap["accounts"][number]): string {
  return account.masked_label.replaceAll(account.account_id, "••••");
}

export function readMandateDraft(
  fields: FormData, bootstrap: CustomerBootstrap, actions: MandateAction[],
): ValidationResult<MandateTermsInput> {
  const fail = (error: string) => ({ ok: false, error } as const);
  const agent = bootstrap.agents.find((card) => card.agent_id === fields.get("agent"));
  const account = bootstrap.accounts.find((card) => card.account_id === fields.get("account"));
  if (!account || !agent) return fail("Select an account and an agent.");
  if (!actions.length || actions.some((action) => !agent.max_scopes?.includes(action))) {
    return fail("Select at least one action allowed for this agent.");
  }

  const expiry = parseKolkataExpiry(String(fields.get("expiry") ?? ""));
  if (!expiry.ok) return expiry;
  const startTime = new Date();
  if (Date.parse(expiry.value) <= startTime.getTime()) return fail("Enter an expiry in the future.");

  const countText = String(fields.get("count") ?? "").trim();
  const count = Number(countText);
  if (!/^\d+$/.test(countText) || !Number.isSafeInteger(count) || count < 1) {
    return fail("Enter a positive whole number for the count limit.");
  }

  let moneyTerms: Pick<GrantInput, "payee_ids" | "currency" | "max_per_txn_paise" | "max_total_paise"> = {};
  const hasMoneyAction = actions.some(isMoneyAction);
  if (hasMoneyAction) {
    const payees = bootstrap.payees.filter(({ payee_id }) => fields.getAll("payee").includes(payee_id));
    if (!payees.length) return fail("Select at least one allowed payee.");
    const perTxn = parseMandateCapPaise(String(fields.get("perTxn") ?? ""));
    if (!perTxn.ok) return fail(`Per-transaction cap. ${perTxn.error}`);
    const lifetime = parseMandateCapPaise(String(fields.get("lifetime") ?? ""));
    if (!lifetime.ok) return fail(`Mandate-lifetime cap. ${lifetime.error}`);
    if (perTxn.value > lifetime.value) return fail("The per-transaction cap must fit within the lifetime cap.");
    moneyTerms = {
      payee_ids: payees.map(({ payee_id }) => payee_id), currency: "INR",
      max_per_txn_paise: perTxn.value, max_total_paise: lifetime.value,
    };
  }

  const days = weekdays.flatMap((_, index) => fields.getAll("weekday").includes(String(index + 1)) ? [index + 1] : []);
  const start = String(fields.get("start") ?? "").trim();
  const end = String(fields.get("end") ?? "").trim();
  let timeWindow: TimeWindow | undefined;
  if (days.length || start || end) {
    const clock = /^(?:[01]\d|2[0-3]):[0-5]\d$/;
    let effectiveStart = start;
    let effectiveEnd = end;
    if (start && end) {
      if (!clock.test(start) || !clock.test(end) || start >= end) {
        return fail("Enter an IST time window where window starts is before window ends on the same day.");
      }
    } else if (start && !end) {
      if (!clock.test(start)) {
        return fail("Enter a valid IST window start time (HH:MM).");
      }
      effectiveEnd = "23:59";
    } else if (!start && end) {
      if (!clock.test(end)) {
        return fail("Enter a valid IST window end time (HH:MM).");
      }
      effectiveStart = "00:00";
    } else {
      // Both start and end omitted: optional time window covering the entire selected days
      effectiveStart = "00:00";
      effectiveEnd = "23:59";
    }
    const effectiveDays = days.length ? days : [1, 2, 3, 4, 5, 6, 7];
    timeWindow = { timezone: "Asia/Kolkata", weekdays: effectiveDays, start_local: effectiveStart, end_local: effectiveEnd };
  }

  const stepUp: MandateTermsInput["step_up"] = {
    new_payee_requires_approval: fields.has("newPayee"),
    always_ask_actions: actions.filter((action) => fields.getAll("alwaysAsk").includes(action)),
  };
  const threshold = String(fields.get("threshold") ?? "").trim();
  if (hasMoneyAction && threshold) {
    const amount = parseRupeePaise(threshold);
    if (!amount.ok) return fail(`Approval threshold. ${amount.error}`);
    stepUp.amount_above_paise = amount.value;
  }

  return {
    ok: true,
    value: {
      agent_id: agent.agent_id, valid_from: startTime.toISOString(), valid_until: expiry.value,
      risk_profile: "balanced", step_up: stepUp,
      grants: actions.map((action) => ({
        grant_id: `grant_${action}`, action, account_ids: [account.account_id], max_count: count, window: "mandate",
        time_window: timeWindow,
        ...(isMoneyAction(action) ? moneyTerms : {}),
      })),
    },
  };
}
