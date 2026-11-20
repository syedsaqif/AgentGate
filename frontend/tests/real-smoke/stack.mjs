import { execFile, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
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
  const gateway = `http://127.0.0.1:${gatewayPort}`;
  const bank = `http://127.0.0.1:${bankPort}`;
  const env = { ...process.env, PYTHONPATH: repo, AGENTGATE_PROFILE: "real",
    AGENTGATE_BANK_SERVICE_CREDENTIAL: randomBytes(32).toString("hex"),
    AGENTGATE_HUMAN_SESSION_SECRET: randomBytes(32).toString("hex"),
    AGENTGATE_GATEWAY_ORIGIN: gateway, AGENTGATE_BANK_ORIGIN: bank,
    AGENTGATE_GATEWAY_DB: path.join(runtime, "gateway.db"), AGENTGATE_BANK_DB: path.join(runtime, "bank.db"),
    AGENTGATE_BANK_DB_URL: "sqlite+aiosqlite:///runtime/bank.db",
  };
  for (const name of ["MANDATE", "AUDIT", "TOKEN"]) {
    env[`AGENTGATE_${name}_SIGNING_KEY_PATH`] = path.join(runtime, `${name.toLowerCase()}.pem`);
  }
  const options = { env, cwd: dir, windowsHide: true };
  const children = [];
  async function launch(args, url) {
    let missing = [];
    const child = spawn(python, ["-m", ...args], { ...options, stdio: ["ignore", "ignore", "pipe"] });
    children.push(child);
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
    async seed() {
      try { await exec(python, ["-m", "tools.seed"], { ...options, timeout: 30000 }); }
      catch (error) {
        throw new SmokeFailure(error.stderr?.includes("intentionally not implemented")
          ? "tools.seed is the IC0 placeholder (exit 1)" : "tools.seed failed");
      }
    },
    bank: () => launch(["uvicorn", "bank_mock.main:app", "--host", "127.0.0.1", "--port", String(bankPort)], bank + "/openapi.json"),
    ready: () => launch(["tools.dev", "service", "a", "--profile", "real", "--port", String(gatewayPort)], gateway + "/health/ready"),
    async checkSdk() {
      const { stdout } = await exec(python, ["-m", "agent_sdk.cli", "pay-bill", "--help"], { ...options, timeout: 10000 });
      requireResult(["--key", "--mandate-id", "--amount-paise"].every((flag) => stdout.includes(flag)),
        "checkout does not provide B's signed pay-bill command");
    },
    async pay(mandateId) {
      const key = process.env.D06_BILLPAL_KEY;
      requireResult(key && path.isAbsolute(key), "B seed must supply the registered BillPal PEM path in D06_BILLPAL_KEY");
      let stdout;
      try {
        ({ stdout } = await exec(python, ["-m", "agent_sdk.cli", "pay-bill", "--gateway-url", gateway,
          "--agent-id", "agt_billpal", "--mandate-id", mandateId, "--key", key, "--from-account", "ACC-001",
          "--payee-id", "BILL-ELEC", "--amount-paise", "480000", "--task", "Pay electricity bill",
          "--category", "utilities", "--idempotency-key", `d06-${randomBytes(16).toString("hex")}`],
        { ...options, timeout: 30000 }));
      } catch { throw new SmokeFailure("B SDK pay-bill failed; signed action outcome unconfirmed"); }
      requireResult(stdout.trim(), "B SDK pay-bill produced no result");
      const result = JSON.parse(stdout);
      requireResult(result.status === "PENDING_APPROVAL" && result.decision === "ESCALATE", "signed payment did not return pending approval");
      return result;
    },
    async close() {
      for (const child of children.reverse()) {
        if (!child.pid || child.exitCode !== null || child.signalCode !== null) continue;
        // Windows venv launchers can have a second Python process.
        if (windows) await exec("taskkill", ["/PID", String(child.pid), "/T", "/F"], { windowsHide: true });
        else {
          const closed = new Promise((resolve) => child.once("close", resolve));
          child.kill();
          await closed;
        }
      }
      await rm(dir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
    },
  };
}
