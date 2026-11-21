import { ApiError, errorFromResponse } from "../../../api/errors";
import { isRecord } from "../../../api/shellWire";
import { readSse, type SseFrame } from "../../../api/sse";

export type Scenario = {
  id: string; kind: "normal" | "attack"; group: string; title: string; description: string;
  prompt: string; attachment: string | null; prerequisites: string; expected_stop_point: string;
  expected_verdict: string; expected_reason_code?: string | null;
  expected_risk_band?: string | null; expected_risk_score?: number | null;
  observation_available: boolean; source: string; fixture?: Record<string, unknown> | null;
  observed_result?: Record<string, unknown> | null;
};
export type Catalogue = {
  items: Scenario[];
  policy_inputs: { payees: { id: string; name: string; category: string }[]; categories: string[] };
};
export type PolicyInput = { action: string; payee_id: string; amount_paise: number; category: string; document?: string };
export type StartInput = { scenario_id?: string; prompt?: string; attachment?: string; overrides?: PolicyInput; agent_mode?: string };
export type Step = {
  seq: number; name: string; tool?: string; args?: Record<string, string | number>; requestId?: string;
  verdict?: string; status?: string; reason?: string; message?: string; cached: boolean;
  delta?: number; agentStatus?: string; suspended?: boolean;
  attempt?: number; traceRequestId?: string;
};

const hostname = window.location.hostname === "localhost" ? "localhost" : "127.0.0.1";
const base = (import.meta.env.VITE_DEMO_AGENT_ORIGIN?.trim() || import.meta.env.VITE_DEMO_BANK_ORIGIN?.trim()
  || `http://${hostname}:8002`).replace(/\/$/, "") + "/demo";
const text = (value: unknown) => typeof value === "string" ? value : undefined;
const number = (value: unknown) => typeof value === "number" && Number.isSafeInteger(value) ? value : undefined;

async function request(path: string, signal: AbortSignal, payload?: object) {
  const response = await fetch(base + path, {
    signal, cache: "no-store", method: payload ? "POST" : "GET",
    headers: payload ? { "Content-Type": "application/json" } : undefined,
    body: payload && JSON.stringify(payload),
  });
  if (!response.ok) throw errorFromResponse(response, await response.json().catch(() => undefined));
  return response;
}

export async function readCatalogue(signal: AbortSignal): Promise<Catalogue> {
  const value = await (await request("/scenarios", signal)).json();
  if (!Array.isArray(value?.items) || !Array.isArray(value?.policy_inputs?.payees)
    || !Array.isArray(value?.policy_inputs?.categories)) throw new Error("The scenario catalogue is unavailable.");
  return value;
}

export async function startRun(payload: StartInput, signal: AbortSignal) {
  const value = await (await request("/agent/run", signal, payload)).json();
  if (!/^run_[\w-]+$/.test(value?.run_id ?? "")) throw new Error("The server did not confirm a run ID.");
  return value.run_id as string;
}

export async function softReset(signal: AbortSignal) {
  const value = await (await request("/reset", signal, {})).json();
  const count = Array.isArray(value.revoked_mandates) ? value.revoked_mandates.length : 0;
  return `${value.created_mandate_id ? "Fresh demo mandate created." : "Could not create a fresh demo mandate."} `
    + `${count} mandates revoked. ${value.reinstated_agent ? "BillPal reinstated." : "Reinstatement was not confirmed."}`;
}

export function decodeStep(frame: SseFrame): Step | undefined {
  if (frame.event !== "agent_step" || !frame.id || !/^\d{1,9}$/.test(frame.id)) return;
  let raw: unknown;
  try { raw = JSON.parse(frame.data); } catch { return; }
  if (!isRecord(raw) || typeof raw.step !== "string") return;
  const args: NonNullable<Step["args"]> = {};
  if (isRecord(raw.args)) for (const key of ["from_account", "payee_id", "amount_paise", "category"]) {
    const value = key === "amount_paise" ? number(raw.args[key]) : text(raw.args[key]);
    if (value !== undefined) args[key] = value;
  }
  const id = text(raw.request_id);
  return {
    seq: Number(frame.id), name: raw.step, tool: text(raw.tool) ?? text(raw.action), args,
    attempt: number(raw.attempt),
    traceRequestId: typeof raw.trace_request_id === "string" && /^req_[a-f0-9]{16}$/.test(raw.trace_request_id)
      ? raw.trace_request_id : undefined,
    requestId: id && /^req_[a-f0-9]{16}$/.test(id) ? id : undefined,
    verdict: text(raw.verdict), reason: text(raw.reason_code), status: text(raw.status),
    message: ["refusal", "clarification"].includes(raw.step) ? text(raw.message) : undefined,
    cached: raw.cached === true, delta: number(raw.balance_delta_paise), agentStatus: text(raw.agent_status),
    suspended: typeof raw.agent_suspended_during_run === "boolean" ? raw.agent_suspended_during_run : undefined,
  };
}

export async function streamRun(id: string, cursor: number, signal: AbortSignal, onStep: (step: Step) => void) {
  const headers = new Headers({ Accept: "text/event-stream" });
  if (cursor) headers.set("Last-Event-ID", String(cursor));
  const response = await fetch(`${base}/agent/stream?run_id=${encodeURIComponent(id)}`, { signal, headers });
  if (!response.ok || !response.body) throw new ApiError("Run history is unavailable.", "UI_RUN_UNAVAILABLE", undefined, response.status);
  await readSse(response.body, signal, (frame) => {
    const step = decodeStep(frame);
    if (step) onStep(step);
  });
}

export const isFixture = (card: Scenario) => card.source !== "live_supported"
  || card.group === "identity_integrity" || card.group === "audit_tamper";
