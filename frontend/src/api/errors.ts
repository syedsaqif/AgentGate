import { isRecord } from "./shellWire";
import type { components } from "./generated/openapi";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly code: string,
    readonly requestId?: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export function redactPin(error: unknown, secret?: string, message = "The gateway rejected the request."): unknown {
  if (!(error instanceof ApiError) || !secret) return error;
  const pin = secret.trim();
  const containsPin = (text?: string) => text !== undefined
    && (text.includes(secret) || (pin !== "" && text.includes(pin)));
  return new ApiError(
    containsPin(error.message) ? message : error.message,
    containsPin(error.code) ? "UI_REQUEST_FAILED" : error.code,
    containsPin(error.requestId) ? undefined : error.requestId,
    error.status,
  );
}

export function errorFromResponse(response: Response, payload: unknown): ApiError {
  if (isErrorEnvelope(payload)) {
    const { message, code, request_id } = payload.error;
    return new ApiError(message, code, request_id, response.status);
  }
  return new ApiError(
    response.ok
      ? "The gateway returned an unexpected response."
      : `The gateway request failed (HTTP ${response.status}).`,
    response.ok ? "UI_INVALID_RESPONSE" : "UI_HTTP_ERROR",
    response.headers.get("X-Request-ID") ?? undefined,
    response.status,
  );
}

function isErrorEnvelope(value: unknown): value is components["schemas"]["ErrorEnvelope"] {
  return isRecord(value) && isErrorDetail(value.error);
}

function isErrorDetail(value: unknown): value is components["schemas"]["ErrorDetail"] {
  return isRecord(value)
    && typeof value.code === "string"
    && typeof value.message === "string"
    && typeof value.request_id === "string";
}
