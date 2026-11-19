import {
  isGatewayTimestamp, isMandateAction, isMoneyAction, isNonnegativeInteger, mandatePath,
  type CreateMandateBody, type GrantInput, type MandateListItem, type MandatePage, type MandateTermsInput,
} from "../api/mandateWire";
import { isRecord, type CustomerBootstrap } from "../api/shellWire";

export type MockCustomer = { id: string; secret: string; bootstrap: CustomerBootstrap };
type MockReply = {
  respond: (payload: unknown, status?: number) => Response;
  reject: (status: number, code: string, message: string) => Response;
};
type StoredMandate = MandateListItem & {
  terms: MandateTermsInput & { mandate_id: string };
};

const revokePattern = /^\/v1\/customer\/mandates\/([^/]+)\/revoke$/;
export function isMandateRoute(path: string) {
  return path === mandatePath || revokePattern.test(path);
}

export function createMandateFixtures(now: () => number) {
  const owned = new Map<string, StoredMandate[]>();
  let nextId = 0;

  function withExpiry(item: MandateListItem): MandateListItem {
    return item.status === "ACTIVE" && Date.parse(item.terms.valid_until) <= now()
      ? { ...item, status: "EXPIRED" } : item;
  }

  return function handleMandate(url: URL, init: RequestInit, customer: MockCustomer, { respond, reject }: MockReply) {
    const records = owned.get(customer.id) ?? [];
    const invalid = (message: string) => reject(400, "E_REQ_INVALID", message);
    const notFound = () => reject(404, "E_REQ_INVALID", "Mandate not found.");

    if (url.pathname === mandatePath && init.method === "GET") {
      const limit = Number(url.searchParams.get("limit") ?? "50");
      const cursor = url.searchParams.get("cursor");
      const start = cursor === null ? 0 : records.findIndex((item) => cursorFor(item) === cursor) + 1;
      if (!Number.isInteger(limit) || limit < 1 || limit > 100 || (cursor !== null && start === 0)) {
        return invalid("Invalid mandate page parameters.");
      }
      const rows = records.slice(start, start + limit);
      return respond({
        items: rows.map(withExpiry),
        next_cursor: start + rows.length < records.length ? cursorFor(rows[rows.length - 1]) : null,
      } satisfies MandatePage);
    }

    if (url.pathname === mandatePath && init.method === "POST") {
      const body = parseBody(init.body);
      if (!isCreateBody(body) || !allowedGrantTerms(body, customer.bootstrap)
        || Date.parse(body.valid_from) >= Date.parse(body.valid_until)
        || Date.parse(body.valid_until) <= now()) return invalid("Check mandate scope, limits, and expiry.");
      if (body.pin !== customer.secret) return reject(403, "E_ESC_BAD_PIN", "The confirmation PIN is incorrect.");

      const mandateId = `mnd_mock_${++nextId}`;
      const { pin: _pin, ...terms } = body;
      const item: StoredMandate = {
        terms: { ...terms, mandate_id: mandateId },
        status: "ACTIVE",
        usage: terms.grants.map((grant) => ({
          grant_id: grant.grant_id,
          committed_paise: 0, reserved_paise: 0, remaining_paise: grant.max_total_paise ?? null,
          committed_count: 0, reserved_count: 0, remaining_count: grant.max_count,
        })),
      };
      owned.set(customer.id, [item, ...records]);
      return respond({ mandate: { terms: item.terms, status: item.status } }, 201);
    }

    const revoke = revokePattern.exec(url.pathname);
    if (revoke && init.method === "POST") {
      const item = records.find((row) => encodeURIComponent(row.terms.mandate_id) === revoke[1]);
      if (!item) return notFound();
      item.status = "REVOKED";
      return respond({ status: item.status });
    }

    return invalid("This request is outside the mock mandates API.");
  };
}

function cursorFor(item: MandateListItem): string {
  return `after:${item.terms.mandate_id}`;
}

function parseBody(body: RequestInit["body"]): unknown {
  try {
    return typeof body === "string" ? JSON.parse(body) : null;
  } catch {
    return null;
  }
}

function onlyKeys(value: Record<string, unknown>, allowed: string[]) {
  return Object.keys(value).every((key) => allowed.includes(key));
}

function isPositiveInteger(value: unknown): value is number {
  return isNonnegativeInteger(value) && value > 0;
}

function isIdList(value: unknown): value is string[] {
  return Array.isArray(value) && value.length > 0
    && value.every((id: unknown) => typeof id === "string" && id !== "");
}

function isTimeWindow(value: unknown) {
  const clockTime = (time: unknown): time is string => typeof time === "string" && /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(time);
  return isRecord(value) && onlyKeys(value, ["timezone", "weekdays", "start_local", "end_local"])
    && value.timezone === "Asia/Kolkata" && Array.isArray(value.weekdays) && value.weekdays.length > 0
    && value.weekdays.every((day: unknown) => isPositiveInteger(day) && day <= 7)
    && clockTime(value.start_local) && clockTime(value.end_local) && value.start_local < value.end_local;
}

function isGrant(value: unknown): value is GrantInput {
  if (!isRecord(value) || !onlyKeys(value, [
    "grant_id", "action", "account_ids", "payee_ids", "currency", "max_per_txn_paise",
    "max_total_paise", "max_count", "window", "time_window",
  ]) || typeof value.grant_id !== "string" || !value.grant_id
    || !isMandateAction(value.action) || !isIdList(value.account_ids) || value.window !== "mandate"
    || !isPositiveInteger(value.max_count) || (value.time_window !== undefined && !isTimeWindow(value.time_window))) {
    return false;
  }
  return isMoneyAction(value.action)
    ? value.currency === "INR" && isIdList(value.payee_ids)
      && isPositiveInteger(value.max_per_txn_paise) && isPositiveInteger(value.max_total_paise)
      && value.max_per_txn_paise <= value.max_total_paise
    : value.currency === undefined && value.payee_ids === undefined
      && value.max_per_txn_paise === undefined && value.max_total_paise === undefined;
}

function isCreateBody(value: unknown): value is CreateMandateBody {
  return isRecord(value) && onlyKeys(value, ["agent_id", "valid_from", "valid_until", "grants", "step_up", "risk_profile", "pin"])
    && typeof value.agent_id === "string" && typeof value.pin === "string" && value.pin !== ""
    && isGatewayTimestamp(value.valid_from) && isGatewayTimestamp(value.valid_until) && value.risk_profile === "balanced"
    && Array.isArray(value.grants) && value.grants.length > 0 && value.grants.every(isGrant)
    && isRecord(value.step_up)
    && onlyKeys(value.step_up, ["amount_above_paise", "new_payee_requires_approval", "always_ask_actions"])
    && (value.step_up.amount_above_paise === undefined || isNonnegativeInteger(value.step_up.amount_above_paise))
    && typeof value.step_up.new_payee_requires_approval === "boolean"
    && Array.isArray(value.step_up.always_ask_actions) && value.step_up.always_ask_actions.every(isMandateAction);
}

function allowedGrantTerms(body: CreateMandateBody, bootstrap: CustomerBootstrap) {
  const agent = bootstrap.agents.find((card) => card.agent_id === body.agent_id);
  const actions = new Set(body.grants.map((grant) => grant.action));
  const grantIds = new Set(body.grants.map((grant) => grant.grant_id));
  return actions.size === body.grants.length && grantIds.size === body.grants.length
    && body.step_up.always_ask_actions.every((action) => actions.has(action))
    && body.grants.every((grant) => agent?.max_scopes?.includes(grant.action)
      && grant.account_ids.every((id) => bootstrap.accounts.some((account) => account.account_id === id))
      && (grant.payee_ids ?? []).every((id) => bootstrap.payees.some((payee) => payee.payee_id === id)));
}
