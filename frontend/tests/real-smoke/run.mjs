import { deepStrictEqual } from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { chromium } from "playwright";
import { createServer } from "vite";
import { checkReceiptQr, downloadAndCheck, verifierHost } from "./dispute.mjs";
import { disposableStack, frontend, requireResult, SmokeFailure } from "./stack.mjs";

// This is an explicit integration gate. Never echo browser errors, bodies, headers or secrets.
async function step(name, action, dependency = true) {
  if (!dependency) {
    console.log(`BLOCKED  ${name} (prerequisite unavailable)`);
    process.exitCode = 1;
    return;
  }
  try {
    const result = await action();
    console.log(`PASS     ${name}`);
    return result ?? true;
  } catch (error) {
    process.exitCode = 1;
    console.log(`FAIL     ${name}: ${error instanceof SmokeFailure ? error.message : "expected operation did not complete"}`);
  }
}

function publicId(value) {
  requireResult(typeof value === "string" && /^[\w.-]{1,128}$/.test(value), "invalid or missing provider identifier");
  return value;
}

async function exchange(page, gateway, path, method, act, allowed) {
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url() === gateway + path && r.method() === method),
    act(),
  ]);
  const res = await req.response();
  if (!res) {
    const alert = page.getByRole("alert").filter({ hasText: "UI_GATEWAY_UNREACHABLE" });
    const unreachable = await alert.waitFor({ timeout: 1500 }).then(() => true, () => false);
    throw new SmokeFailure(`${method} ${path}: no HTTP response${unreachable ? " (UI_GATEWAY_UNREACHABLE)" : ""}`);
  }
  requireResult(allowed.includes(res.status()), `${method} ${path}: HTTP ${res.status()}`);
  return res;
}
const button = (where, name) => where.getByRole("button", { name, exact: true });
async function login(page, role, secret, id, gateway) {
  await page.getByLabel(role === "customer" ? "Customer ID" : "Operator ID").fill(id);
  await page.getByLabel(role === "customer" ? "PIN" : "Password", { exact: true }).fill(secret);
  await exchange(page, gateway, `/v1/${role}/login`, "POST", () => button(page, "Sign in").click(), [200]);
  await page.getByText(role === "customer" ? "Signed in as Customer" : "Signed in as Ops", { exact: true }).waitFor();
}

async function mandateForm(page, gateway, pin) {
  await page.locator('select[name="account"]').selectOption("ACC-001");
  await page.locator('select[name="agent"]').selectOption("agt_billpal");
  await page.getByLabel("Pay bills", { exact: true }).check();
  await page.locator('input[name="payee"][value="BILL-ELEC"]').check();
  await page.getByLabel("Per-transaction cap (INR)").fill("5000");
  await page.getByLabel("Mandate-lifetime cap (INR)").fill("10000");
  await page.getByLabel("Count limit per grant").fill("5");
  const tomorrow = new Date(Date.now() + 86400000).toISOString().slice(0, 10);
  await page.getByLabel("Expiry (IST)").fill(tomorrow + "T23:59");
  await page.getByText("Optional time and approval terms").click();
  await page.getByLabel("Approval threshold (INR)").fill("4000");
  await button(page, "Review mandate").click();
  await page.getByLabel("Confirmation PIN").fill(pin);
  const res = await exchange(page, gateway, "/v1/customer/mandates", "POST", () => button(page, "Confirm mandate").click(), [201]);
  // A's tests/a/test_mandates.py documents the create envelope.
  return publicId((await res.json()).mandate?.terms?.mandate_id);
}

async function run() {
  const stack = await disposableStack();
  let browser, vite, verifier;
  try {
    await step("gateway seed", () => stack.seed());
    const sdk = await step("B SDK signed payment command", () => stack.checkSdk());
    const ready = await step("gateway GET /health/ready", async () => {
      const r = await stack.ready();
      const names = ["identity", "human_auth", "audit", "risk", "escalation", "bank", "policy", "mandates"];
      const modes = names.map((key) => [key, ["real", "fake", "unconfigured"].includes(r.providers?.[key]) ? r.providers[key] : "unknown"]);
      console.log("Readiness", ["real", "test", "scripted"].includes(r.profile) ? r.profile : "unknown", Object.fromEntries(modes));
      requireResult(r.status === "ready" && r.profile === "real" && r.contract_version === "1.0.0"
        && modes.every(([, mode]) => mode === "real"), "gateway did not report ready real providers for IC0");
    });
    const bank = await step("isolated simulated bank startup", async () => {
      requireResult((await stack.bank()).info.title === "AgentGate Mock Bank", "unexpected bank application");
    }, !!ready);
    const opening = await step("fresh bank balance and no existing debits", () => {
      const snapshot = stack.bankSnapshot();
      requireResult(snapshot.balance === 12500000 && snapshot.debits.length === 0, "bank state is not fresh");
      return snapshot;
    }, !!bank);
    process.env.VITE_GATEWAY_ORIGIN = stack.gateway;
    verifier = await verifierHost(await stack.verifierTls());
    process.env.VITE_DISPUTE_VERIFIER_URL = verifier.url;
    process.env.VITE_DEMO_BANK_ORIGIN = stack.demo;
    vite = await createServer({ root: frontend, mode: "real", logLevel: "silent", server: { host: "127.0.0.1", port: 0, open: false } });
    await vite.listen();
    browser = await chromium.launch();
    const origin = `http://127.0.0.1:${vite.httpServer.address().port}`;
    let unexpectedDestination = false;
    async function workspace(role) {
      const context = await browser.newContext();
      context.on("request", (request) => {
        if (![origin, stack.gateway].includes(new URL(request.url()).origin)) unexpectedDestination = true;
      });
      context.setDefaultTimeout(7000);
      const page = await context.newPage();
      await page.goto(origin + "/" + role);
      await button(page, "Sign in").waitFor();
      requireResult(await page.locator(".mock-mode").count() === 0, "unexpected fixture mode");
      return page;
    }
    const customer = await step("Chromium /customer", () => workspace("customer"));
    const pin = stack.customerPin;
    const signedIn = await step("Customer login", () => login(customer, "customer", pin, "cust_001", stack.gateway), !!(customer && ready));
    const bootstrap = await step("Customer bootstrap", () => customer.getByRole("heading", { name: "Your accounts" }).waitFor(), !!signedIn);
    const ops = await step("Chromium /ops in separate context", () => workspace("ops"));
    const operator = await step("Ops login and authenticated stream", async () => {
      await login(ops, "ops", stack.opsPassword, stack.opsId, stack.gateway);
      await ops.getByText("Live stream connected.", { exact: true }).waitFor();
      await ops.getByText("agt_billpal", { exact: true }).waitFor();
    }, !!(ops && ready));
    const mandate = await step("create Customer mandate", () => mandateForm(customer, stack.gateway, pin), !!(bootstrap && ready));
    if (mandate) console.log("Mandate", mandate);
    const payment = await step("SDK signed S9 pending payment", async () => {
      const response = await stack.pay(mandate);
      return { request: publicId(response.request_id), escalation: publicId(response.escalation_id) };
    }, !!(mandate && opening && sdk));
    if (payment) console.log("Request", payment.request, "escalation", payment.escalation);
    await step("same request in the Ops live decision feed", async () => {
      const feed = ops.getByRole("region", { name: "Live decision feed" });
      const item = feed.getByRole("listitem").filter({ has: ops.getByText(payment.request, { exact: true }) });
      await item.getByText("ESCALATE", { exact: true }).waitFor();
      requireResult(/^\d+$/.test(await item.locator("dl div").filter({ hasText: "Risk score" }).locator("dd").innerText()),
        "Ops decision omitted its risk score");
    }, !!(operator && payment));
    const card = await step("matching pending inbox item", async () => {
      await button(customer, "Refresh inbox").click();
      const item = customer.getByRole("article").filter({ has: customer.getByText(payment.request, { exact: true }) });
      await item.getByText("₹4,800.00", { exact: true }).waitFor();
      await item.getByText("PENDING", { exact: true }).waitFor();
      return item;
    }, !!payment);
    const decision = await step("approve once in Customer browser", async () => {
      // The app opens its authenticated stream on workspace mount, before the decision POST.
      await customer.getByText("Customer receipt stream connected.", { exact: true }).waitFor();
      await card.getByLabel("Approval PIN").fill(pin);
      const path = `/v1/customer/escalations/${payment.escalation}/decision`;
      let posts = 0;
      customer.on("request", (req) => { if (req.url() === stack.gateway + path && req.method() === "POST") posts++; });
      const response = await exchange(customer, stack.gateway, path, "POST", () => button(card, "Approve").click(), [200, 202]);
      console.log(`Decision HTTP ${response.status()}`);
      return { response, posts: () => posts };
    }, !!card);
    const receipt = await step("Customer-visible execution and matching receipt", async () => {
      const row = customer.locator(".approval-result").filter({ has: customer.getByText(payment.request, { exact: true }) });
      const accepted = decision.response.status() === 202;
      if (accepted) await row.getByText("Customer stream confirmed execution", { exact: false }).waitFor();
      await button(row, "Copy receipt").waitFor();
      const text = await row.locator(".approval-receipt pre").innerText();
      const match = /^audit_ref\.seq: ([1-9]\d*)\naudit_ref\.entry_hash: ([a-f0-9]{64})$/.exec(text);
      requireResult(match && decision.posts() === 1, "missing receipt or repeated browser decision");
      if (!accepted) {
        const response = await decision.response.json();
        requireResult(response.escalation_id === payment.escalation && response.decision === "APPROVE"
          && response.request_id === payment.request && response.result?.status === "EXECUTED"
          && response.result.audit_ref?.seq === Number(match[1]) && response.result.audit_ref.entry_hash === match[2],
        "Customer receipt did not match the gateway decision");
        await row.getByText("Customer receipt stream confirmed this receipt.", { exact: true }).waitFor();
      }
      console.log(`Customer receipt via ${accepted ? "202 + Customer SSE" : "decision response and Customer SSE"}: seq ${match[1]}, hash ${match[2]}`);
      return { seq: Number(match[1]), entry_hash: match[2] };
    }, !!decision);
    const revoked = await step("revoke same mandate and refetch REVOKED", async () => {
      const row = customer.getByRole("article").filter({ has: customer.getByText(mandate, { exact: true }) });
      const [page] = await Promise.all([
        customer.waitForResponse((r) => r.url() === stack.gateway + "/v1/customer/mandates" && r.request().method() === "GET"),
        exchange(customer, stack.gateway, `/v1/customer/mandates/${mandate}/revoke`, "POST", () => button(row, "Revoke mandate").click(), [200]),
      ]);
      const item = (await page.json()).items.find((item) => item.terms.mandate_id === mandate);
      requireResult(item?.status === "REVOKED", "refetch omitted revoked mandate");
      await row.getByText(/^REVOKED/).waitFor();
      return item;
    }, !!mandate);
    await step("one committed use for 480000 paise", () => {
      requireResult(revoked.usage.length === 1 && revoked.usage[0].committed_count === 1
        && revoked.usage[0].committed_paise === 480000 && revoked.usage[0].reserved_count === 0
        && revoked.usage[0].reserved_paise === 0 && decision.posts() === 1, "mandate usage or decision count disagreed");
      console.log("Decision POSTs 1; committed 1 use / 480000 paise; reserved 0 uses / 0 paise; mandate REVOKED");
    }, !!(receipt && revoked));
    const report = await step("Ops audit verification covers the Customer receipt", async () => {
      const response = await exchange(ops, stack.gateway, "/v1/audit/verify", "GET", () => button(ops, "Verify audit chain").click(), [200]);
      const report = await response.json();
      requireResult(report.valid && report.first_failure === null && report.entries_checked === report.head_seq
        && report.head_seq >= receipt.seq, "verification did not cover the Customer receipt");
      await ops.getByText("Verification passed.", { exact: true }).waitFor();
      console.log(`Audit entries ${report.entries_checked}; retained checkpoint through ${report.anchored_through_seq}`);
      return report;
    }, !!(operator && receipt && revoked));
    const bundle = await step("Ops historical evidence matches mandate, approval, receipt and bank operation", async () => {
      await ops.getByLabel("Request ID", { exact: true }).fill(payment.request);
      await button(ops, "Apply filters").click();
      const evidence = ops.getByRole("button", { name: `View evidence for ${payment.request} from sequence`, exact: false }).first();
      await exchange(ops, stack.gateway, `/v1/audit/evidence/${payment.request}`, "GET", () => evidence.click(), [200]);
      const region = ops.getByRole("region", { name: `Evidence for ${payment.request}` });
      const evidenceText = await region.locator("details pre").innerText();
      const bundle = JSON.parse(evidenceText, (key, value) => {
        requireResult(!/^(?:authorization|access_token|session_token|pin|password|private_key)$/i.test(key),
          "evidence included a confidential field");
        return value;
      });
      requireResult(bundle.request_id === payment.request && bundle.action.request_id === payment.request
        && bundle.action.mandate_id === mandate && bundle.mandate.terms.mandate_id === mandate
        && bundle.mandate.status === "ACTIVE" && bundle.action.mandate_hash === bundle.mandate.mandate_hash
        && bundle.action.params.amount_paise === 480000 && bundle.action.params.currency === "INR"
        && bundle.action.params.from_account === "ACC-001" && bundle.action.params.payee_id === "BILL-ELEC"
        && bundle.action.params.reference === stack.reference
        && bundle.bank_outcome?.status === "EXECUTED" && !!bundle.bank_outcome.bank_operation_id
        && typeof bundle.bank_outcome.bank_idempotency_key === "string",
      "historical mandate, action amount, or bank execution did not agree");
      const approvals = bundle.approval_history.filter((item) => item.event_type === "escalation.approved");
      requireResult(approvals.length === 1 && approvals[0].safe_payload.escalation_id === payment.escalation
        && approvals[0].safe_payload.decision === "APPROVE", "Ops evidence disagreed with the Customer decision");
      requireResult(bundle.audit_references.some((ref) =>
        ref.seq === receipt.seq && ref.entry_hash === receipt.entry_hash), "Ops evidence lacked the Customer receipt");
      requireResult(bundle.verification.valid && bundle.verification.head_seq === report.head_seq
        && bundle.verification.head_hash === report.head_hash, "Ops evidence disagreed with the verification report");
      const [download] = await Promise.all([
        ops.waitForEvent("download"), region.getByRole("link", { name: "Download evidence JSON" }).click(),
      ]);
      requireResult(download.suggestedFilename() === `audit-evidence-${payment.request}.json`, "unexpected evidence filename");
      deepStrictEqual(JSON.parse(await readFile(await download.path(), "utf8")), bundle);
      console.log(`Verified evidence download for ${payment.request}; approval audit seq ${approvals[0].seq}`);
      return bundle;
    }, !!(operator && report && payment));
    await step("exactly one persisted bank debit of 480000 paise for the approved operation", () => {
      const outcome = bundle.bank_outcome;
      const { balance, debits, operation } = stack.bankSnapshot(outcome);
      requireResult(debits.length === 1 && debits[0].reference === bundle.action.params.reference
        && debits[0].amount_paise === 480000 && debits[0].balance_after_paise === balance
        && opening.balance - balance === 480000 && outcome.balance_paise === balance
        && operation?.bank_operation_id === outcome.bank_operation_id && operation.status === "EXECUTED"
        && operation.amount_paise === 480000 && operation.balance_paise === balance,
      "persisted bank operation, debit or balance disagreed with the approved payment");
      console.log(`Bank operation ${publicId(operation.bank_operation_id)}; ledger entry ${publicId(debits[0].entry_id)}; balance ${opening.balance} → ${balance} paise`);
    }, !!(opening && bundle));
    await step("witness-backed ZIP and Ops and Demo Bank QR downloads", async () => {
      const response = await fetch(stack.gateway + "/v1/audit/proof/" + payment.request);
      requireResult(response.status === 401, "Dispute export must require Ops authentication.");
      await stack.startDemo(origin, verifier.url);
      const context = await browser.newContext();
      try {
        const bankPage = await context.newPage();
        bankPage.setDefaultTimeout(10_000);
        await bankPage.goto(origin + "/demo/bank");
        await bankPage.getByLabel("Customer PIN").fill(pin);
        await button(bankPage, "Sign in").click();
        await bankPage.getByText("Live updates", { exact: true }).waitFor();
        for (const name of ["Mandates", "Write a mandate", "Use example", "Parse terms", "Review mandate"]) {
          await button(bankPage, name).click();
        }
        await bankPage.getByLabel("Confirmation PIN").fill(pin);
        await button(bankPage, "Sign mandate").click();
        const active = bankPage.locator("[data-mandate-id]").filter({ hasText: "Active" });
        const activeMandate = await active.getAttribute("data-mandate-id");
        await button(bankPage, "Home").click();
        const n1 = await stack.pay(activeMandate, 185000);
        const witness = await stack.witness();
        await downloadAndCheck({ ops, browser, host: verifier, witness, payment, receipt });
        await checkReceiptQr(ops, button(ops, "Download QR code"), verifier.url, payment.request, true, witness.checkpoint.seq);
        const paid = bankPage.getByRole("region", { name: "Notifications" }).getByRole("listitem")
          .filter({ hasText: /paid .*1,850\.00/ });
        await checkReceiptQr(bankPage, button(paid, "Download QR code"), verifier.url, n1.request_id, false, witness.checkpoint.seq);
        await bankPage.reload();
        const row = bankPage.getByRole("region", { name: "Payment receipts" }).getByRole("listitem")
          .filter({ hasText: n1.request_id });
        await checkReceiptQr(bankPage, button(row, "Download QR code"), verifier.url, n1.request_id, false, witness.checkpoint.seq);
      } finally {
        await context.close();
      }
    }, !!(opening && bundle && receipt));
    await step("Ops suspend and reinstate through refreshed registry state", async () => {
      const agent = ops.getByRole("listitem").filter({ has: ops.getByText("agt_billpal", { exact: true }) });
      for (const [action, label, status] of [
        ["suspend", "Suspend globally BillPal Agent", "SUSPENDED"],
        ["reinstate", "Reinstate BillPal Agent", "ACTIVE"],
      ]) {
        const response = await exchange(ops, stack.gateway, `/v1/ops/agents/agt_billpal/${action}`, "POST",
          () => button(agent, label).click(), [200]);
        requireResult((await response.json()).status === status, `agent mutation did not return ${status}`);
        await agent.getByText(status, { exact: true }).waitFor();
      }
    }, !!(operator && bundle));
    await step("browser HTTP destinations limited to UI and gateway", () => {
      requireResult(!unexpectedDestination, "browser contacted an unexpected origin");
    }, !!(customer && ops));
  } finally {
    try { await browser?.close(); } finally {
      try { await vite?.close(); } finally {
        try { await verifier?.close(); } finally { await stack.close(); }
      }
    }
  }
}

run().catch(() => {
  console.error("FAIL     Smoke setup or cleanup failed. Check the locked dependencies, Chromium installation and local launch instructions.");
  process.exitCode = 1;
});
