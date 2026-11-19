# D01 API bindings

From `frontend/` in PowerShell:

```powershell
npm ci
npm run api:generate
npm run api:check
```

The checked-in [snapshot](../../../contracts/openapi.json) uses OpenAPI **3.1.0** and contract version **1.0.0**. The pinned `openapi-typescript` generator reads that snapshot and writes [the TypeScript bindings](generated/openapi.ts). Node generates the file and checks drift in memory, so the check also works with local edits and newly generated files. Include the regenerated artifact with the contract change. The lockfile pins the development dependencies, including a scoped Undici update for the generator.

The transport uses generated path and method types and the generated error shape. The snapshot declares the two bearer schemes. The transport still supplies the bearer header from the in-memory session. The frontend reads the generated contract version for the workspace header.

| Endpoint | Snapshot request and auth | Snapshot responses | Provisional fields and owner |
| --- | --- | --- | --- |
| `POST /v1/customer/login` | [OpenAPI](../../../contracts/openapi.json#L22) omits `requestBody` and `security`. [B fragment](../../../contracts/http/b.yaml#L6) specifies unauthenticated, rate-limited login. | `200` session and `429` rate limit are description-only. `401` references the JSON `Unauthorized` response. | **B.** Request `{customer_id, pin}`. Success projection `{session_token, role, expires_at}`, with `role: "customer"`. |
| `POST /v1/ops/login` | [OpenAPI](../../../contracts/openapi.json#L23) omits `requestBody` and `security`. [B fragment](../../../contracts/http/b.yaml#L7) specifies unauthenticated, rate-limited login. | `200` session and `429` rate limit are description-only. `401` references the JSON `Unauthorized` response. | **B.** Request `{operator_id, password}`. Same success projection with `role: "ops"`. |
| `GET /v1/customer/bootstrap` | [OpenAPI](../../../contracts/openapi.json#L24) and the [A fragment](../../../contracts/http/a.yaml#L9) require a Customer bearer session. OpenAPI declares no request body or query parameters. | `200` describes owned accounts, catalog, and public agent cards without a schema. `401` references the JSON `Unauthorized` response. | **A.** Assumes `{accounts, payees, agents}`. Uses `{account_id, masked_label}`, `{payee_id, display_name}`, and `{agent_id, name, vendor}` from the [core DTOs](../../../contracts/core_models.py) and [identity DTOs](../../../contracts/identity_models.py). |
| `GET /v1/ops/agents` | [OpenAPI](../../../contracts/openapi.json#L34) and the [B fragment](../../../contracts/http/b.yaml#L8) require an Ops bearer session. OpenAPI declares no request body or query parameters. | The only declared response is description-only `200` for the registered-agent page. | **B.** Assumes `{items, next_cursor}` with `{agent_id, name, vendor, status}` rows. The shell displays the first page. |

The aliases in [shellWire.ts](shellWire.ts) use the team plan, the checked-in DTOs, and Member B’s unmerged provider implementation at `f4c0542` as provisional references. B’s implementation also returns `session_id` and `subject_id` on login. The A and B HTTP fragments list routes and authentication but supply no additional body schemas for these operations. The A, B, and D fixture files supply scenario and UI examples.

The contract tests resolve the three declared `401` response schemas from the snapshot and validate actual mock responses with Ajv. They also validate the mock-only Ops `401` against the shared `ErrorEnvelope` component and reject a malformed bootstrap error missing `request_id`. The snapshot leaves both login requests, all four `200` bodies, and the two `429` bodies outside schema-validation coverage. Mock validation and unavailable responses (`400` and `503`) also lack declarations on these routes.

Member B needs to publish the two login body schemas, both successful session responses, the rate-limit response contract, and the Ops list and auth-error contract. Member A needs to confirm and publish the bootstrap response and export the agreed changes into the shared snapshot. Live gateway compatibility remains an integration check.
