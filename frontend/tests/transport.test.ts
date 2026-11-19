import { describe, expect, it } from "vitest";
import { createShellApi } from "../src/api/client";
import { createGatewayTransport } from "../src/api/gatewayTransport";
import { shellRoutes, type Role } from "../src/api/shellWire";
import { demoCredentials } from "../src/mocks/fixtures";
import { createMockTransport } from "../src/mocks/transport";

describe("mock authorization boundary", () => {
  it.each([
    { role: "customer", otherRole: "ops" },
    { role: "ops", otherRole: "customer" },
  ] as const)("rejects a $role token on the $otherRole protected route", async ({ role, otherRole }) => {
    const transport = createMockTransport();
    const { id, secret } = demoCredentials[role];
    const { session_token } = await createShellApi(transport).login(role, id, secret, new AbortController().signal);

    const response = await transport.request(shellRoutes[otherRole].read, {
      method: "GET",
      headers: { Authorization: `Bearer ${session_token}` },
    });
    expect(response.status).toBe(401);
    expect(await response.json()).toEqual({
      error: {
        code: "E_AUTH_INVALID_TOKEN",
        message: "Invalid authentication session.",
        request_id: response.headers.get("X-Request-ID"),
      },
    });
  });

  it.each(["customer", "ops"] as const)("requires an Authorization header for the %s read", async (role: Role) => {
    const response = await createMockTransport().request(shellRoutes[role].read, { method: "GET" });
    expect(response.status).toBe(401);
    expect(await response.json()).toMatchObject({ error: { code: "E_AUTH_MISSING" } });
  });
});

describe("gateway configuration", () => {
  it.each([undefined, "http://127.0.0.1:8000/bank", "http://user:password@127.0.0.1:8000"])(
    "rejects invalid configuration %s",
    (origin) => expect(() => createGatewayTransport(origin)).toThrow(expect.objectContaining({
      code: "UI_GATEWAY_CONFIG",
    })),
  );
});
