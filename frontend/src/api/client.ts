import { ApiError, errorFromResponse } from "./errors";
import type { operations } from "./generated/openapi";
import { isMandatePage, mandatePath, revokePath, type CreateMandateBody, type RevokePathParams } from "./mandateWire";
import {
  isCustomerBootstrap,
  isHumanSession,
  isRegisteredAgents,
  shellRoutes,
  type CustomerLogin,
  type OpsLogin,
  type Role,
} from "./shellWire";
import type { ApiTransport, PathForMethod } from "./types";

export type ShellApi = ReturnType<typeof createShellApi>;

type ShellRequest =
  | { path: Exclude<PathForMethod<"post">, typeof revokePath>; method: "POST" }
  | { path: typeof revokePath; method: "POST"; params: RevokePathParams }
  | { path: PathForMethod<"get">; method: "GET" };

export function createShellApi(transport: ApiTransport) {
  function send(target: ShellRequest, init: Omit<RequestInit, "method">, query = "") {
    const path = "params" in target
      ? target.path.replace("{id}", encodeURIComponent(target.params.id))
      : target.path;
    return transport.request(path + query, { ...init, method: target.method });
  }

  async function request<T>(
    target: ShellRequest,
    init: Omit<RequestInit, "method">,
    isValid: (value: unknown) => value is T,
    query?: string,
  ): Promise<T> {
    const response = await send(target, init, query);
    const payload: unknown = await response.json().catch(() => null);
    if (!response.ok || !isValid(payload)) throw errorFromResponse(response, payload);
    return payload;
  }

  async function mutate(target: ShellRequest, init: Omit<RequestInit, "method">, expectedStatus: number) {
    const response = await send(target, init);
    if (response.status !== expectedStatus) {
      const payload: unknown = await response.json().catch(() => null);
      throw errorFromResponse(response, payload);
    }
  }

  const authorized = (token: string, signal: AbortSignal) => ({
    headers: { Accept: "application/json", Authorization: `Bearer ${token}` },
    signal,
  });

  return {
    login(role: Role, id: string, secret: string, signal: AbortSignal) {
      const body: CustomerLogin | OpsLogin = role === "customer"
        ? { customer_id: id, pin: secret }
        : { operator_id: id, password: secret };
      return request({ path: shellRoutes[role].login, method: "POST" }, {
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal,
      }, (value) => isHumanSession(value, role));
    },
    customerBootstrap(token: string, signal: AbortSignal) {
      return request({ path: shellRoutes.customer.read, method: "GET" },
        authorized(token, signal), isCustomerBootstrap);
    },
    registeredAgents(token: string, signal: AbortSignal) {
      return request({ path: shellRoutes.ops.read, method: "GET" },
        authorized(token, signal), isRegisteredAgents);
    },
    customerMandates(token: string, cursor: string | null, signal: AbortSignal) {
      const query = cursor === null ? "" : `?${new URLSearchParams({ cursor })}`;
      return request({ path: mandatePath, method: "GET" },
        authorized(token, signal), isMandatePage, query);
    },
    async createMandate(token: string, body: CreateMandateBody, signal: AbortSignal) {
      const auth = authorized(token, signal);
      try {
        await mutate({ path: mandatePath, method: "POST" }, {
          ...auth,
          headers: { ...auth.headers, "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }, 201 satisfies keyof operations["createMandate"]["responses"]);
      } catch (error) {
        if (!(error instanceof ApiError) || !body.pin) throw error;
        const pin = body.pin.trim();
        const containsPin = (text?: string) => text !== undefined
          && (text.includes(body.pin) || (pin !== "" && text.includes(pin)));
        throw new ApiError(
          containsPin(error.message) ? "The gateway rejected the mandate request." : error.message,
          containsPin(error.code) ? "UI_REQUEST_FAILED" : error.code,
          containsPin(error.requestId) ? undefined : error.requestId,
          error.status,
        );
      }
    },
    revokeMandate(token: string, id: string, signal: AbortSignal) {
      if (!id || id === "." || id === "..") throw new ApiError("The mandate identifier is invalid.", "UI_INVALID_RESPONSE");
      return mutate({ path: revokePath, method: "POST", params: { id } },
        authorized(token, signal), 200 satisfies keyof operations["revokeMandate"]["responses"]);
    },
  };
}
