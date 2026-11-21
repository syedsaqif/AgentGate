import assert from "node:assert/strict";
import { chromium } from "playwright";
import { createServer } from "vite";
import { checkReceiptQr, verifierHost } from "./dispute.mjs";
import { frames } from "./capture-stage.mjs";
import { disposableStack, frontend } from "./stack.mjs";

async function exercise(browser, script) {
  const stack = await disposableStack();
  let vite, verifier, stopped = false;
  const context = await browser.newContext({ viewport:{width:1440,height:900} });
  const page = await context.newPage();
  page.setDefaultTimeout(15000);
  let posts = 0;
  page.on("request", r => { if (r.method() === "POST" && r.url().endsWith("/demo/agent/run")) posts++; });
  try {
    await stack.seed(); await stack.ready();
    verifier = await verifierHost(await stack.verifierTls());
    process.env.VITE_DEMO_AGENT_ORIGIN = stack.demo;
    process.env.VITE_DEMO_BANK_ORIGIN = stack.demo;
    process.env.VITE_DEMO_GATE_STREAM_URL = stack.demo + "/demo/gate/stream";
    vite = await createServer({root:frontend,mode:"real", server:{host:"127.0.0.1",port:0}});
    await vite.listen();
    const ui = "http://127.0.0.1:" + vite.httpServer.address().port;
    await stack.startDemo(ui, verifier.url);
    await page.goto(ui + "/demo");
    const button = name => page.getByRole("button", {name,exact:true});
    const gate = page.getByRole("region", {name:"Gate",exact:true});
    const result = page.getByRole("region", {name:"Run result"});
    const bank = page.locator(".stage-bank");
    await bank.getByLabel("Customer PIN").fill(stack.customerPin);
    await bank.getByRole("button",{name:"Sign in"}).click();
    await bank.locator("[data-balance-paise='12500000']").waitFor();
    const gateBox = await gate.boundingBox();
    assert(gateBox.width > (await bank.boundingBox()).width * 1.8);
    assert(gateBox.width > (await page.locator(".stage-agent").boundingBox()).width * 1.8);
    const scenario = page.getByLabel("Scenario", {exact:true});
    async function run(id, decision) {
      await scenario.selectOption(id);
      const previous = posts;
      const posted = page.waitForResponse(r => r.request().method() === "POST" && r.url().endsWith("/demo/agent/run"));
      await button("Run scenario").click();
      assert.equal((await posted).status(), 202);
      await result.getByText(decision, {exact:true}).waitFor();
      await button("Skip to result").click();
      assert.equal(posts, previous + 1);
    }
    async function approval(amount) {
      const before = stack.bankSnapshot();
      await bank.getByLabel("Approval PIN").fill(stack.customerPin);
      await bank.getByRole("button",{name:"Approve",exact:true}).click();
      await bank.locator("[data-balance-paise='" + (before.balance - amount) + "']").waitFor();
      assert.equal(stack.bankSnapshot().debits.length, before.debits.length + 1);
      await gate.getByText("Bank executed",{exact:true}).waitFor();
    }
    if (script !== "fresh-N2" && script !== "P4") {
      await run("P1","ALLOW");
      await bank.locator("[data-kind='bank.executed']").waitFor();
      await gate.getByText("Bank executed",{exact:true}).waitFor();
      assert.equal(stack.bankSnapshot().balance, 12315000);
      assert.equal(stack.bankSnapshot().debits.length, 1);
    }
    if (script === "story") {
      const request = await bank.getByRole("region",{name:"Payment receipts"}).locator("code").first().textContent();
      const witness = await stack.witness();
      await checkReceiptQr(page, bank.locator("[data-kind='bank.executed']").getByRole("button",{name:"Download QR code"}),
        verifier.url,request,false,witness.checkpoint.seq);
      await run("I1","DENY");
      await gate.getByText("Policy failed",{exact:true}).waitFor();
      await gate.getByText("Agent suspended",{exact:true}).waitFor();
      await gate.locator(".gate-band[data-band='HIGH']").waitFor();
      await result.getByText("Agent suspended during run",{exact:true}).waitFor();
      assert.equal(stack.bankSnapshot().balance,12315000);
      assert.equal(stack.bankSnapshot().debits.length,1);
      const cookie = (await context.cookies(stack.demo + '/demo/bank/stream')).map(x=>x.name+"="+x.value).join("; ");
      const [customer, presenter] = await Promise.all([
        frames(stack.demo+"/demo/bank/stream", {Cookie:cookie,"Last-Event-ID":"0"},true),
        frames(stack.demo+"/demo/gate/stream", {"Last-Event-ID":"0"},true),
      ]);
      assert(customer.some(x=>x.value.event_type === "bank.executed"));
      assert(customer.some(x=>x.value.event_type === "agent.suspended"));
      assert(customer.every(x=>x.value.customer_id === "cust_001" && !["gate.step","policy.decision"].includes(x.value.event_type)
        && !("risk" in x.value.data)));
      assert(presenter.some(x=>x.value.event_type === "gate.step"));
      console.log("PASS     N1 → QR → M1, one debit, suspension and role-separated streams; 12500000 → 12315000 → 12315000 paise");

      // A new document and a stopped stack rule out cached live state masquerading as a recording.
      await page.goto("about:blank");
      await stack.close(); stopped = true;
      for (const url of [stack.demo+"/health", stack.gateway+"/health/ready", stack.bankOrigin+"/openapi.json"]) {
        await assert.rejects(fetch(url, {signal:AbortSignal.timeout(1500)}));
      }
      const requests = [];
      page.on("request", r => requests.push({url:r.url(),method:r.method()}));
      await page.goto(ui+"/demo?mode=recorded");
      for (const [id,text] of [["P1","Bank executed"],["I1","Agent suspended"],["P4","Customer approved"],["L1","Per-transaction cap"]]) {
        await scenario.selectOption(id);
        await button("Play recording").click();
        await button("Skip to result").click();
        await gate.getByText(text, {exact:true}).first().waitFor();
        await page.locator(".stage-badge").filter({hasText:/^Recorded$/}).waitFor();
        if (id === "P1") await bank.locator("[data-balance-paise='12315000']").waitFor();
        if (id === "P4") await bank.locator("[data-balance-paise='12080000']").waitFor();
      }
      await scenario.selectOption("C1"); await button("Show fixture").click();
      await page.getByRole("region",{name:"Fixture result"}).waitFor();
      assert(requests.every(r => r.method === "GET" && new URL(r.url).origin === ui));
      assert.equal(posts,2);
      console.log("PASS     Offline stage after all three services stopped; local GETs only");
    } else if (script === "P6-M3") {
      await run("P6","ALLOW");
      await gate.getByText("Stored bank result",{exact:true}).waitFor();
      await page.reload();
      await gate.getByText("Stored bank result",{exact:true}).waitFor();
      assert.equal(posts,2);
      assert.equal(stack.bankSnapshot().balance,12315000);
      assert.equal(stack.bankSnapshot().debits.length,1);
      await run("L1","DENY");
      await gate.getByText("Per-transaction cap",{exact:true}).first().waitFor();
      const explanation = await gate.locator(".gate-current").innerText();
      assert(explanation.includes("7,000") && explanation.includes("5,000"));
      assert.equal(stack.bankSnapshot().balance,12315000);
      console.log("PASS     P6 one persisted debit, reload issued no payment; M3 named the ₹7,000 request and ₹5,000 cap");
    } else if (script === "P4" || script === "fresh-N2") {
      const amount = script === "P4" ? 420000 : 480000;
      await run(script === "P4" ? "P4" : "N2","ESCALATE");
      await gate.getByText("Waiting for approval",{exact:true}).first().waitFor();
      assert.equal(stack.bankSnapshot().debits.length,0);
      await approval(amount);
      console.log("PASS    ",script,"12500000 →",stack.bankSnapshot().balance,"paise, one approved debit");
    } else {
      await scenario.selectOption("N2"); await button("Run scenario").click();
      await result.locator("dt").filter({hasText:"Observed decision"}).waitFor();
      const decision = await result.locator("dt").filter({hasText:"Observed decision"}).locator("+ dd").textContent();
      await button("Skip to result").click();
      if (decision === "ESCALATE") await approval(480000);
      else { assert.equal(decision,"DENY"); assert.equal(stack.bankSnapshot().balance,12315000); }
      console.log("PASS     N2 after N1:",decision,"closing balance",stack.bankSnapshot().balance,"paise");
    }
  } finally {
    await context.close();
    try { await vite?.close(); } finally {
      try { await verifier?.close(); } finally { if (!stopped) await stack.close(); }
    }
  }
}

const browser = await chromium.launch();
try {
  for (const script of (process.argv.length > 2 ? process.argv.slice(2) : ["story","P4","P6-M3","fresh-N2","N2-after-N1"])) {
    await exercise(browser, script);
  }
} finally { await browser.close(); }

