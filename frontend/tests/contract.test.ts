import Ajv2020 from "ajv/dist/2020.js";
import { describe, expect, it } from "vitest";
import snapshot from "../../contracts/openapi.json";
import { decisionPath } from "../src/api/approvalWire";
import type { components } from "../src/api/generated/openapi";
import { shellRoutes } from "../src/api/shellWire";
import { demoIds } from "../src/mocks/fixtures";
import { createMockTransport } from "../src/mocks/transport";

const snapshotId = "https://agentgate.invalid/openapi.json";
// Keep the OpenAPI component paths intact so nested local refs resolve.
const ajv = new Ajv2020().addKeyword("components").addSchema({ components: snapshot.components }, snapshotId);

function unauthorizedSchema(responseRef: string) {
  return ajv.compile<components["schemas"]["ErrorEnvelope"]>({
    $ref: `${snapshotId}${responseRef}/content/application~1json/schema`,
  });
}

const customerBootstrapRef = snapshot.paths[shellRoutes.customer.read].get.responses["401"].$ref;

describe("IC0 mock response conformance", () => {
  it.each([
    {
      path: shellRoutes.customer.login,
      init: { method: "POST", body: JSON.stringify({ customer_id: demoIds.customer, pin: "invalid" }) },
      responseRef: snapshot.paths[shellRoutes.customer.login].post.responses["401"].$ref,
    },
    {
      path: shellRoutes.ops.login,
      init: { method: "POST", body: JSON.stringify({ operator_id: demoIds.ops, password: "invalid" }) },
      responseRef: snapshot.paths[shellRoutes.ops.login].post.responses["401"].$ref,
    },
    {
      path: shellRoutes.customer.read,
      init: { method: "GET" },
      responseRef: customerBootstrapRef,
    },
  ])("validates the mock $path 401 against its published schema", async ({ path, init, responseRef }) => {
    const response = await createMockTransport().request(path, init);
    expect(response.status).toBe(401);
    expect(unauthorizedSchema(responseRef)(await response.json())).toBe(true);
  });

  it("validates the mock-only Ops read error against the shared envelope", async () => {
    const response = await createMockTransport().request(shellRoutes.ops.read, { method: "GET" });
    const validate = ajv.compile({ $ref: `${snapshotId}#/components/schemas/ErrorEnvelope` });
    expect(response.status).toBe(401);
    expect(validate(await response.json())).toBe(true);
  });

  it("rejects a malformed mock bootstrap error missing request_id", async () => {
    const response = await createMockTransport().request(shellRoutes.customer.read, { method: "GET" });
    const payload: unknown = await response.json();
    const validate = unauthorizedSchema(customerBootstrapRef);
    if (!validate(payload)) throw new Error("The source mock response must match the published schema.");

    const { code, message } = payload.error;
    expect(validate({ error: { code, message } })).toBe(false);
    expect(validate.errors).toContainEqual(expect.objectContaining({
      instancePath: "/error",
      keyword: "required",
      params: { missingProperty: "request_id" },
    }));
  });
});

it("accepts the three decision bodies and rejects replacement payment fields in the published schema", () => {
  const { schema } = snapshot.paths[decisionPath].post.requestBody.content["application/json"];
  const validate = ajv.compile({ $ref: `${snapshotId}${schema.$ref}` });
  expect(validate({ decision: "APPROVE", pin: "fixture-value" })).toBe(true);
  expect(validate({ decision: "REJECT" })).toBe(true);
  expect(validate({ decision: "REJECT", suspend_agent: true })).toBe(true);
  expect(validate({ decision: "REJECT", amount_paise: 100 })).toBe(false);
  expect(validate.errors).toContainEqual(expect.objectContaining({
    keyword: "additionalProperties", params: { additionalProperty: "amount_paise" },
  }));
});
