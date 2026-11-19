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

## D02 Customer mandates

[OpenAPI](../../../contracts/openapi.json#L25) and the [A fragment](../../../contracts/http/a.yaml#L9) put Customer bearer auth on all three mandate routes. The fragment adds PIN verification for creation and ownership for revocation. The [shared core DTOs](../../../contracts/core_models.py), [common primitives](../../../contracts/common.py), [Member A’s test payload](../../../tests/a/test_mandates.py#L31), and team plan §§2.2, 6.1, 6.2, and 6.5 back the provisional mapping in [mandateWire.ts](mandateWire.ts).

| Operation | Published coverage | Provisional mapping and Member A confirmation |
| --- | --- | --- |
| `POST /v1/customer/mandates` | `201` signed immutable mandate is description-only. `400` uses the shared error envelope. The request schema is absent. | Uses A’s flat example `{agent_id, valid_from, valid_until, grants, step_up, risk_profile, pin}`. The review time supplies `valid_from`; each action has one grant with an action-derived `grant_id` scoped to the mandate. The risk profile is `balanced`. Money grants have allow-listed payees, INR and the two paise caps. Reads omit money fields. The gateway supplies ownership, mandate IDs, and signatures. A must confirm the layout, grant ID scope, start-time handling, defaults, and `403 E_ESC_BAD_PIN` shown in the A tests. The UI uses `201` and refreshes the list. |
| `GET /v1/customer/mandates` | `200` own mandate page is description-only. The operation omits query parameters and body schemas. | The shared `{items, next_cursor}` envelope and opaque `cursor` query follow §6.1. Each item projects `{terms, status, usage}` from `MandateSnapshot` and per-grant `UsageSnapshot`, with nullable remaining limits. A must confirm the nesting, pagination parameters, and whether unbounded values are returned as `null`. |
| `POST /v1/customer/mandates/{id}/revoke` | Generated path parameter, description-only idempotent `200`, and JSON `404` error envelope. | A bodyless POST follows §6.5. The UI uses `200` and refreshes the current page for authoritative status. A must confirm gateway behavior, including error codes and a success body if one is published. |

The Customer bootstrap projection optionally reads `jkt` and `max_scopes` from the [public identity DTO](../../../contracts/identity_models.py). A must confirm these fields and the eligible payee catalog. The form only offers recognized scopes supplied by bootstrap and treats a missing fingerprint as unavailable. Caps, count, and optional time windows apply separately to each selected action's grant. The backend remains responsible for authorization and signing.

## D03 Customer approval inbox

The inbox uses the generated Customer list query, decision path and request body. `EscalationDecisionRequest` allows only `decision`, `pin`, and `suspend_agent`. Approve sends the PIN; the reject controls omit it. The Customer bearer session is required for both routes. See [OpenAPI](../../../contracts/openapi.json#L30) and the [Member C HTTP fragment](../../../contracts/http/c.yaml).

| Operation | Published coverage and provisional response mapping |
| --- | --- |
| `GET /v1/customer/escalations?status=PENDING` | The generated query includes opaque `cursor` and optional `limit`. The `200` has no schema. Member C needs to publish `{items, next_cursor}` with the inbox fields projected from `Escalation` and the trusted summary. The UI uses structured payee, masked account, integer amount, risk reasons, and expiry. Agent note text is separate. |
| `POST /v1/customer/escalations/{id}/decision` | The request and the `403` and `409` error schemas are published. The `200` final and `202` processing bodies have no schemas. Member C needs to confirm the decision response fields and their relationship to Member A’s execution result. The UI reads returned approval `state` and nested `result.status`; a valid nested `result.audit_ref` is copyable only when execution is confirmed. |

The provisional response fields are isolated in [approvalWire.ts](approvalWire.ts). Their sources are [Escalation](../../../contracts/escalation_models.py), [C's trusted summary builder](../../../gateway/escalation/summary.py), [C's route implementation](../../../gateway/escalation/routes.py), [ExecutionResult](../../../contracts/core_models.py#L165) and [AuditRef](../../../contracts/common.py#L66). The checked-in decision handler can report an outer `EXECUTED` after calling the execution port. The UI requires the nested execution outcome before presenting an execution receipt.

### Receipt after HTTP 202

The Customer inbox query supports only `status=PENDING`. Its refresh can show absence from the pending set. The two documented single-item reads, `GET /v1/escalations/{id}` and `GET /v1/actions/{request_id}`, require agent DPoP. The team plan §6.8 documents a Customer-filtered `GET /v1/customer/stream` notification with `event_type: bank.executed`, `request_id`, and `data.audit_ref`. It describes best-effort delivery and bounded replay. The OpenAPI stream entry supplies no event schema and the checked-out gateway does not mount a Customer stream. C and A need to publish a Customer-owned final-result and receipt read, or specify how a Customer can recover the receipt after missing the event. D03 retains the processing observation and limits refresh to the pending GET. It never resends a decision for status.

## D04 Ops stream and agent lifecycle

All four Ops routes use the Ops bearer session. Methods, paths, the two mutation path parameters, and their `200` and `404` response keys come from the [OpenAPI snapshot](../../../contracts/openapi.json#L33). D04 has two independent reads so a failed registry request can be retried while the stream is available.

| Owner and operation | Published coverage and provisional mapping |
| --- | --- |
| A — `GET /v1/ops/stream` | [A's HTTP fragment](../../../contracts/http/a.yaml#L14) specifies Ops auth. Team plan §6.8 specifies the SSE ID, event type and JSON envelope, with bounded replay and an explicit resync signal. OpenAPI omits event bodies and the resync discriminator. [opsWire.ts](opsWire.ts) provisionally accepts SSE event `resync` and reads decisions from `data.decision`, optional `data.risk.score`, `data.risk.signals[].message`, and fallback `data.reason_codes`. The decision enum and risk projection come from [common.py](../../../contracts/common.py) and [C's risk DTOs](../../../contracts/risk_models.py). A and C must confirm the decision payload. A and B must confirm lifecycle event names. D04 refreshes agents on `agent.*` or `risk.breaker_tripped`. |
| B — `GET /v1/ops/agents` | OpenAPI omits query parameters and the `200` body schema. The provisional list uses `{items, next_cursor}` and `cursor` from the shared list convention, [AgentPublicIdentity](../../../contracts/identity_models.py), and B's unmerged route at `f4c0542`. B must publish the query and page schema. |
| B with A lifecycle — `POST /v1/ops/agents/{id}/suspend` and `/reinstate` | The [B HTTP fragment](../../../contracts/http/b.yaml#L8) confirms Ops auth. The snapshot omits request and success bodies. Per §6.5 and B's unmerged route, the suspension reason is optional. The UI sends both mutations without a body, treats `200` as acknowledgment, and reads the current status from the registry. B and A must confirm the body and lifecycle error/status contract. |

On disconnect the UI polls the displayed registry page every two seconds and retries the stream with `Last-Event-ID` when it has a bookmark. On reconnect or resync it refetches the registry and deduplicates replayed events. A resync without a bookmark permits resuming from the provider's available events. The stream's bounded replay is the only published way to recover missed feed entries. The registry GET restores current agent status. The snapshot publishes no decision-history read or audit projection that reconstructs missed feed entries, so the UI reports a possible history gap. Cross-origin gateway configuration must allow `Authorization` and `Last-Event-ID`.

## D05 Ops audit

The [OpenAPI snapshot](../../../contracts/openapi.json#L37) and [B HTTP fragment](../../../contracts/http/b.yaml#L11) require an Ops bearer session for all three GETs. Their successful response schemas are absent. The provisional projections in [auditWire.ts](auditWire.ts) use [B's checked-in audit DTOs](../../../contracts/audit_models.py) and B's unmerged provider at `f4c0542`. B owns the HTTP schemas and verification semantics.

| Operation | Published coverage and provisional fields |
| --- | --- |
| `GET /v1/audit/entries` | Generated `cursor` and `limit` parameters. The provisional exact-match filters `event_type`, `request_id`, `customer_id`, and `agent_id` come from B's `gateway/audit/routes.py` and `append.py` on the provider branch. A redacted `{items, next_cursor}` page follows the common Page DTO. Row metadata uses `seq` from B's audit store and `event_type`, `request_id`, and `occurred_at` from `AuditEvent`. Member B must publish the filters and redacted row/page schema. |
| `GET /v1/audit/verify` | Description-only `200`. The provisional `VerifyReport` uses `valid`, `entries_checked`, `head_seq`, `head_hash`, `anchored_through_seq`, and nullable/optional `first_failure` with `seq` and `reason`. B's verifier starts at sequence one. The UI shows a contiguous checked range only for a successful report whose checked count equals the reported head. B must publish the report, including exact checked range and anchor semantics for failures if needed. |
| `GET /v1/audit/evidence/{request_id}` | Generated path parameter and `404` error envelope. The provisional bundle uses B's `EvidenceBundle` fields for the historical mandate/action/agent snapshots, signed proof, decisions and risk, approval history, bank outcome, audit references, and verification. The UI checks the returned request ID and the section types, then shows and downloads the original provider JSON. B must publish the bundle schema and confirm redaction at the HTTP boundary. |

The verification UI explains which prefix has retained checkpoint coverage and which suffix passed internal checks. Transport errors show verification as unavailable. Hashing, signature verification, redaction, and tamper tooling belong to the provider. B's S10/S11 provider tests on the unmerged branch are fixture references for D05. Live provider and real tamper verification remain integration work.
