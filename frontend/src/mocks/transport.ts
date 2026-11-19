import { isRecord, shellRoutes, type Role } from "../api/shellWire";
import type { components } from "../api/generated/openapi";
import type { ApiTransport } from "../api/types";
import {
  customerBootstrap,
  demoCredentials,
  demoSessionTtlMs,
  emptyAgents,
  emptyBootstrap,
  registeredAgents,
} from "./fixtures";

type MockOptions = {
  readScenario?: "populated" | "empty" | "unavailable";
  now?: () => number;
};

type MockSession = { role: Role; expiresAt: number };

export function createMockTransport({ readScenario = "populated", now = Date.now }: MockOptions = {}): ApiTransport {
  const sessions = new Map<string, MockSession>();
  let requestCount = 0;

  return {
    async request(path, init) {
      init.signal?.throwIfAborted();
      const requestId = `req_mock_${++requestCount}`;
      const respond = (payload: unknown, status = 200) => Response.json(payload, {
        status,
        headers: { "X-Request-ID": requestId, "Cache-Control": "no-store" },
      });
      const reject = (status: number, code: string, message: string) => respond({
        error: { code, message, request_id: requestId },
      } satisfies components["schemas"]["ErrorEnvelope"], status);

      const isCustomer = path === shellRoutes.customer.login || path === shellRoutes.customer.read;
      const role = isCustomer ? "customer" : "ops";
      if (init.method === "POST" && path === shellRoutes[role].login) {
        const credentials = readCredentials(init.body, role);
        if (!credentials) return reject(400, "E_REQ_INVALID", "Request validation failed.");
        const seed = demoCredentials[role];
        if (credentials.id !== seed.id || credentials.secret !== seed.secret) {
          return reject(401, "E_AUTH_INVALID_TOKEN", "Invalid sign-in credentials.");
        }

        const timestamp = now();
        for (const [token, session] of sessions) {
          if (session.expiresAt <= timestamp) sessions.delete(token);
        }
        const token = crypto.randomUUID();
        const expiresAt = timestamp + demoSessionTtlMs;
        sessions.set(token, { role, expiresAt });
        return respond({
          session_token: token,
          session_id: crypto.randomUUID(),
          subject_id: seed.id,
          role,
          expires_at: new Date(expiresAt).toISOString(),
        });
      }

      if (init.method !== "GET" || path !== shellRoutes[role].read) {
        return reject(400, "E_REQ_INVALID", "This request is outside the mock shell.");
      }

      const authorization = new Headers(init.headers).get("Authorization");
      if (!authorization) return reject(401, "E_AUTH_MISSING", "A sign-in session is required.");
      const token = /^Bearer (\S+)$/i.exec(authorization)?.[1] ?? "";
      const session = sessions.get(token);
      if (!session || session.role !== role) {
        return reject(401, "E_AUTH_INVALID_TOKEN", "Invalid authentication session.");
      }
      if (session.expiresAt <= now()) {
        sessions.delete(token);
        return reject(401, "E_AUTH_EXPIRED", "Your session has expired. Sign in again.");
      }

      if (readScenario === "unavailable") {
        return reject(503, "E_SERVICE_UNAVAILABLE", "The workspace data is temporarily unavailable.");
      }
      return respond(role === "customer"
        ? (readScenario === "empty" ? emptyBootstrap : customerBootstrap)
        : (readScenario === "empty" ? emptyAgents : registeredAgents));
    },
  };
}

function readCredentials(body: RequestInit["body"], role: Role) {
  if (typeof body !== "string") return null;
  try {
    const value: unknown = JSON.parse(body);
    if (!isRecord(value) || Object.keys(value).length !== 2) return null;
    const id = value[role === "customer" ? "customer_id" : "operator_id"];
    const secret = value[role === "customer" ? "pin" : "password"];
    if (typeof id === "string" && id && typeof secret === "string" && secret) return { id, secret };
  } catch {
    // Return the same validation error for every malformed request body.
  }
  return null;
}
