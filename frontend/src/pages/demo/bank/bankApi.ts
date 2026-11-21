import { observeDecision, type CustomerDecision } from "../../../api/approvalWire";
import { ApiError, errorFromResponse, redactPin } from "../../../api/errors";
import type { CreateMandateBody } from "../../../api/mandateWire";
import { readSse, type SseFrame } from "../../../api/sse";
import { readQrSummary } from "../../../components/qrReceipt";
import { parseOverview, stringField } from "./bankWire";

const loopback = window.location.hostname === "localhost" ? "localhost" : "127.0.0.1";
const origin = (import.meta.env.VITE_DEMO_BANK_ORIGIN?.trim() || `http://${loopback}:8002`).replace(/\/$/, "");
const base = `${origin}/demo/bank`;

async function request(path: string, signal: AbortSignal, body?: object) {
  try {
    return await fetch(base + path, {
      signal, credentials: "include", cache: "no-store",
      method: body ? "POST" : "GET",
      headers: { Accept: path === "/stream" ? "text/event-stream" : "application/json",
        ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body && JSON.stringify(body),
    });
  } catch (error) {
    if (signal.aborted) throw error;
    throw new ApiError("The demo bank is unavailable.", "UI_BANK_UNREACHABLE");
  }
}

async function json(path: string, signal: AbortSignal, body?: object, pin?: string) {
  const response = await request(path, signal, body);
  const value: unknown = response.status === 204 ? undefined : await response.json().catch(() => undefined);
  if (!response.ok) throw redactPin(errorFromResponse(response, value), pin);
  return { value, status: response.status };
}

const invalidResponse = () => new ApiError("The bank returned incomplete account data.", "UI_BANK_INVALID_RESPONSE");

export const bankApi = {
  async login(customerId: string, pin: string, signal: AbortSignal) {
    const { value } = await json("/login", signal, { customer_id: customerId, pin }, pin);
    if (stringField(value, "customer_id") !== customerId || !stringField(value, "account_id")) throw invalidResponse();
  },
  async logout(signal: AbortSignal) {
    await json("/logout", signal, {});
  },
  async overview(signal: AbortSignal) {
    const { value } = await json("/overview", signal);
    const overview = parseOverview(value);
    if (!overview) throw invalidResponse();
    return overview;
  },
  async create(body: CreateMandateBody, signal: AbortSignal) {
    await json("/mandates", signal, body, body.pin);
  },
  async revoke(id: string, signal: AbortSignal) {
    const { value } = await json(`/mandates/${encodeURIComponent(id)}/revoke`, signal, {});
    if (stringField(value, "mandate_id") !== id || stringField(value, "status") !== "REVOKED") throw invalidResponse();
  },
  async decide(id: string, requestId: string, choice: CustomerDecision, signal: AbortSignal) {
    const { value, status } = await json(`/approvals/${encodeURIComponent(id)}`, signal, choice,
      choice.decision === "APPROVE" ? choice.pin : undefined);
    return observeDecision(value, status, id, requestId);
  },
  async receiptQr(requestId: string, signal: AbortSignal) {
    const { value } = await json(`/receipts/${encodeURIComponent(requestId)}/qr`, signal);
    return readQrSummary(value, requestId);
  },
  async stream(cursor: string | undefined, signal: AbortSignal, opened: () => void, onFrame: (frame: SseFrame) => void) {
    const headers = new Headers({ Accept: "text/event-stream" });
    if (cursor) headers.set("Last-Event-ID", cursor);
    const response = await fetch(`${base}/stream`, { credentials: "include", signal, headers });
    if (!response.ok || !response.body || !response.headers.get("Content-Type")?.startsWith("text/event-stream")) {
      void response.body?.cancel().catch(() => undefined);
      throw errorFromResponse(response, undefined);
    }
    opened();
    await readSse(response.body, signal, onFrame);
  },
};
