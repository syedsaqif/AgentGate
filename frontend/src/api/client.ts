import { errorFromResponse } from "./errors";
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
  | { path: PathForMethod<"post">; method: "POST" }
  | { path: PathForMethod<"get">; method: "GET" };

export function createShellApi(transport: ApiTransport) {
  async function request<T>(
    { path, method }: ShellRequest,
    init: Omit<RequestInit, "method">,
    isValid: (value: unknown) => value is T,
  ): Promise<T> {
    const response = await transport.request(path, { ...init, method });
    const payload: unknown = await response.json().catch(() => null);
    if (!response.ok || !isValid(payload)) throw errorFromResponse(response, payload);
    return payload;
  }

  const read = (token: string, signal: AbortSignal): Omit<RequestInit, "method"> => ({
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
        read(token, signal), isCustomerBootstrap);
    },
    registeredAgents(token: string, signal: AbortSignal) {
      return request({ path: shellRoutes.ops.read, method: "GET" },
        read(token, signal), isRegisteredAgents);
    },
  };
}
