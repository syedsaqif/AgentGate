import { createHash, sign } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { createServer } from "node:https";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { strFromU8, unzipSync } from "fflate";
import jsQR from "jsqr";
import { PNG } from "pngjs";
import { devices } from "playwright";
import { requireResult } from "./stack.mjs";

const filename = "dispute-verifier-v1.html";
const anchored = "Verified against external witness";
const failed = "Verification failed";

function canonical(value) {
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (value !== null && typeof value === "object") {
    return "{" + Object.keys(value).sort().map(key =>
      JSON.stringify(key) + ":" + canonical(value[key])).join(",") + "}";
  }
  return JSON.stringify(value);
}

export async function verifierHost(tls) {
  const html = await readFile(new URL("../../verifier/" + filename, import.meta.url));
  const server = createServer(tls, (req, res) => {
    if (req.url === "/" + filename) {
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      res.end(html);
    } else {
      res.writeHead(404).end();
    }
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject).listen(0, "127.0.0.1", resolve);
  });
  return {
    url: "https://127.0.0.1:" + server.address().port + "/" + filename,
    close: () => new Promise(resolve => {
      server.close(resolve);
      server.closeAllConnections();
    }),
  };
}

export async function downloadAndCheck({ ops, browser, host, witness, payment, receipt }) {
  const downloading = ops.waitForEvent("download").catch(() => null);
  const [response] = await Promise.all([
    ops.waitForResponse(result => result.url().endsWith("/v1/audit/proof/" + payment.request)),
    ops.getByRole("button", { name: "Export Court-Ready Proof" }).click(),
  ]).catch(() => []);
  requireResult(response, "Ops export did not request the proof endpoint.");
  requireResult(response.ok(), "Ops proof endpoint returned HTTP " + response.status() + ".");
  const download = await downloading;
  requireResult(download, "Ops proof archive download did not start.");
  const zip = unzipSync(await readFile(await download.path()));
  requireResult(Object.keys(zip).sort().join(",") === [filename, "proof.json", "receipt.html"].join(","),
    "Proof zip contents differ from the standalone export.");
  const proofBytes = zip["proof.json"];
  const proof = JSON.parse(strFromU8(proofBytes));
  const printed = strFromU8(zip["receipt.html"]);
  requireResult(proof.record.request_id === payment.request && proof.evidence.receipt_seq === receipt.seq
    && proof.evidence.receipt_hash === receipt.entry_hash && proof.evidence.amount_paise === 480000
    && proof.witness_checkpoint.seq >= proof.record.seq && printed.includes(receipt.entry_hash)
    && printed.includes(payment.request), "Disclosure or printable receipt differs from Customer receipt.");
  requireResult(!/proof_jws|ACC-001|cust_001|BILL-ELEC|mandate_snapshot|authorization|session_token|idempotency_key/i
    .test(strFromU8(proofBytes)), "Confidential field found in exported proof.");
  const qrBytes = /src="data:image\/png;base64,([^"]+)"/.exec(printed)?.[1];
  requireResult(qrBytes, "Printable receipt QR missing.");
  const { data, width, height } = PNG.sync.read(Buffer.from(qrBytes, "base64"));
  const decoded = jsQR(new Uint8ClampedArray(data), width, height)?.data;
  requireResult(decoded === host.url, "The QR must contain only the versioned HTTPS verifier URL.");
  const fingerprint = createHash("sha256").update(witness.trust).digest("hex");
  const trust = JSON.parse(witness.trust);
  const localFile = path.join(witness.dir, filename);
  await writeFile(localFile, zip[filename]);

  function payload(name, value) {
    return { name, mimeType: "application/json", buffer: Buffer.from(canonical(value)) };
  }
  async function verify(page, packageValue, expected, reason, trustValue = trust, independent) {
    await page.locator("#proof").setInputFiles(payload("proof.json", packageValue));
    await page.locator("#trust").setInputFiles(payload("trust.json", trustValue));
    await page.locator("#checkpoint").setInputFiles(independent ? payload("newer.json", independent) : []);
    await page.locator("#fingerprint").fill(fingerprint);
    await page.getByRole("button", { name: "Verify proof" }).click();
    await page.waitForFunction(() => ["Verified against external witness", "Verification failed",
      "Valid chain without external anchoring"].includes(document.getElementById("status").textContent));
    requireResult(await page.locator("#status").innerText() === expected,
      "Verifier disagreed with expected outcome " + expected + ".");
    if (reason) {
      requireResult((await page.locator("#reason").innerText()).includes(reason),
        "Verifier failure reason differs from the expected check.");
    }
  }

  async function offline(url, contextOptions, check) {
    const context = await browser.newContext({ ...contextOptions, serviceWorkers: "block" });
    try {
      const local = url.startsWith("file:");
      const requests = [];
      context.on("request", request => {
        if (/^https?:/.test(request.url())) requests.push(request.url());
      });
      if (local) await context.setOffline(true);
      const page = await context.newPage();
      await page.goto(url);
      const loadRequests = local ? 0 : 1;
      requireResult(requests.length === loadRequests && (local || requests[0] === url),
        "Opening the verifier requested an unexpected network resource.");
      if (!local) await context.setOffline(true);
      await check(page);
      requireResult(requests.length === loadRequests,
        "The verifier attempted a network request after loading.");
    } finally {
      await context.close();
    }
  }

  await offline(decoded, { ...devices["Pixel 7"], ignoreHTTPSErrors: true }, page => (
    verify(page, proof, anchored, "covered through sequence")
  ));
  await offline(pathToFileURL(localFile).href, {}, async page => {
    await verify(page, proof, anchored);
    await verify(page, { ...proof, witness_checkpoint: null }, "Valid chain without external anchoring");
    await verify(page, { ...proof, witness_checkpoint: null }, anchored, "covered through sequence",
      trust, proof.witness_checkpoint);
    await verify(page, { ...proof, record: null }, failed, "Malformed redacted disclosure");
    await verify(page, { ...proof, algorithm: "unsupported" }, failed, "Unsupported proof");
    await verify(page, { ...proof, evidence: { ...proof.evidence, amount_paise: 1 } }, failed, "Edited");
    const evidence = { ...proof.evidence, amount_paise: 1 };
    await verify(page, { ...proof, evidence, record: { ...proof.record, safe_payload: evidence } },
      failed, "audit record hash");
    const reversed = [proof.links[1], proof.links[0], ...proof.links.slice(2)];
    await verify(page, { ...proof, links: reversed }, failed, "Sequence gap");
    await verify(page, { ...proof, links: proof.links.filter((_, index) => index !== 1) },
      failed, "Sequence gap");
    const first = proof.links[0];
    const wrongLink = { ...first, prev_hash: "1".repeat(64) };
    await verify(page, { ...proof, links: [wrongLink, ...proof.links.slice(1)] }, failed, "Previous-hash");
    const invalidSignature = value => (value[0] === "A" ? "B" : "A") + value.slice(1);
    const badSig = { ...first, sig: invalidSignature(first.sig) };
    await verify(page, { ...proof, links: [badSig, ...proof.links.slice(1)] }, failed, "Invalid audit record signature");
    const seal = proof.witness_checkpoint;
    const badWitness = { ...seal, sig: invalidSignature(seal.sig) };
    await verify(page, { ...proof, witness_checkpoint: badWitness }, failed, "Invalid witness-checkpoint signature");
    await verify(page, { ...proof, audit_seal: { ...proof.audit_seal, key_id: seal.key_id } }, failed, "Unknown audit-prefix");
    await verify(page, { ...proof, witness_checkpoint: { ...seal, key_id: proof.audit_seal.key_id } }, failed, "Unknown witness-checkpoint");
    await verify(page, proof, failed, "fingerprint", { ...trust, audit_key: trust.witness_key });
    await verify(page, proof, failed, "fingerprint", { ...trust, witness_key: trust.audit_key });
    await verify(page, { ...proof, witness_checkpoint: witness.earlier }, failed, "predates");
    const signedWitness = changes => {
      const { sig: _, ...unsigned } = { ...seal, ...changes };
      return { ...unsigned, sig: sign(null, Buffer.from(canonical(unsigned)), witness.secret).toString("base64url") };
    };
    await verify(page, { ...proof, witness_checkpoint: signedWitness({ log_id: "other-log" }) },
      failed, "another log");
    await verify(page, { ...proof, witness_checkpoint: signedWitness({ entry_hash: "1".repeat(64) }) },
      failed, "conflicts");
    await verify(page, proof, failed, "incomplete", trust, signedWitness({ seq: seal.seq + 1 }));
  });
}
