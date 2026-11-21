import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import { disposableStack } from "./stack.mjs";

const archive = new URL("../../src/pages/demo/stage/recordings/", import.meta.url);
const fields = (value, keys) => Object.fromEntries(keys.filter(k => value?.[k] !== undefined).map(k => [k, value[k]]));

// Each fixture is captured on an isolated provider DB. Store only fields rendered by the stage.
function snapshot(raw) {
  return {
    ...fields(raw, ["customer_id", "account_id", "balance_paise", "currency"]),
    transactions: raw.transactions.map(x => fields(x, ["entry_id", "account_id", "entry_type", "amount_paise", "balance_after_paise", "created_at"])),
    accounts: raw.accounts.map(x => fields(x, ["account_id", "masked_label"])),
    agents: raw.agents.map(x => fields(x, ["agent_id", "name", "vendor"])),
    payees: raw.payees.map(x => fields(x, ["payee_id", "display_name"])),
    mandates: [], receipts: raw.receipts.map(x => fields(x, ["request_id", "amount_paise", "from_account", "payee_id"])),
    escalations: raw.escalations.map(x => ({
      ...fields(x, ["request_id", "escalation_id", "state", "expires_at"]),
      trusted_summary: {
        amount: fields(x.trusted_summary.amount, ["currency", "amount_paise"]),
        from_account: fields(x.trusted_summary.from_account, ["account_id","masked_label"]),
        payee: x.trusted_summary.payee && fields(x.trusted_summary.payee, ["payee_id","display_name"]),
      },
    })),
  };
}

function projectEvent(event, customer) {
  if (customer) {
    assert(!["gate.step", "policy.decision"].includes(event.event_type));
    assert.equal(event.customer_id, "cust_001");
    for (const field of ["risk","internal_trace","raw_signals"]) assert(!(field in event.data));
  }
  if (!["gate.step","bank.executed","bank.unknown","bank.failed","escalation.created","escalation.resolved","agent.suspended","policy.decision","action.denied"].includes(event.event_type)) return;
  const raw = event.data;
  const data = fields(raw, ["state","status","reason_code","decision","expires_at","amount_paise","from_account","payee_id","escalation_id"]);
  if (!customer) {
    Object.assign(data, fields(raw, ["stage","check","seq","at","reason_codes"]));
    if (raw.detail) data.detail = fields(raw.detail, ["new","decisive","grant_index","elapsed_ns","requested_paise","threshold_paise","limit_paise","projected_paise","projected_count","limit","score","severity","weight","recommended","fallback","outcome","verdict","band","decision","reason_code"]);
    if (raw.request) data.request = fields(raw.request, ["agent_name","agent_id","action_type","amount_paise","payee_id","category","risk_profile","fingerprint"]);
    if (raw.audit) data.audit = raw.audit.map(x => fields(x, ["seq","entry_hash","prev_hash","event_type"]));
    if (raw.audit_ref) data.audit_ref = fields(raw.audit_ref, ["seq","entry_hash"]);
    if (raw.risk) data.risk = fields(raw.risk, ["score","level","model_version"]);
  }
  return { ...fields(event, ["event_type","event_id","request_id","customer_id","agent_id","occurred_at"]), data };
}

// A keepalive arrives after the gateway has drained the replay buffer.
export async function frames(url, headers, untilIdle) {
  const response = await fetch(url, { headers, signal: AbortSignal.timeout(20000) });
  assert.equal(response.status, 200);
  let buffer = "";
  const output = [];
  for await (const chunk of response.body.pipeThrough(new TextDecoderStream())) {
    buffer += chunk.replaceAll("\r", "");
    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      if (block.startsWith(":") && untilIdle) return output;
      const line = block.split("\n").find(x => x.startsWith("data:"));
      const id = /^id: (\d+)$/m.exec(block)?.[1];
      if (id && line) output.push({ id, value: JSON.parse(line.slice(5)) });
    }
  }
  return output;
}

export async function capture(stack, id, prerequisite) {
  const base = stack.demo + "/demo";
  let cookie;
  async function json(path, body) {
    const response = await fetch(base + path, { method: body ? "POST" : "GET",
      headers: { "Content-Type": "application/json", ...(cookie && { Cookie: cookie }) },
      ...(body && {body: JSON.stringify(body)}), signal: AbortSignal.timeout(20000) });
    assert(response.ok, path + " returned " + response.status);
    if (response.headers.has("set-cookie")) cookie = response.headers.get("set-cookie").split(";")[0];
    return response.json();
  }
  async function run(scenario_id) {
    const {run_id} = await json("/agent/run", {scenario_id});
    const observed = await frames(base + "/agent/stream?run_id=" + run_id, {}, false);
    assert.equal(observed.at(-1)?.value.step, "completed");
    return {run_id, steps: observed.map(({id,value}) => ({ id, data: {
      ...fields(value, ["step","attempt","tool","action","request_id","trace_request_id","status","verdict","reason_code","message","cached","balance_delta_paise","agent_status","agent_suspended_during_run"]),
      ...(value.args && {args: fields(value.args, ["from_account","amount_paise","payee_id","category"])}),
    } }))};
  }
  const card = (await json("/scenarios")).items.find(x => x.id === id);
  assert(card);
  if (card.source !== "live_supported") return {card};
  await json("/bank/login", {customer_id:"cust_001",pin:stack.customerPin});
  let since = 0;
  if (prerequisite === "revoke") {
    const reset = await json("/reset", {});
    await json("/bank/login", {customer_id:"cust_001",pin:stack.customerPin});
    assert(reset.created_mandate_id);
    await json("/bank/mandates/" + reset.created_mandate_id + "/revoke", {});
  } else if (prerequisite) {
    await run(prerequisite);
  }
  if (prerequisite) {
    const prior = await frames(base + "/gate/stream", {"Last-Event-ID":"0"}, true);
    since = Number(prior.at(-1)?.id || 0);
  }
  const bank = async () => ({ at: new Date().toISOString(), overview: snapshot(await json("/bank/overview")) });
  const opening = await bank();
  const {run_id, steps} = await run(id);
  const phases = [];
  async function phase() {
    const [gate, customer, observed] = await Promise.all([
      frames(base + "/gate/stream", {"Last-Event-ID":String(since)}, true),
      frames(base + "/bank/stream", {Cookie:cookie,"Last-Event-ID":String(since)}, true), bank(),
    ]);
    const project = (entries, customer) => entries.map(x => projectEvent(x.value, customer)).filter(Boolean);
    phases.push({ ...observed, gate: project(gate, false), notices: project(customer, true) });
    since = Math.max(since, ...gate.map(x=>Number(x.id)), ...customer.map(x=>Number(x.id)));
  }
  await phase();
  const approvals = phases[0].overview.escalations.filter(x => x.state === "PENDING");
  if (approvals.length) {
    for (const item of approvals.reverse()) {
      try { await json("/bank/approvals/" + item.escalation_id,
      id === "P5" ? {decision:"REJECT"} : {decision:"APPROVE",pin:stack.customerPin}); }
      catch (error) {
        if (id !== "L2" || !String(error).includes("returned 422")) throw error;
        const latest = (await json("/bank/overview")).escalations.find(x => x.escalation_id === item.escalation_id);
        if (latest?.state === "PENDING") await json("/bank/approvals/" + item.escalation_id, {decision:"REJECT"});
      }
      await phase();
    }
  }
  return {card, run_id, steps, opening, phases, prerequisite};
}

async function main() {
  await mkdir(archive, {recursive:true});
  // Discover cards from the service, including read-only fixtures.
  let ids = process.argv.slice(2);
  for (let index = 0; index < (ids.length || 1); index++) {
    const stack = await disposableStack();
    try {
      await stack.seed(); await stack.ready();
      await stack.startDemo("http://127.0.0.1:5173", "https://example.invalid/verifier");
      if (!ids.length) ids = (await (await fetch(stack.demo + "/demo/scenarios")).json()).items.map(x=>x.id).concat("N2-after-N1");
      const key = ids[index];
      const prereq = { P6:"P1", L6:"I1", L7:"revoke", "N2-after-N1":"P1" }[key];
      const record = await capture(stack, key === "N2-after-N1" ? "N2" : key, prereq);
      await writeFile(new URL(key + ".json", archive), JSON.stringify(record) + "\n");
      console.log(key, record.steps?.at(-1)?.data.verdict ?? record.card.source,
        record.phases?.at(-1)?.overview.balance_paise ?? "", record.phases?.flatMap(x=>x.gate).length ?? "");
    } finally { await stack.close(); }
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main();

