import { execFile, spawn } from "node:child_process";
import { randomBytes, randomInt } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

const exec = promisify(execFile);
export const frontend = fileURLToPath(new URL("../../", import.meta.url));
const repo = path.resolve(frontend, "..");
const windows = process.platform === "win32";
const python = path.join(repo, ".venv", windows ? "Scripts/python.exe" : "bin/python");
export class SmokeFailure extends Error {}
export function requireResult(condition, message) {
  if (!condition) throw new SmokeFailure(message);
}

async function localPort() {
  const socket = createServer();
  await new Promise((resolve, reject) => socket.once("error", reject).listen(0, "127.0.0.1", resolve));
  const { port } = socket.address();
  await new Promise((resolve) => socket.close(resolve));
  return port;
}

export async function disposableStack() {
  const dir = await mkdtemp(path.join(tmpdir(), "agentgate-d06-"));
  const runtime = path.join(dir, "runtime");
  await mkdir(runtime);
  const gatewayPort = await localPort();
  let bankPort = await localPort();
  while (bankPort === gatewayPort) bankPort = await localPort();
  let demoPort = await localPort();
  while ([gatewayPort, bankPort].includes(demoPort)) demoPort = await localPort();
  const gateway = `http://127.0.0.1:${gatewayPort}`;
  const bank = `http://127.0.0.1:${bankPort}`;
  const demo = `http://127.0.0.1:${demoPort}`;
  const opsId = "ops_admin";
  const opsPassword = randomBytes(24).toString("hex");
  const reference = `d06-${randomBytes(16).toString("hex")}`;
  const customerPin = String(randomInt(1000, 10000));
  const billpalKey = path.join(runtime, "billpal.pem");
  const separateWitness = path.join(dir, "witness");
  await mkdir(separateWitness);
  const witnessKey = path.join(separateWitness, "witness.pem");
  const trust = path.join(separateWitness, "trust.json");
  const checkpoint = path.join(separateWitness, "checkpoint.json");
  const env = { ...process.env, PYTHONPATH: repo, AGENTGATE_PROFILE: "real",
    D06_OPS_ID: opsId, D06_OPS_PASSWORD: opsPassword, D06_CUSTOMER_PIN: customerPin,
    D06_BILLPAL_KEY: billpalKey, D06_REGISTRAR_PASSWORD: randomBytes(24).toString("hex"),
    AGENTGATE_BANK_SERVICE_CREDENTIAL: randomBytes(32).toString("hex"),
    AGENTGATE_GATEWAY_ORIGIN: gateway, AGENTGATE_BANK_ORIGIN: bank,
    AGENTGATE_GATEWAY_DB: path.join(runtime, "gateway.db"), AGENTGATE_BANK_DB: path.join(runtime, "bank.db"),
    AGENTGATE_BANK_DB_URL: `sqlite+aiosqlite:///${path.join(runtime, "bank.db").split(path.sep).join("/")}`,
    AGENTGATE_DISPUTE_TRUST_PATH: trust, AGENTGATE_DISPUTE_CHECKPOINT_PATH: checkpoint,
  };
  for (const name of ["MANDATE", "AUDIT", "TOKEN"]) {
    env[`AGENTGATE_${name}_SIGNING_KEY_PATH`] = path.join(runtime, `${name.toLowerCase()}.pem`);
  }
  const options = { env, cwd: dir, windowsHide: true };
  const children = [];
  async function launch(args, url) {
    let missing = [];
    const child = spawn(python, ["-m", ...args], {
      ...options, detached: !windows, stdio: ["ignore", "ignore", "pipe"],
    });
    const closed = new Promise((resolve) => child.once("close", resolve));
    children.push({ child, closed });
    let stopped = false;
    child.once("error", () => { stopped = true; });
    child.once("exit", () => { stopped = true; });
    let diagnostic = "";
    child.stderr.on("data", (chunk) => {
      // Only these known provider names may reach stdout. Drop raw launcher diagnostics.
      diagnostic = (diagnostic + chunk).slice(-4096);
      const line = diagnostic.match(/Missing required real providers: ([a-z_, ]+)/);
      if (line) missing = line[1].split(", ").filter((name) =>
        ["identity_verifier", "human_auth", "audit_log", "policy_evaluator", "risk_engine",
          "escalation_service", "bank_client"].includes(name));
    });
    for (let attempt = 0; attempt < 100 && !stopped; attempt++) {
      try {
        const res = await fetch(url, { signal: AbortSignal.timeout(700), redirect: "error" });
        if (res.ok) return await res.json();
        if (res.status !== 503) throw new SmokeFailure(`HTTP ${res.status}`);
      } catch (error) { if (error instanceof SmokeFailure) throw error; }
      await delay(150);
    }
    throw new SmokeFailure(missing.length
      ? `real startup refused; missing ${missing.join(", ")}; no HTTP readiness response`
      : "service did not become ready; check the Python environment and local launcher");
  }
  return {
    gateway,
    bankOrigin: bank,
    demo,
    opsId,
    opsPassword,
    customerPin,
    reference,
    async seed() {
      try {
        await exec(python, ["-m", "tools.seed"], { ...options, timeout: 30000 });
        const { stdout } = await exec(python, ["-m", "agent_sdk.cli", "inspect-key", "--key",
          env.AGENTGATE_AUDIT_SIGNING_KEY_PATH], options);
        const auditPublic = path.join(separateWitness, "audit-public.json");
        await writeFile(auditPublic, stdout);
        await exec(python, ["-m", "agent_sdk.cli", "keygen", "--out", witnessKey], options);
        await exec(python, ["-m", "tools.audit_witness", "register", "--log-id", "disposable-audit",
          "--audit-public", auditPublic, "--witness-key", witnessKey, "--trust", trust], options);
      }
      catch (error) {
        throw new SmokeFailure(error.stderr?.includes("intentionally not implemented")
          ? "tools.seed is the IC0 placeholder (exit 1)" : "tools.seed failed");
      }
    },
    async witness() {
      const journal = path.join(separateWitness, "journal.db");
      const earlier = path.join(separateWitness, "earlier.json");
      const publish = ["-m", "tools.audit_witness", "checkpoint", "--audit-db", env.AGENTGATE_GATEWAY_DB,
        "--witness-key", witnessKey, "--trust", trust, "--journal", journal];
      await exec(python, [...publish, "--output", earlier, "--through-sequence", "1"], options);
      await exec(python, [...publish, "--output", checkpoint], options);
      return { dir: separateWitness, earlier: JSON.parse(await readFile(earlier, "utf8")),
        checkpoint: JSON.parse(await readFile(checkpoint, "utf8")),
        secret: await readFile(witnessKey), trust: await readFile(trust) };
    },
    async verifierTls() {
      const { stdout } = await exec(python, [path.join(frontend, "tests/real-smoke/tls_fixture.py")], {
        ...options, timeout: 10000,
      });
      return JSON.parse(stdout);
    },
    async bank() {
      const response = await fetch(bank + "/openapi.json", { signal: AbortSignal.timeout(3000), redirect: "error" });
      requireResult(response.ok, `bank readiness returned HTTP ${response.status}`);
      return response.json();
    },
    ready: () => launch(["tools.dev", "stack", "--profile", "real", "--port", String(gatewayPort),
      "--bank-port", String(bankPort), "--no-demo"], gateway + "/health/ready"),
    startDemo(origin, verifierUrl) {
      env.DEMO_FRONTEND_ORIGINS = origin;
      env.AGENTGATE_DISPUTE_VERIFIER_URL = verifierUrl;
      return launch(["uvicorn", "demo_server.main:app", "--host", "127.0.0.1", "--port", String(demoPort)], demo + "/health");
    },
    async checkSdk() {
      const { stdout } = await exec(python, ["-m", "agent_sdk.cli", "pay-bill", "--help"], { ...options, timeout: 10000 });
      requireResult(["--key", "--mandate-id", "--amount-paise"].every((flag) => stdout.includes(flag)),
        "checkout does not provide B's signed pay-bill command");
    },
    async pay(mandateId, amount = 480000) {
      let stdout;
      try {
        ({ stdout } = await exec(python, ["-m", "agent_sdk.cli", "pay-bill", "--gateway-url", gateway,
          "--agent-id", "agt_billpal", "--mandate-id", mandateId, "--key", billpalKey, "--from-account", "ACC-001",
          "--payee-id", "BILL-ELEC", "--amount-paise", String(amount), "--task", "Pay electricity bill",
          "--category", "utilities.electricity", "--reference", reference,
          "--idempotency-key", `d06-${randomBytes(16).toString("hex")}`],
        { ...options, timeout: 30000 }));
      } catch {
        throw new SmokeFailure("B SDK pay-bill failed; signed action outcome unconfirmed");
      }
      requireResult(stdout.trim(), "B SDK pay-bill produced no result");
      const result = JSON.parse(stdout);
      requireResult(amount === 480000 ? result.status === "PENDING_APPROVAL" && result.decision === "ESCALATE"
        : result.status === "EXECUTED", "signed payment did not reach the expected state");
      return result;
    },
    bankSnapshot(bankOutcome) {
      // Inspect the disposable provider database without expanding the browser or bank API.
      const db = new DatabaseSync(env.AGENTGATE_BANK_DB, { readOnly: true });
      try {
        const account = db.prepare("SELECT balance_paise FROM accounts WHERE account_id = ?").get("ACC-001");
        const debits = db.prepare(`SELECT entry_id, amount_paise, balance_after_paise, reference
          FROM ledger WHERE account_id = ? AND entry_type = 'DEBIT'`).all("ACC-001");
        const operation = bankOutcome && db.prepare(`SELECT bank_operation_id, status, amount_paise, balance_paise
          FROM bank_idempotency_records WHERE bank_idempotency_key = ?`).get(bankOutcome.bank_idempotency_key);
        return { balance: account?.balance_paise, debits, operation };
      } finally {
        db.close();
      }
    },
    async close() {
      for (const { child, closed } of children.reverse()) {
        if (!child.pid) continue;
        // Both the bank and its launcher inherit the diagnostic pipe.
        if (windows) {
          if (child.exitCode === null && child.signalCode === null) {
            await exec("taskkill", ["/PID", String(child.pid), "/T", "/F"], { windowsHide: true });
          }
        } else {
          try { process.kill(-child.pid, "SIGTERM"); }
          catch (error) { if (error.code !== "ESRCH") throw error; }
        }
        await closed;
      }
      await rm(dir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
    },
  };
}
