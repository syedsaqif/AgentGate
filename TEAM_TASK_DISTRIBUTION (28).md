# AgentGate — Team Task Distribution

**Document:** `TEAM_TASK_DISTRIBUTION.md`  
**Prepared:** 16 November 2026  
**Team:** Member A, Member B, Member C, Member D  
**Build baseline:** 24–36 hours; four parallel tracks; local demonstration.

## 1. What this distribution is based on

This document follows the supplied **“Cybersecurity: Securing Banks Against Rogue AI Agents”** problem statement and the complete **AgentGate Research & Development Plan v1.0**. Both supplied copies, `RESEARCH_AND_DEVELOPMENT_PLAN.md` and `RESEARCH_AND_DEVELOPMENT_PLAN(1).md`, contain the same 1,311-line plan. The source links are in Section 15.

The problem asks for at least two security capabilities; the approved plan deliberately includes **all five**. This distribution preserves them: scoped delegation, agent authentication, behavioural anomaly detection, human approval, and tamper-evident audit. It also retains the mock bank, Python agent SDK, deterministic scenario runner, and two React screens.

**Recommended split:** keep the plan’s four specialist tracks, but specify every connection between them before implementation. A owns enforcement and execution; B owns identity and evidence; C owns risk and approvals; D owns the human interface and demo experience. Integration remains a team responsibility, coordinated by A.

**Important distinction:** runtime dependencies are unavoidable. Development dependencies are removed with frozen data contracts, injected interfaces, fixtures, and test doubles. A successful Git merge alone is never evidence that the application works.

This is an implementation assignment and acceptance specification. The repository paths, commands, schemas, and test targets below are deliverables to create; they are not claims that a repository or working application already exists.

## 2. Scope and decisions to freeze

### 2.1 Build boundary

| Area | Commitment for this build |
|---|---|
| Gateway | One Python 3.12 FastAPI application, divided into in-process modules. Do not create a network microservice per teammate. |
| Database | SQLAlchemy 2, SQLite WAL, one gateway worker. One gateway database; a separate database for the mock bank. |
| Client | React, Vite, TypeScript; Customer and Ops routes. Use the plan’s UI libraries only where they save time. |
| Money and time | Strict integer paise, INR only; UTC timestamps internally; Asia/Kolkata for displayed times and demo time windows; injected clock. |
| Agent authentication | Registered Ed25519 keys, short-lived key-bound JWTs, DPoP-style per-request proofs, replay protection. Explicitly a project profile, not an interoperability certification. |
| Risk | Deterministic rules and statistics, explainable reasons, circuit breaker. ML remains optional. |
| Bank | Simulated balances, billers, payments, transfers, and transaction history. Only the gateway can execute bank operations. |
| Demo | Local, deterministic, no paid service or internet required; scripted prompt-injection scenario. |
| Excluded | Real UPI/bank integration, KYC/AML, mobile app, regulatory certification, external identity provider, microservice platform, new product features. |

### 2.2 Explicit resolutions of ambiguities in the source plan

These are compatibility decisions, not extra product scope. They must be reflected in fixtures and tests.

| Ambiguity | Frozen resolution |
|---|---|
| Two action-hash definitions, in §3.4 and Appendix B | Use Appendix B’s richer projection: include `intent.task` and `intent.category`, exclude only `intent.note`; Section 6.3 defines the exact projection. Everyone imports the same implementation and golden vectors. |
| Approval for a payee outside the allow-list | An unlisted payee is a hard DENY. “New payee” step-up means an **allow-listed but never previously paid** payee. Approval cannot widen a mandate. |
| Bank timeout described as a failure that releases budget | Release only after a confirmed rejection or proof that execution did not occur. A timeout with unknown outcome retains the reservation and is resolved using the original bank idempotency key. |
| Evidence is P0 in the audit section but P1 in the backlog | Keep a **minimal JSON evidence bundle, S11, and a basic UI detail/download** in the committed scope. Printable HTML and rich dispute visualizations remain optional. |
| Calendar-month budgets appear both as a grant option and as stretch | P0 supports `window="mandate"` only. Reject other windows explicitly; calendar-month rollover is P2. Keep expiry, caps, count limits, and optional day/hour constraints. |
| Identity failures are sometimes all 401, sometimes 403 | Invalid credentials/proofs are 401; cryptographically valid callers with suspended/revoked/expired authority get the specific 403 state codes. Detailed ordering is in Section 6.4. |
| “Score every request,” including unauthenticated traffic | Score every schema-valid, authenticated action attempt, including policy denials. Audit and rate-limit invalid/auth-failed traffic separately; never attribute an attacker’s claimed agent ID as a verified identity. |
| Safe stubs versus permissive examples in the plan | Use controlled, explicit fixtures. No auto-approve or allow-all substitute is permitted in the real demo profile. Contract compliance does not make a fake implementation secure. |
| Contracts/OpenAPI arrive only after the gateway exists | Publish a contract-only API description and fixtures at IC0, before real backend logic. D starts with those; later compare the real FastAPI OpenAPI export against them. |
| “Four approvals” on a four-person team | All four agree; the PR author signs off in the PR description and the other three approve. Do not require the author to submit an impossible self-approval. CODEOWNERS alone does not enforce unanimity. |
| Customer suspension versus a shared registered agent | Customer safety actions suspend that customer’s delegation; ops and the trusted breaker may suspend the registered agent globally. This preserves cross-customer isolation. |

### 2.3 Non-negotiable security properties

1. The agent reaches bank operations only through the gateway.
2. Identity, current agent status, current mandate status, ownership, scope, expiry, and limits are checked before execution.
3. Risk can restrict an action but cannot override a hard policy denial.
4. Approval authorizes one immutable stored action and cannot bypass revocation or exhausted budget.
5. Every execution has a committed audit decision before the bank is contacted.
6. Concurrent requests cannot exceed money/count caps or spend the same idempotent payment twice.
7. Customer, agent, and ops authentication are separate; an agent cannot grant itself authority or approve itself.
8. Sensitive values—PINs, private keys, raw access/session tokens—never appear in audit data, ordinary logs, fixtures, or Git.

## 3. Balanced member responsibilities

### 3.1 Ownership summary

| Member | Owns | Concrete final deliverables | Does not own |
|---|---|---|---|
| **A — Enforcement and integration** | Gateway assembly, F1 mandates and policy, decision combiner, execution, atomic budgets, idempotency, mock bank, event transport, lifecycle coordination | Working gateway, pure policy evaluator, gateway/bank persistence, private bank adapter, startup/reset commands, integration wiring | JWT/proof internals, risk formulas, approval state machine, React components |
| **B — Identity and audit** | F2 registry/authentication/tokens, customer/ops sessions and PIN checking, Python SDK, F5 signed audit chain, verification/checkpoints, JSON evidence | Auth routes and ports, SDK CLI/client, signed audit store, evidence API, safe tamper demonstration | Mandate policy, payment execution, risk calculation, UI layouts |
| **C — Risk and approval workflow** | F3 signals/baselines/breaker recommendation, F4 escalation/summary/expiry/decision state, synthetic histories, scenario runner | Explainable risk engine, persistent approval workflow, deterministic scenarios S1–S11, basic metrics summary | Direct bank calls, token generation implementation, frontend, root app wiring |
| **D — Customer/Ops experience and demo** | All frontend, API mocks/generated TS client, approval UI, audit/evidence UI, UI verification, README/demo script/backup recording | Two functional screens, mock and real modes, visible receipts and status, reproducible demo guide | Backend authorization decisions, server-side PIN checks, security algorithms |

**No owner is “just integration” or “just documentation.”** Every member builds a substantial product component, owns its tests, and participates in the final verification.

### 3.2 Workload budget

Estimates are planning allowances, not promised completion times. They assume familiarity with the chosen stack, basic reusable UI components, and no optional work before the integrated core passes.

| Work category | A | B | C | D |
|---|---:|---:|---:|---:|
| Contracts, local harness, own fakes | 2 h | 2 h | 2 h | 2 h |
| Main implementation | 12 h | 12 h | 12 h | 12 h |
| Own unit/security/UI tests and fixes | 3 h | 3 h | 3 h | 3 h |
| Integration, rehearsal, final fixes | 4 h | 4 h | 4 h | 4 h |
| **Target effort** | **21 h** | **21 h** | **21 h** | **21 h** |

Main-work allocation: A = policy/mandates 4 h, execution/budgets 4 h, bank/assembly/events 4 h; B = auth/human sessions 5 h, SDK 2 h, audit/evidence 5 h; C = risk 4 h, escalation 4 h, scenario data/runner 4 h; D = customer UI 4 h, ops UI 4 h, client/mocks/demo preparation 4 h.

Balance is by difficulty and support burden, not file count. A’s integration role is limited to wiring and coordination: B fixes identity/audit defects, C fixes risk/approval defects, and D fixes UI defects. D prepares the demo while others finish security tests. B and C work on their independent submodules when a local issue stalls one task. If behind, reduce visual polish and optional analysis first; never transfer unreviewed security ownership at the end.

Use the 36-hour schedule if any member is learning the stack or applied cryptography during the event. The 24-hour version is ambitious and depends on the existing plan’s local-only, rules-first and minimal-UI limits. Reserve integration time before assigning optional work.

### 3.3 Feature-to-owner and acceptance mapping

| Requirement from the plan | Backend owner | UI/support owner | Verification |
|---|---|---|---|
| F1.1 mandate creation, immutable terms, actions/payees/accounts/caps/count/expiry | A; B supplies PIN/session verification | D | S1; invalid creation and ownership tests |
| F1.2 per-request scope enforcement | A | D displays coarse/user-facing reasons | S2a–c |
| F1.3 concurrent cumulative caps | A | C drives parallel scenarios | S3 and 100 randomized race runs |
| F1.4 expiry, optional permitted hours/days | A | C supplies clock fixtures | S4 and time-boundary tests |
| F1.5 instant revoke and cancellation | A coordinates; B revokes tokens; C cancels approvals | D revoke control | S5 plus revoke/dispatch race |
| F2.1 registration, F2.2 key proof, replay protection | B | C attack client scenarios | S6a–g |
| F2.3 customer/agent/mandate attribution | B authenticates; A binds action | D identity card | S1, S11, cross-customer tests |
| F2.4 suspension/disable/reinstatement | B state owner; A coordinates side effects | D kill switch; C breaker signal | S5, S7, S8 |
| F3.1 baselines and scoring denied attempts | C | A supplies verified action/history DTOs | S7, S8, denied-attempt counter tests |
| F3.2 reasons/bands and F3.4 intent mismatch | C | D risk display | Signal tests, S7–S8 |
| F3.3 circuit breaker | C detects; A invokes lifecycle; B stores suspension | D shows status | S2d, S7, S8 |
| F4.1–F4.3 triggers, hold, PIN decision, action binding, single use, TTL | C; A alone executes; B verifies human identity/PIN | D approval inbox | S9a–e, revoke/expiry race |
| F5.1 event coverage | Every module emits its events through B’s audit port | D live view | Audit assertions in every scenario |
| F5.2 signed chain, append-only protections, checkpoints | B | D verify result | S10a–d, failure injection |
| F5.3 dispute evidence | B assembles recorded evidence | D basic detail/JSON download | S11 |
| Supporting mock bank, SDK, simulation | A / B / C respectively | D demo guide | Full real-stack suite |

## 4. Repository layout and exclusive file ownership

Use one monorepo. The table defines the folder structure without implying separate services for each module. Every new file inherits its nearest owning directory unless explicitly listed otherwise.

| Path | Owner | Contents |
|---|---|---|
| `contracts/common.py` | A, frozen after IC0 | IDs, enums, error envelope, clock/transaction abstractions, shared primitive types |
| `contracts/core_models.py`, `contracts/core_ports.py` | A, frozen | Action, mandate, grant, policy, reservation, bank, lifecycle, catalog and execution ports |
| `contracts/identity_models.py`, `contracts/identity_ports.py` | B, frozen | AuthContext, sessions, tokens, registry, verification ports |
| `contracts/audit_models.py`, `contracts/audit_ports.py` | B, frozen | AuditEvent, AuditRef, VerifyReport, evidence bundle and audit port |
| `contracts/risk_models.py`, `contracts/risk_ports.py` | C, frozen | Risk inputs/results, history DTOs, risk port |
| `contracts/escalation_models.py`, `contracts/escalation_ports.py` | C, frozen | Approval DTOs, state enum, escalation port |
| `contracts/schemas.py`, `contracts/interfaces.py` | A, frozen | Re-export public contracts for compatibility with the original plan; no domain logic |
| `contracts/canonical.py`, `contracts/reason_codes.py` | B, frozen | One hashing projection, reason/status registry; all members review |
| `contracts/http/a.yaml`, `contracts/http/b.yaml`, `contracts/http/c.yaml` | A / B / C respectively, frozen | Endpoint fragments, auth requirements, example request/response schemas |
| `contracts/openapi.json` | Generated; A owns generator | Contract-only API snapshot at IC0, compared with real API at integration |
| `contracts/fixtures/a/`, `b/`, `c/`, `d/` | Corresponding member, frozen | Golden scope/race, auth/audit, risk/approval, and UI examples |
| `gateway/main.py`, `deps.py`, `pipeline.py`, `config.py`, `errors.py` | A | Composition root, dependency injection, pipeline and error handlers |
| `gateway/platform/` | A | DB/session factory, clock, startup, lifecycle coordinator, health/profile checks |
| `gateway/mandates/` | A | `models.py`, `repository.py`, `service.py`, `policy.py`, `routes.py`, `schema.py` |
| `gateway/executor/` | A | `actions.py`, `idempotency.py`, `reservations.py`, `service.py`, `bank_client.py`, `schema.py` |
| `gateway/events/` | A | `bus.py`, `sse.py`, role-filtered stream routes |
| `gateway/identity/` | B | `registry.py`, `tokens.py`, `proofs.py`, `replay.py`, `human_auth.py`, `routes.py`, `models.py`, `schema.py` |
| `gateway/audit/` | B | `append.py`, `verify.py`, `checkpoints.py`, `evidence.py`, `routes.py`, `models.py`, `schema.py` |
| `gateway/risk/` | C | `features.py`, `signals.py`, `scoring.py`, `history.py`, `breaker.py`, `models.py`, `schema.py` |
| `gateway/escalation/` | C | `service.py`, `summary.py`, `expiry.py`, `routes.py`, `models.py`, `schema.py` |
| `bank_mock/` | A | Separate FastAPI app, database, catalog seed, service auth, idempotent ledger |
| `agent_sdk/` | B | Key generation, request serialization/proofs, token fetch, client/retry/poll helpers, CLI |
| `simulator/` | C | `personas.py`, `history_seed.py`, `client_port.py`, `runner.py`, `scenarios/s01.py` through `s11.py` |
| `frontend/src/api/`, `frontend/src/mocks/` | D | Generated client, transport adapters, stateful mock handlers from fixtures |
| `frontend/src/pages/customer/`, `pages/ops/`, `components/` | D | Customer/Ops views, identity/approval cards, status, audit and evidence widgets |
| `frontend/` remaining files | D | Frontend package manifest and lockfile, styles, tests, build configuration |
| `tests/a/`, `tests/b/`, `tests/c/` | A / B / C | Each owner’s unit, contract, integration-adapter and security tests |
| `tests/integration/test_pipeline.py`, `test_budget_races.py`, `test_failures.py` | A | Cross-module flow, DB concurrency and execution failure tests |
| `tests/integration/test_auth_audit.py` | B | Real proofs, auth domains, audit ordering and tamper tests |
| `tests/integration/test_scenarios.py`, `test_approval_races.py` | C | Full scenario runner and approval lifecycle races |
| `frontend/tests/` | D | Mock UI checks and a real-backend approval/revoke/verify smoke path |
| `tests/conftest.py`, `tests/support/platform.py` | A | Only global test setup; no owner-specific fixtures here |
| `tests/support/a/`, `b/`, `c/` | Corresponding member | Own fakes, fixture factories, isolated service harnesses |
| `tools/dev.py`, `seed.py`, `reset.py`, `export_openapi.py` | A | Cross-platform start/reset/schema setup and API contract export |
| `tools/tamper.py`, `tools/verify_vectors.py` | B | Isolated tamper copies and canonical hash/signature checks |
| `tools/metrics.py` | C | Scenario outcomes, detection/false-positive counts, timing summary |
| `docs/a/`, `docs/b/`, `docs/c/`, `docs/d/` | Corresponding member | Module notes and handoff examples |
| `docs/DEMO.md`, `docs/TEST_RUN.md`, `README.md` | D | Demo instructions, collected verification evidence, user setup guide |
| `docs/CONTRACT_CHANGES.md` | A, all-member sign-off | Compatibility decisions and contract versions |
| `.github/workflows/a.yml`, `b.yml`, `c.yml`, `d.yml` | Corresponding member | Owner-specific checks |
| `.github/workflows/integration.yml` | A | Full suite on proposed merge commit and default branch |
| `.github/CODEOWNERS` | A, all-member review | Actual GitHub handles substituted at kickoff |
| `pyproject.toml`, `uv.lock`, `config.yaml`, `.env.example`, `.gitignore`, `Makefile` | A, serialized changes | Shared Python dependencies, runtime flags, commands |

### 4.1 Rules that prevent file and database conflicts

- Do not put all SQLAlchemy models or all tests in one shared file. Module `schema.py` files register their own tables; A invokes them from the composition root in a documented order.
- Gateway table write ownership: A = `mandates`, `mandate_usage`, `reservations`, `actions`, `idempotency_records`; B = `customers`, `human_sessions`, `agents`, `issued_tokens`, `audit_log`; C = `escalations`, `risk_attempts`, `risk_baselines`. A owns the bank’s accounts, payees, ledger, and bank idempotency records in the separate bank database.
- Other modules consume typed DTOs and ports. They must not import a neighbour’s repository, mutate a neighbour’s table, or share ORM objects across boundaries.
- Shared DB transactions can span ports through an injected `Tx` context. Table ownership remains with the service receiving the call.
- Schema changes live beside the owning module. During initial development, use fresh disposable databases and a versioned schema manifest; do not commit database files or create competing global migration heads. After integration freeze, any schema change needs an explicit upgrade/reset note.
- Root Python dependency changes go through A. D alone updates the frontend manifest/lockfile. Do not hand-edit either lockfile to resolve conflicts.
- Keep `.env`, private keys, SQLite/WAL files, `node_modules`, runtime checkpoints, and recordings out of Git. Commit seed recipes, public keys/test vectors, and fixture definitions only. Any test private key must be clearly test-only and unusable in the demo profile.

## 5. Start immediately: parallel setup and mocks

### 5.1 First two hours

All four start at the same time. The shared kickoff is a short compatibility exercise, not a wait for A to finish the backend.

| Time | A | B | C | D |
|---|---|---|---|---|
| 0–20 min | Create repository skeleton and command manifest; copy common contract decisions | Prepare key/proof and audit test vectors; review identity/hash decisions | Prepare risk/approval fixtures and state transitions | Scaffold frontend; build initial Customer/Ops shell and fixture transport |
| 20–75 min | Implement A contract files, fake bank, clock and policy harness | Implement B contract files, fakes, crypto tests | Implement C contract files, risk/escalation fakes and scenario client abstraction | Build UI mocks from the specified envelopes; implement currency/time adapters |
| 75–120 min | Assemble contracts and contract-only OpenAPI export | Validate body/hash/proof vectors and B schemas | Validate decision/state fixtures and C schemas | Generate TS types/client and validate mock responses |
| IC0 | All agree on contract v1, merge the small bootstrap, then continue independently | Same | Same | Same |

An isolated pure function, SDK serializer, mock handler, or page can be developed from this document while the shared files are being assembled. Once IC0 lands, import the shared contracts and remove temporary local copies. Do not allow temporary duplicate schemas to survive into integration.

### 5.2 Substitute table

| Consumer’s runtime dependency | Substitute while provider is unfinished | Required compatibility check |
|---|---|---|
| A needs B authentication | `FixtureIdentityVerifier` returns a predetermined AuthContext or a selected auth error | Same return schema and exception mapping; real verifier additionally passes cryptographic security tests |
| A/C need B human auth | `FixtureHumanAuth` with explicit customer/ops identities and PIN outcomes | Ownership, role and PIN-success/failure contract cases |
| A/C need B audit | `MemoryAuditLog` with append/query and an injected failure switch | Envelope/reference shapes and failure propagation; real store alone proves signatures, persistence and tamper detection |
| B needs A mandate state | `FixtureMandateReader` returns active/revoked/expired/bound-to-other-agent snapshots | Same DTOs; live adapter tested with A’s real service |
| C needs A action/history/catalog | `FixtureActionReader`, trusted seeded payee registry, deterministic clock | Never read another module’s SQL tables directly |
| A needs C risk | `FixtureRiskEngine` returns low/medium/high/error from selected test cases | Same result schema, reason names and fallback behavior |
| A needs C approvals | `MemoryEscalationStore` with pending/reject/expire/consume transitions | No automatic approval; live version adds transaction/race tests |
| C needs A executor | `RecordingExecutionPort` records a request to execute the stored action and returns a selected result | Proves C does not call the bank or substitute new parameters |
| C needs B SDK | `ScenarioClient` fake responding from fixtures | Switch adapter to B’s SDK without rewriting scenarios; full suite cannot pass on the fake |
| D needs backend HTTP/SSE | Stateful mock HTTP handlers plus a mock event adapter | Validate responses against the same schemas, including errors and terminal states |
| Anyone needs time | `FrozenClock.advance(seconds)` | UTC-aware, no sleeping, expiry and boundary cases repeatable |

Mocks exercise interface behavior, not the security guarantee of a real dependency. Each fake must include negative cases: revoked authority, audit unavailable, pending/expired approval, conflict and bank failure. “Always success” is insufficient.

### 5.3 Proposed developer commands

A supplies one cross-platform Python launcher so Windows members do not require GNU Make. The plan’s Makefile remains a convenience wrapper. Each command prints its active profile and component providers.

```text
uv sync --frozen
uv run python -m tools.dev check-contracts
uv run python -m tools.dev service a --profile test
uv run python -m tools.dev service b --profile test
uv run python -m tools.dev service c --profile test
uv run pytest tests/a
uv run pytest tests/b
uv run pytest tests/c
uv run python -m tools.dev reset --profile scripted
uv run python -m tools.dev stack --profile real
uv run python -m simulator.runner --all --profile scripted
```

From `frontend/`, D supplies `npm ci`, `npm run dev:mock`, `npm run dev:real`, `npm run typecheck`, `npm test`, and `npm run build`. Equivalent root wrappers: `make test`, `make e2e`, `make reset`, `make demo`.

Profiles: `test` permits injected fakes; `scripted` uses **real security modules** plus seeded data/frozen-clock controls; `real` uses real modules, enforcement enabled, and disables reset/clock/tamper HTTP routes. Startup must refuse `real` if identity, audit, policy, bank auth or approvals are replaced by fakes. `risk.mode=shadow` is allowed only in the isolated S3 fixture, not the final demo run.

## 6. Shared contracts and integration rules

### 6.1 Common schema rules

- Contract version starts at `1.0.0`; backend, SDK, simulator and frontend record it in their build metadata.
- Pydantic request models reject unknown fields. Integer money rejects booleans, floats, numeric strings, negative values and zero for money-out actions. Read requests have no money amount.
- IDs are opaque non-empty strings. All serialized timestamps are UTC ISO-8601 ending in `Z`; convert to IST only for display or explicit time-window evaluation.
- Currency is exactly `INR`. UI converts decimal rupee strings to paise using string/integer arithmetic, not floating-point multiplication.
- Enums are case-sensitive: actions `bill_pay|transfer|balance_read|transactions_read`; policy `ALLOW|STEP_UP|DENY`; final decision `ALLOW|ESCALATE|DENY`; risk `LOW|MEDIUM|HIGH`.
- Lists return `{items: [...], next_cursor: string|null}`. Use an opaque cursor and a bounded `limit` (default 50, maximum 100). Do not invent separate list envelopes per endpoint.
- One error envelope: `{error: {code, message, request_id}}`. The server generates `request_id`; an incoming correlation hint never substitutes for a trusted action ID.
- `reason_codes` are stable identifiers; human-readable messages are presentation data. Never parse a message string to control logic.
- Responses include `X-Request-ID`. An executed action also includes `audit_ref: {seq, entry_hash}`. A failure to persist its final receipt is not reported as a fully completed success.

### 6.2 DTO inventory

These fields define the boundary; owners may add private implementation fields inside their own modules.

| DTO | Required content |
|---|---|
| `AuthContext` | `customer_id`, `agent_id`, `mandate_id`, `mandate_hash`, `jkt`, `token_jti`, `proof_jti`; created only by verified identity code |
| `HumanContext` | `session_id`, `subject_id`, `role` (`customer`, `ops` or `registrar`), `expires_at`; verified by B; customer ID derived from session |
| `Grant` | `grant_id`, `action`, non-empty `account_ids`; money-out additionally requires non-empty `payee_ids`, `currency`, `max_per_txn_paise`, `max_total_paise`, `max_count`, `window="mandate"`; optional `category` and `time_window` |
| `TimeWindow` | `timezone="Asia/Kolkata"`, ISO weekdays 1–7, `start_local` and `end_local` (`HH:MM`); start inclusive/end exclusive; overnight intervals rejected in MVP to avoid ambiguous rules |
| `MandateTerms` | `mandate_id`, `version`, `customer_id`, `agent_id`, `valid_from`, `valid_until`, `grants`, `step_up`, `risk_profile`; immutable |
| `MandateSnapshot` | Terms plus `status`, `mandate_hash`, `sig`, `key_id`; status is mutable and excluded from signed terms |
| `StepUpRules` | `amount_above_paise`, `new_payee_requires_approval` (default true), `always_ask_actions` |
| `UsageSnapshot` | Per `(mandate_id, grant_id, window_key)`: committed/reserved paise and counts, remaining amount/count |
| `CanonicalAction` | Fields in Section 6.3; server fills authority fields, stores the original intent note separately as unverified text |
| `PolicyResult` | `verdict`, `reason_codes`, `matched_grant_ids`, `step_up_reasons`, `usage_snapshot`; never mutates usage |
| `RiskInput` | AuthContext, action, policy result, trusted mandate/catalog/history snapshots, `now`, `attempt_id`; includes the current attempt exactly once |
| `RiskResult` | `score` 0–100, `level`, `recommended`, `signals[{name,severity,weight,reason_code,message}]`, `breaker_recommended`, `breaker_reasons`, `model_version` |
| `Reservation` | `reservation_id`, `request_id`, all affected grant/window keys, paise/count, state `RESERVED`, `COMMITTED`, `RELEASED` or `UNKNOWN` |
| `BankResult` | `status` is `EXECUTED`, `REJECTED` or `UNKNOWN`; stable `bank_operation_id` if known, `bank_idempotency_key`, optional reason/balance, `executed_at` if executed |
| `Escalation` | `escalation_id`, `request_id`, ownership IDs, immutable action/hash, trusted summary, reason codes, `expires_at`, state, decision metadata, optional result/audit reference |
| `ExecutionResult` | Action ID, `status`, bank result if known, `audit_ref`, optional `escalation_id`; no raw tokens or detailed risk data for the agent |
| `AuditEvent` | Event type, request/correlation IDs, trusted actor and authority IDs if known, mandate hash, decision/reasons/risk, safe payload, UTC timestamp |
| `VerifyReport` | `valid`, `entries_checked`, `head_seq`, `head_hash`, `anchored_through_seq`, `first_failure` (`null` or `{seq,reason}`) |
| `EvidenceBundle` | `request_id`, mandate snapshot, agent public identity, signed request evidence, decisions/risk, approval history, bank outcome, audit references, verification report |

Creation takes the **terms the customer can choose**, plus PIN. It does not accept client-selected `status`, signature, hashes, or customer identity. A validates the registered agent’s capability ceiling, account ownership, payee existence, validity interval, thresholds and caps. The server generates mandate IDs/version/hash/signature. No in-place widening endpoint is needed: revoke and create a new immutable mandate; keep the old snapshot for evidence.

For overlapping grants of the same action, use the intersection of constraints and reserve against every applicable grant; never choose the most permissive grant. The MVP UI creates one grant per action, but the backend must reject ambiguity or enforce this intersection consistently. Reads also require an allowed account and action; count limits, if specified, apply to reads with a zero-money reservation.

### 6.3 Exact action and hashing rules

Agent bill-payment request:

```http
POST /v1/payments/bills
Authorization: DPoP <access-token>
DPoP: <signed-proof-jwt>
Idempotency-Key: bill-nov-001
Content-Type: application/json
```

```json
{
  "from_account": "ACC-001",
  "payee_id": "BILL-ELEC",
  "amount_paise": 480000,
  "currency": "INR",
  "reference": "NOV-2026",
  "intent": {
    "task": "pay_electricity_bill",
    "category": "utilities.electricity",
    "note": "Monthly bill"
  }
}
```

The body never supplies `customer_id`, `agent_id`, `mandate_id`, `mandate_hash` or a substitute approval. Transfers use the same parameter shape with `action_type="transfer"` inferred from the route. Balance/transaction reads use the account in the path and a bounded transaction-history query; the server creates their canonical read action.

Frozen bill-payment hash projection:

```json
{
  "customer_id": "cust_001",
  "agent_id": "agt_billpal",
  "mandate_id": "mnd_demo_1",
  "mandate_hash": "sha256:<actual-mandate-terms-digest>",
  "action_type": "bill_pay",
  "params": {
    "from_account": "ACC-001",
    "payee_id": "BILL-ELEC",
    "amount_paise": 480000,
    "currency": "INR",
    "reference": "NOV-2026"
  },
  "intent": {
    "task": "pay_electricity_bill",
    "category": "utilities.electricity"
  },
  "idempotency_key": "bill-nov-001"
}
```

`action_hash = "sha256:" + lowercase_hex(SHA256(JCS(projection)))`. JCS means RFC 8785 canonical JSON. The gateway stores the complete normalized action and note, then uses the stored immutable projection for approval and execution. Changing task/category/amount/account/payee/reference/key changes this hash. The unverified note is excluded from authority hashing but remains covered by the signed raw-body digest and idempotency request fingerprint.

`mandate_hash` hashes exactly `MandateTerms`, excluding mutable status, counters, `mandate_hash`, `sig` and `key_id`; sign the digest using the mandate issuer’s separate Ed25519 key. Define optional fields with explicit schema defaults before canonicalization; do not mix omission and `null` across languages. B’s golden vectors cover reordered JSON keys, Unicode, note-only changes and single-paise changes.

**Three different hashes serve different purposes:**

| Hash | Input | Purpose |
|---|---|---|
| Proof `bsh` | Exact transmitted UTF-8 body bytes; empty bytes for bodyless GET | Detect body changes in transit; SDK serializes once, signs those bytes and sends those same bytes |
| Request fingerprint | JCS of validated route/method/body including the note | Detect changed-body reuse of the same idempotency key |
| Action hash | Immutable authority/action projection above | Bind human approval and execution to the same action |

Never have the frontend reimplement authority hashing. It displays the server’s hash/reference; B’s SDK signs bytes, and A/C import the common canonical module.

### 6.4 Identity and human authentication contract

B implements the plan’s DPoP-style profile using reviewed crypto libraries. Project-specific choices are fixed at IC0:

- Ed25519/`EdDSA` only for MVP keys and signatures; reject other algorithms, `none`, symmetric algorithms and embedded private JWKs. ES256 interoperability is deferred.
- Token claims: `iss=agentgate`, `aud=agentgate-agent-api`, `sub=customer_id`, `act.sub=agent_id`, mandate ID/hash, `cnf.jkt`, `iat`, `exp`, `jti`. TTL at most 600 seconds and never beyond mandate expiry.
- Proof header: `typ=dpop+jwt`, `alg=EdDSA`, public `jwk`. Proof claims: `htm`, `htu`, `iat`, unique `jti`, `bsh`; resource requests also require `ath`.
- `htu` uses the configured trusted gateway origin and actual route, excluding query and fragment, following RFC 9449. Ignore spoofed forwarded headers on local direct requests. Authorization of transaction query parameters still happens server-side.
- `ath` and `bsh` are unpadded base64url SHA-256 digests. `ath` hashes the raw access-token string; `bsh` is the project’s body-binding extension.
- Token request is project JSON `{client_id, mandate_id}` with a proof and `bsh`, but no `ath` because it has no access token yet. Do not call this a full OAuth authorization-server implementation.
- Proof time tolerance is ±60 seconds. Atomically claim `(registered_jkt, proof_jti)` after cryptographic validation; retain it through the end of its acceptance window, including future-skew allowance. Concurrent reuse must have exactly one winner.
- Each retry gets a **new proof** while keeping the original payment idempotency key. An expired token may be refreshed only while current authority remains valid.
- After token/proof validity and identity binding are proven, check live agent and mandate state. A valid old token for a revoked mandate returns `E_MANDATE_REVOKED`; suspended agent returns `E_AUTH_AGENT_SUSPENDED`; otherwise revoked token returns `E_AUTH_TOKEN_REVOKED`.
- Unknown agent and invalid registration proof have an indistinguishable public response. Record the precise internal reason without trusting a supplied identity as verified.

This preserves RFC 9449’s key, method, URI and token binding; `bsh` and the JSON token exchange are explicitly local profile choices. See the RFC in Section 15.

B also implements seeded customer/ops login, separate session audiences and signing keys, argon2 PIN verification, expiry, and basic failed-login/PIN throttling. A and C call `HumanAuth` instead of handling PIN hashes themselves. For the local React demo use an Authorization-bearing fetch transport and **in-memory** human session tokens; do not place tokens in URLs or localStorage. Frontend reload requires login again. No self-registration or account-recovery product is added.

Revoke, reject and suspend require a valid authorized session but no additional PIN. Create mandate and approve require the correct customer PIN. Reinstatement requires authorized ops role (or customer PIN if exposing that customer capability). A customer can suspend only their own agent delegation; the global agent kill switch belongs to ops or the verified circuit breaker. Customer “reject and suspend” suspends that customer’s mandate(s) for the agent, preventing one customer from disabling another’s delegation.

### 6.5 HTTP endpoint ownership

All external routes are served by the single gateway. A registers routers; route implementation belongs to the owner below. All non-login human endpoints enforce role and object ownership.

| Method/path | Owner | Input/auth | Output and behavior |
|---|---|---|---|
| `POST /v1/oauth/token` | B | Registered agent proof; `{client_id,mandate_id}` | 200 `{access_token,token_type:"DPoP",expires_in}`; never audit token bytes |
| `POST /v1/agents/register` | B | Registrar session; name, vendor, public JWK, `max_scopes` | 201 registered identity; unique thumbprint |
| `GET /v1/accounts/{id}/balance` | A | Agent token/proof; scoped account | 200 result plus audit reference |
| `GET /v1/accounts/{id}/transactions` | A | Agent token/proof; scoped account, bounded cursor query | 200 scoped transactions plus audit reference |
| `POST /v1/payments/bills` | A | Agent token/proof; body above, idempotency key | 200 executed / 202 pending / denied or failure envelope |
| `POST /v1/transfers` | A | Same; transfer action | Same shape; demo mandate does not authorize transfers |
| `GET /v1/actions/{request_id}` | A | Same verified customer/agent binding | Existing own action/result, including uncertain bank result; read-only support for safe retry |
| `GET /v1/escalations/{id}` | C | Agent token/proof; exact ownership binding | Status/result only; no full customer-facing risk diagnostics |
| `POST /v1/customer/login` | B | Seeded customer ID and PIN | Session and role, expiry; rate limited |
| `POST /v1/ops/login` | B | Seeded ops credentials | Separate ops session; no agent-token access |
| `GET /v1/customer/bootstrap` | A | Customer session | Own account summaries, biller catalog, delegable agent public cards; B supplies agent cards via port |
| `POST /v1/customer/mandates` | A | Customer session, selected terms and PIN | 201 signed immutable mandate; current customer comes from session |
| `GET /v1/customer/mandates` | A | Customer session | Own mandates with committed/reserved/remaining usage |
| `POST /v1/customer/mandates/{id}/revoke` | A | Customer session; own mandate | 200 revoked after atomic state/token/approval changes; repeat is idempotent |
| `GET /v1/customer/escalations?status=PENDING` | C | Customer session | Paginated own approval inbox; status enum uses uppercase everywhere |
| `POST /v1/customer/escalations/{id}/decision` | C | Customer session; `decision` is `APPROVE` or `REJECT`, optional `pin` and boolean `suspend_agent` | Final state or 202 processing; PIN needed only for approve; never accepts replacement action parameters |
| `GET /v1/customer/stream` | A | Customer session | Own events only |
| `GET /v1/ops/stream` | A | Ops session | Detailed decision, risk and lifecycle feed |
| `GET /v1/ops/agents` | B | Ops session | Registered agents and current status |
| `POST /v1/ops/agents/{id}/suspend` | B route, A lifecycle | Ops session; optional reason | State change, token invalidation and approval cancellation committed together |
| `POST /v1/ops/agents/{id}/reinstate` | B route, A lifecycle | Ops session | Active for future actions; does not revive old tokens, approvals or revoked mandates |
| `GET /v1/audit/entries` | B | Ops session; filters/cursor | Redacted paginated entries |
| `GET /v1/audit/verify` | B | Ops session | VerifyReport, including anchor coverage |
| `GET /v1/audit/evidence/{request_id}` | B | Ops session | Minimal JSON EvidenceBundle |
| `GET /health/live`, `GET /health/ready` | A | Local health check | Ready includes contract version and actual provider/profile status, never secrets |
| `POST /v1/dev/reset`, `POST /v1/dev/clock` | A | Scripted profile and local test credential only | Absent in real mode; reset only the configured disposable demo data |

`bootstrap`, ops login and action-status lookup fill necessary gaps in the original endpoint summary: UI data, human authentication, and safe uncertain-payment recovery. They add no new product feature. Bank-only routes additionally include `GET /bank/accounts/{id}/balance`, `GET /bank/accounts/{id}/transactions`, `POST /bank/payments`, `POST /bank/transfers`, and `GET /bank/operations/{bank_idempotency_key}`. All require a server-only service credential and validate the customer/account relationship. Bind the mock bank to loopback for the laptop demo; enforce the credential even on loopback. No bank URL/credential is included in browser code or agent SDK configuration.

Example response fixtures:

```json
{
  "request_id": "req_demo_1",
  "decision": "ESCALATE",
  "status": "PENDING_APPROVAL",
  "escalation_id": "esc_demo_1",
  "expires_at": "2026-11-17T04:05:00Z",
  "audit_ref": {"seq": 12, "entry_hash": "<64-lowercase-hex>"}
}
```

```json
{
  "request_id": "req_demo_1",
  "decision": "ALLOW",
  "status": "EXECUTED",
  "bank_result": {
    "status": "EXECUTED",
    "bank_operation_id": "bank_demo_1",
    "bank_idempotency_key": "<server-derived-key>",
    "executed_at": "2026-11-17T04:02:00Z"
  },
  "audit_ref": {"seq": 18, "entry_hash": "<64-lowercase-hex>"}
}
```

```json
{
  "error": {
    "code": "E_POLICY_PAYEE_NOT_ALLOWED",
    "message": "This payee is outside the mandate.",
    "request_id": "req_demo_2"
  }
}
```

Final IC0 fixtures replace illustrative hashes with real golden values. Pending approval responds with HTTP 202 and `Retry-After: 2`. Unknown execution has `status="EXECUTION_UNKNOWN"`, retains the original request ID, and provides the action-status URL; do not label it rejected or invite a new payment key.

### 6.6 Error mapping

Copy the full Appendix C enum into `contracts/reason_codes.py`. Freeze these mappings rather than leaving `403 / 409` undecided:

| HTTP | Codes or conditions |
|---|---|
| 400 | `E_REQ_INVALID`, malformed body, unsupported currency/window/schema; FastAPI validation errors normalized here |
| 401 | `E_AUTH_MISSING`, `E_AUTH_INVALID_TOKEN`, `E_AUTH_EXPIRED`, `E_AUTH_PROOF_INVALID`, `E_AUTH_KEY_MISMATCH`, `E_AUTH_REPLAY`, `E_AUTH_TOKEN_REVOKED`, `E_AUTH_CLOCK_SKEW`; unknown agent is publicly generic |
| 403 | `E_AUTH_AGENT_SUSPENDED`, `E_AUTH_AGENT_MANDATE_MISMATCH`; authenticated mandate state and hard policy failures; `E_RISK_HIGH`, `E_RISK_BREAKER_OPEN`; `E_ESC_BAD_PIN` |
| 404 | Customer/agent requests for an object they do not own or an unknown object; do not reveal another customer’s existence |
| 409 | `E_REQ_IDEMPOTENCY_CONFLICT`, `E_ESC_ALREADY_CONSUMED`, `E_ESC_HASH_MISMATCH`, `E_ESC_EXPIRED`, `E_ESC_CANCELLED`, `E_ESC_REJECTED`, `E_ESC_LIMIT_REACHED` on attempted mutation |
| 422 | `E_BANK_INSUFFICIENT_FUNDS` or definitive bank business rejection |
| 429 | `E_REQ_RATE_LIMITED`; return bounded retry guidance |
| 502 | `E_BANK_UNAVAILABLE` when execution is confirmed not to have happened |
| 503 | `E_AUDIT_WRITE_FAILED`, unavailable trusted state, bounded DB lock timeout; no new bank execution |
| 202 | Held approval; or separately identified `EXECUTION_UNKNOWN` after uncertain bank contact |

Polling a valid, owned escalation returns HTTP 200 with its terminal state even when that state is `REJECTED`, `EXPIRED` or `CANCELLED`. The error codes above apply to an invalid decision/consume attempt, not to observing a state. Additional plumbing errors such as `E_SERVICE_UNAVAILABLE` must be registered at IC0, not introduced as ad hoc strings later.

### 6.7 Internal interfaces: who calls whom

Implement these as typed Python protocols; all I/O methods are async. Pure evaluation/scoring may remain synchronous. `Tx` is an opaque unit-of-work context provided by A; services must not commit a caller-owned transaction.

| Provider | Port and key methods | Consumers |
|---|---|---|
| B | `IdentityVerifier.authenticate(request_bytes, headers, method, url) -> AuthContext` | A agent routes; C agent polling |
| B | `HumanAuth.authenticate(session_token, required_role) -> HumanContext`; `verify_pin(customer_id, pin) -> None` | A mandate routes, C decisions, B ops/audit routes |
| B | `AgentRegistry.get(id)`; `set_status(id,status,tx)`; `list_public()` | A lifecycle/bootstrap; C via read DTOs |
| B | `TokenService.issue(...)`; `revoke_for_mandate(id,tx)`; `revoke_for_agent(id,tx)` | B token routes; A lifecycle |
| A | `MandateReader.get(id,tx?) -> MandateSnapshot`; `UsageReader.get(...)` | B identity; C risk/approval |
| A | `MandateService.create(...)`; `evaluate(auth,action,snapshot,usage,now) -> PolicyResult`; `reserve(action,tx) -> Reservation`; `commit(reservation,tx)`; `release(reservation,tx)` | A pipeline; C uses read/evaluate through execution coordination |
| A | `Lifecycle.revoke_mandate(...)`; `suspend_agent(...)`; `suspend_customer_delegation(...)`; `reinstate_agent(...)` | Customer/ops routes, C breaker and reject/suspend |
| A | `ActionReader.get(request_id,tx?)`; `Catalog.get_payee(id)`; `Catalog.get_account(id)` | C summaries/risk; B evidence uses recorded snapshots |
| C | `RiskEngine.score(input) -> RiskResult`; `record_attempt(attempt,tx)`; `record_outcome(outcome,tx)` | A pipeline and execution completion |
| C | `EscalationService.create(action,assessment,tx)`; `get_owned(...)`; `decide(...)`; `claim_execution(id,expected_hash,tx)`; `cancel_for_mandate(id,tx)`; `cancel_for_agent(id,tx)`; `expire_due(now)` | A pipeline/lifecycle, C routes |
| A | `ExecutionPort.execute_stored(request_id,escalation_id?) -> ExecutionResult` | C after approval; A allow path |
| B | `AuditLog.append(event,tx?) -> AuditRef`; `verify(anchor?)`; `checkpoint()`; `evidence(request_id)` | Every backend module; B query routes |
| A | `BankClient.balance(...)`, `transactions(...)`, `pay_bill(...)`, `transfer(...)`, `lookup(bank_idempotency_key)` | A executor only |
| A | `EventBus.publish(committed_event)`; `Clock.now()`; `UnitOfWork.begin()` | Backend modules through dependency injection |

Cross-module dependency imports are allowed only for contract types. Only A’s composition root imports all real implementations and injects them. For the apparent cycle “identity needs mandate state; mandates need identity,” inject a read-only MandateReader into B and IdentityVerifier into A; neither service constructs the other. For “escalation needs executor; executor needs escalation,” inject ExecutionPort into C and EscalationService into A at composition time; never recursively call `decide` from execution.

### 6.8 SSE/event contract

Use SSE for committed updates; it is notification transport, not the source of authorization state.

```json
{
  "event_id": "18",
  "event_type": "bank.executed",
  "occurred_at": "2026-11-17T04:02:00Z",
  "request_id": "req_demo_1",
  "customer_id": "cust_001",
  "agent_id": "agt_billpal",
  "data": {
    "status": "EXECUTED",
    "audit_ref": {"seq": 18, "entry_hash": "<64-lowercase-hex>"}
  }
}
```

Wire format: SSE `id` equals event ID, `event` equals event type, `data` is one JSON envelope. Persist/audit before publish. Customer streams filter by authenticated customer ID and redact ops-only details. Ops streams require ops auth. D uses authenticated fetch streaming so tokens never enter query strings; use the same transport interface for mock events.

Clients deduplicate IDs, reconnect, then refetch affected REST lists. A may replay a bounded recent sequence using `Last-Event-ID`; if history is unavailable, explicitly signal a resync and refetch. Delivery is at least once/best effort; missed SSE cannot change approval, money or revocation state. The planned fallback is REST polling every two seconds.

## 7. Runtime integration and transaction ownership

### 7.1 One complete request path

1. **A edge:** validate size/schema, rate-limit, assign request ID; preserve raw bytes for B.
2. **B identity:** prove token/key possession and live authority. On failure, append `auth.failed` with only verified identity fields, then stop.
3. **A action store:** build canonical action from trusted identity. Claim a unique idempotency record for money-out. Same key/body returns the existing authorized result/pending action; different body conflicts.
4. **A policy:** evaluate hard constraints and soft step-up rules using current snapshots.
5. **C risk:** record and score this verified attempt, including hard-denied actions. Distinguish attempts from successful outcomes so pending/denied activity counts toward probing/structuring without contaminating the normal baseline.
6. **A combiner:** hard policy DENY wins; otherwise deny-band risk or breaker wins; otherwise step-up/medium risk/intent mismatch means ESCALATE; otherwise ALLOW. Risk errors use score 50 plus an explicit escalation flag and an ops alert; they never downgrade DENY.
7. **B audit through A’s transaction:** record request, risk assessment, decision and required snapshots. If a breaker fires, A coordinates suspension/cancellation in the same unit of work before returning.
8. **ALLOW:** A rechecks current hard authority, reserves budgets/counts atomically, persists a dispatch claim and write-ahead decision, commits, then calls the bank outside the SQLite transaction.
9. **ESCALATE:** C persists immutable pending approval plus its event in a committed transaction. No bank call and no budget spend yet. Enforce the pending-count cap atomically.
10. **DENY:** return the stable coarse reason. No bank action. Publish customer/ops updates only after the database commit.
11. **Completion:** A records bank outcome, commits/releases/retains the reservation appropriately, B appends the result, C updates outcomes, and A publishes committed events.

Never short-circuit policy denials before risk/circuit-breaker processing for verified actions. Conversely, never let unverified requests create trusted behavioural history for a claimed agent.

### 7.2 Budget, idempotency and SQLite transactions

| Concern | Required implementation |
|---|---|
| Money budget | `committed_paise + reserved_paise + new_amount <= max_total_paise`; equivalent count check. Conditional update and reservation record in the same transaction. |
| Multiple matching grants | Reserve every applicable grant row in a stable order or none; rollback on any failed constraint. |
| Unique business operation | Gateway unique key `(customer_id,agent_id,mandate_id,idempotency_key)`; retain fingerprint, request ID and response/state. Same key/body cannot create a second action or escalation. |
| Bank idempotency | Derive a stable server-side key from the full gateway idempotency scope. Bank has a unique key and rejects same-key/different-action calls. Debits and its operation record commit together. |
| SQLite writer contention | Use short `BEGIN IMMEDIATE` transactions, bounded busy retries, and one session per unit of work. SQLite permits only one concurrent writer; WAL does not remove this constraint. |
| Audit plus state | `AuditLog.append(event,tx)` joins the caller’s transaction; it must not open a second writer/commit inside it. No network calls while holding the writer transaction. |
| Repeated completion | Reservation commit/release and outcome recording are idempotent by operation/request ID. A retried callback cannot spend/count twice. |
| Read operations | Scope and audit still required; apply optional read count constraints. No money debit. |

AuditRef is provisional until its surrounding transaction commits. Publish SSE or return a receipt only after commit. If audit insertion fails, rollback the associated state mutation and do not dispatch a bank operation. This makes “no log, no action” testable rather than a comment in the pipeline.

### 7.3 Revocation, suspension and execution races

A’s lifecycle coordinator performs, in one gateway transaction: current mandate/agent status update through its owner, token revocation through B, cancellation of pending/approved-but-undispatched escalations through C, and the audit records through B. Event publication follows commit. A customer revoke response is sent only after this transaction succeeds.

The **dispatch claim** is the execution boundary. In the same transaction as the final hard checks and reservation, A marks the action `DISPATCHED`. If revocation/suspension commits first, no new dispatch is allowed. If dispatch commits first, the action is already in flight and may finish; its outcome remains auditable. Do not promise rollback of a bank operation that has already been dispatched. A retry after revocation must not create a new execution.

At startup, unresolved dispatches are checked against the bank operation lookup. If a call was never completed and authority has since been revoked, do not make a new bank submission. Never turn reinstate into resurrection of old approvals or tokens. New tokens and new requests are required.

The plan’s “under one second” revocation is a local target for subsequent-request enforcement, measured from revoke acknowledgement. It is not a guarantee that a previous in-flight bank call can be cancelled.

### 7.4 Approval lifecycle and ownership

Persistent states: `PENDING`, `APPROVED`, `EXECUTED`, `REJECTED`, `EXPIRED`, `CANCELLED`, `FAILED`; use action state `EXECUTION_UNKNOWN` for an uncertain bank outcome while retaining approval consumption metadata.

- C builds a summary from the stored action, registered payee, account mask and mandate terms. Agent note is separate, labelled unverified, escaped in the UI.
- C authenticates the customer via B, verifies ownership, verifies PIN for approval, then atomically changes `PENDING -> APPROVED` with a conditional update and audit record. Default TTL is 300 seconds; scripted TTL is 60 seconds; at most three pending approvals per mandate.
- C calls A’s ExecutionPort with stored IDs only. A/C atomically claim execution once, validate stored hash, live mandate/agent state, expiry, remaining budgets and approval ownership before dispatch. Approval deadline must still be valid at this claim.
- Approval resolves the soft step-up associated with this exact assessment; it does not bypass a new hard failure or subsequent suspension. Do not re-escalate the unchanged stored action in an infinite loop.
- A is the only code that calls BankClient. C never reconstructs a payment from a browser follow-up payload.
- Duplicate approval/consume attempts return 409 `E_ESC_ALREADY_CONSUMED` and the client refetches status; at most one bank execution occurs. Duplicate delivery of the executor job safely returns the stored action outcome.
- Reject, expire, revoke or suspend before dispatch prevents execution. Wrong PIN leaves the action pending and is audited/rate-limited. Reject-and-suspend invokes A’s customer-scoped lifecycle path.
- Expiry runs on startup and periodically. Approved-but-undispatched records are resumed by the executor only after rechecking all constraints; crashes must not strand an approval indefinitely or execute it twice.
- An authenticated agent whose financial authority was revoked may obtain read-only status of its existing action/escalation if B verifies its cryptographic identity and original ownership binding. This narrow status path grants no new bank access; all other revoked-token operations remain blocked.

### 7.5 Bank errors, restart and audit failure after payment

| Situation | Action |
|---|---|
| Confirmed insufficient funds/rejection | Append outcome, release reservation once, return business error; no debit. |
| Connection failure proven before submission | Record terminal failure and release reservation. Same-key retries return that stored failure; a newly intended payment requires a new business key. |
| Timeout or lost response after possible acceptance | Mark action `EXECUTION_UNKNOWN`, retain reservation, poll bank with original key. No new key and no assumed failure. |
| Bank confirms success but gateway crashes before recording it | Recovery looks up the same bank key, commits the existing reservation, appends the missing result once. |
| Final audit write fails after bank success | Write-ahead decision and dispatch remain durable. Do not undo a real debit by releasing budget or report clean success. Retain unresolved state; recover through bank lookup and persist result/receipt when audit is available. |
| Bank outcome remains unknowable | Keep budget reserved and show unresolved status; local demo should expose this as a controlled fault case, not pretend it succeeded or failed. |

This is a narrow recovery path required by idempotent money execution. It is not the plan’s optional general reconciliation platform. A implements it with the existing action/reservation records and bank lookup, plus a startup/retry sweep.

### 7.6 Audit integrity and evidence

B fixes the chain encoding in `contracts/canonical.py`:

```text
entry_without_hash_and_signature = all defined audit entry fields except entry_hash and sig
entry_hash = hex(SHA256(bytes.fromhex(prev_hash) || JCS(entry_without_hash_and_signature)))
sig = base64url(Ed25519_sign(audit_private_key, bytes.fromhex(entry_hash)))
genesis prev_hash = 64 zero hex characters
```

The canonical entry includes `prev_hash` as a field as well as the explicit chain prefix; every verifier uses this exact rule and golden vectors. Keep token, mandate and audit signing keys separate. Entry sequence allocation and append serialize in the same transaction. UPDATE/DELETE protection applies to the normal audit table; verification checks sequence, hash links, recomputed hashes and signatures.

Write signed checkpoints every 50 entries and on demand to an independent local append-only file; expose the anchored sequence. A checkpoint at sequence N must match the chain entry at N, and the current head must be at least N. It need not equal an older checkpoint if valid entries have since been appended. Tail deletion **after the latest external anchor cannot be detected by the remaining chain alone**. Force a checkpoint/receipt at the test head before demonstrating tail-truncation detection. State that a local checkpoint file is not protection against full host/key compromise.

`tools/tamper.py` only changes a disposable database copy with separately retained original anchors. Disable/remove append-only triggers in that copy for the demonstration; an ordinary UPDATE against the protected live table would merely prove trigger enforcement. Test edited content, middle deletion, rewritten hashes without signing key, and anchored tail truncation.

Every execution decision records the exact mandate snapshot/hash, public agent key identity and action projection. Store the verified signed proof JWS plus safe digest/claim metadata if demonstrating offline signature verification; a bare `jti` and `bsh` alone is not a verifiable signature. Never store the access token itself. B builds the evidence bundle from these immutable audit snapshots and state-transition events, so it does not need direct access to A/C’s private ORM models.

Minimum events: agent registered/suspended/reinstated, token issued/revoked, mandate created/revoked/suspended/expired, auth failed, request received, risk scored, policy decision, escalation created/approved/rejected/expired/cancelled, bank executed/failed/unknown, and checkpoint metadata. Assign event creation to the initiating service; append through B’s port exactly once per logical state transition. Reject unsigned large arbitrary payloads; cap/redact text and retain a digest when needed. A signature proves attribution to a key, not that its runtime was uncompromised.

## 8. Member A execution roadmap

**Goal:** turn verified requests into correctly authorized, auditable, idempotent bank actions. A owns assembly, not everybody’s implementation.

| Order / task | Build | Independent test / deliverable |
|---|---|---|
| A01 — Foundation | Common/core contracts, app factory, config, clock, DB factory, error mapping, owner-specific router registration | Minimal app starts with injected providers; health shows provider modes; no hard import of unfinished B/C modules |
| A02 — Fake bank and private bank app | FakeBank for tests; real simulated accounts/catalog/ledger and service credential; same-key lookup and execution | Direct unauthenticated bank request fails; same-key/same-payment debits once; changed payment conflicts; balances and history agree |
| A03 — Mandate domain | Validated immutable terms, signature through shared crypto utility, customer create/list, account/agent bindings, usage rows | Use fake HumanAuth/AgentRegistry/Audit; ownership and invalid constraints rejected; signatures/hashes match vectors |
| A04 — Pure policy | Action/account/payee allow-lists, per-txn and total/count caps, validity/hour windows, STEP_UP rules, overlap handling | Every policy reason has a boundary case: cap exactly equal, cap+1, amount 0, wrong account, future/expired/revoked mandate; no DB needed |
| A05 — Execution ledger | Idempotency claims, multi-grant reservations, committed/reserved counters, bank outcome handling and minimal recovery | Real temporary SQLite and fake bank; five concurrent calls, repeated callbacks, crash after bank accept, unknown timeout all preserve money |
| A06 — Pipeline and lifecycle | Identity → policy → risk → decision → audit → dispatch; revoke/suspend/expiry coordination and event publication | Inject fail/error/pending providers; hard DENY never becomes allow; failed audit yields no bank call; pending approval produces no debit |
| A07 — Streams and read support | Scoped balance/transactions/action status, bootstrap catalog, customer/ops SSE, dev reset/clock guard | Customer A never receives B’s events; reconnect/refetch works; dev routes absent in real mode |
| A08 — Real integration | Replace B/C fakes in `deps.py`; run contract/API diff and real scenarios; provide cross-platform start/reset | Clean clone, deterministic seed and real stack start; A-owned race/failure integration suite green |

**A handoff:** core contract version, run commands, environment variable names, factory signature, provider injection examples, DB ownership/schema order, exact execution state transitions, real BankClient behavior and ports. A must deliver integration seams early; completing all policy details is not a prerequisite for B/C/D to work.

**A acceptance:** S1–S5, bank bypass test, idempotency/recovery tests and no-log/no-action pass. Bank dispatch is unreachable from untrusted or approval-only modules. No provisional audit receipt is returned before commit.

## 9. Member B execution roadmap

**Goal:** prove who acts and preserve verifiable evidence of what was authorized and executed. B can work on identity and audit independently using A/C fixtures.

| Order / task | Build | Independent test / deliverable |
|---|---|---|
| B01 — Shared crypto vectors | Canonical projections, digest encodings, public JWK thumbprint, Ed25519 helpers, reason registry | Golden vectors for identical semantic JSON, altered amount, altered task, note-only change, empty body and Unicode |
| B02 — Registry and human auth | Agent registration/status, unique key constraint, seeded customer/ops sessions, PIN hash checks and throttling | Fake mandate/catalog ports; wrong role/audience, IDOR, wrong PIN and duplicate public key rejected |
| B03 — Agent proof verifier | Signature/type/algorithm/key checks, method/URI/body/token binding, timestamp and atomic replay cache | One test for every checklist item; simultaneous identical proof has one winner; invalid proof cannot mutate verified risk history |
| B04 — Token lifecycle | Token issue/expiry, registry-mandate binding, live status, revocation ports and specific state/error mapping | Fixture mandates active/expired/revoked/wrong agent; stolen token with wrong key rejected; agent token cannot call human endpoints |
| B05 — Python SDK | Keygen, serialize-once signed requests, token fetch, money idempotency key, fresh-proof retry, approval/action polling | `httpx` mock transport asserts exact sent bytes and headers; retry changes proof JTI, preserves business key, never silently changes amount |
| B06 — Audit store | Append chain/signatures, DB triggers, transaction participation, serialized writers, checkpoint file | Temporary SQLite; concurrent appends form one contiguous valid chain; writer rollback hides event and reference; append failure propagates |
| B07 — Verify and evidence | Verification report, safe tamper-copy script, JSON bundle assembled from recorded snapshots | S10 variants using fixed signed anchors; S11 fixture includes mandate, identity, request, risk, approval and bank result |
| B08 — Real integration | Verify SDK↔gateway proofs, replace fake readers/auth/audit, wire complete decision snapshots | S6a–h and real S10/S11 pass; scan API/audit/debug outputs for leaked token/PIN/key data |

**B handoff:** SDK import/CLI example, token/proof contract and golden vectors, auth-domain requirements, audit port with `Tx` semantics, event-schema examples, verification output, evidence response fixtures, safe key-generation/setup steps. Do not send teammates real private keys in chat or commit them.

**B acceptance:** all S6 authentication cases pass with precise internal reasons, real audit detects all four anchored tamper cases, audit failure blocks a new bank action, and evidence corresponds to the actual recorded request. No assertion that a key-bound token prevents abuse by an attacker who also controls the legitimate private key.

## 10. Member C execution roadmap

**Goal:** detect misuse that survives ordinary scope checks, pause risky actions, and turn the threat model into executable scenarios.

| Order / task | Build | Independent test / deliverable |
|---|---|---|
| C01 — Risk/approval contracts | RiskInput/Result, escalation states, deterministic clock fixtures, ScenarioClient abstraction | Contract tests and fake executor/audit/human-auth/history providers run without gateway or SDK |
| C02 — History and feature inputs | Seeded 60-day baseline per customer/agent; recent verified attempts distinct from successful outcomes; trusted catalog adapter | Fixed random seed; current attempt counted once; denied/pending attempts affect detection but not normal baseline; zero-history/MAD=0 cases defined |
| C03 — Rule scoring | All ten rule/statistic signals from Appendix D, noisy-OR combination, profile bands, max-three summary reasons | Hand-built history test per signal; monotonicity property; low/medium/high and risk-error fixtures; deterministic timing |
| C04 — Circuit breaker | Score deny-band, five policy denials in ten minutes, intent mismatch plus unverified payee; return recommendation to A’s lifecycle | With fake lifecycle, correct subject/scope/reasons emitted once; with real A/B, next call rejected and pending approvals cancelled |
| C05 — Approval service | Persist canonical action/hash, gateway summary, pending cap, decision/claim state machine, TTL sweeper | Wrong owner/PIN, expired action, duplicate approve, hash mismatch, simultaneous approvals and max-pending races |
| C06 — Execution handoff | C decision invokes A’s execute-stored port; no replacement parameters; reject-and-suspend path | Recording executor proves stored IDs only; no bank invocation on reject/expiry/cancellation; recovery resumes once |
| C07 — Scenario runner | S1–S11, independent reset per scenario, expectation assertions on HTTP + bank ledger + usage + audit | First run against fixtures to debug orchestration; switch ScenarioClient to B’s real SDK and fail if any provider is still a fake |
| C08 — Metrics and real integration | Outcome table, actual detection/false-positive counts and latency; seed tuning against known scenario bounds | Report measured results and dataset limitations; S7/S8/S9 meet expected state transitions without hard-coding scenario IDs into risk logic |

**Rule catalog to implement:** `new_payee`, `amount_outlier`, `near_limit_structuring`, `velocity`, `request_rate`, `off_pattern_time`, `sequence_anomaly`, `probing`, `intent_mismatch`, `read_enumeration`. `ml_outlier`/Isolation Forest is optional after all integrated gates pass.

Use the plan’s noisy-OR formula and default weights; thresholds are balanced 35/70, conservative 25/60, permissive 45/80. Seed history must make the ordinary ₹1,850 payment low risk and the ₹4,800 example an approvable step-up, not an unintended hard deny. S8’s quoted scores are targets, not magic constants: assert bands and payment counts, inspect signals, and tune fixture distributions transparently. No risk rule checks `scenario_id`.

C owns the history store and baseline update. Normal baseline uses confirmed successful/approved actions with bounded drift; recent-attempt counters include denials/pending actions. Customer rejection feedback can raise sensitivity but never create permission. Audit these versioned scoring choices. Seeded historical rows are explicitly synthetic and are not counted against the current mandate’s usage unless the fixture says so.

**C handoff:** signal definitions and thresholds, seeded datasets/clock, approval DTOs and summaries, scenario commands, expected decision bands, provider adapters, test reports. Failed/uncertain bank actions must not become successful baseline observations.

**C acceptance:** S2d, S7, S8 and all S9 variants pass; S1–S11 runner works with the real modules; at most one S8 payment executes before suspension; pending approvals survive restart and cannot be consumed twice.

## 11. Member D execution roadmap

**Goal:** make all five capabilities usable and demonstrable without placing security decisions in the browser. D starts from contract fixtures, not a running backend.

| Order / task | Build | Independent test / deliverable |
|---|---|---|
| D01 — Shell and transport | Customer/Ops routes, seeded login forms, generated TS client, mock/real transport selector, error/status components | Entire app runs against stateful fixtures; typecheck and production build pass; mock indicator visible |
| D02 — Mandate experience | Agent name/vendor/key fingerprint, account/payee selection, actions/caps/count/expiry, PIN creation, list/budget/revoke | Integer paise parsing, IST display, validation errors, loading/failure/empty states; revoked UI refetches server state |
| D03 — Approval inbox | Trusted payee/account/amount summary, up to three risk reasons, deadline, unverified note label, approve/PIN, reject, reject-and-suspend | Pending/approved/executed/rejected/expired/cancelled/failed/unknown fixtures; double click disabled but server remains authoritative |
| D04 — Ops decisions and agents | Live decision list, compact score/reasons, registered agents, suspend/reinstate with result/error feedback | Stream disconnect/resync and duplicate event fixtures; no stale ACTIVE badge after suspension |
| D05 — Audit and evidence | Filtered log, verify button, first-failure location, anchor coverage, simple JSON evidence detail/download | Valid, edited, sequence-gap, signature-invalid and truncated fixtures; never label unanchored history “tamper-proof” |
| D06 — Real endpoint wiring | Connect the same transport adapter to gateway, authenticated fetch/SSE, refetch after mutations | A clean real-backend smoke: login → create → payment pending → approve → executed → revoke → verify |
| D07 — Demo preparation | Setup README, exact seven-minute script, screen layout, simple architecture explanation and measured-results slide content | Another member follows the README from a clean reset; mock mode absent in final demo |
| D08 — Final verification | UI smoke, projector readability, scenario status, backup recording | Three rehearsals; record one successful real-stack run; mark failed/unmeasured metrics honestly |

**Required UI details:** all approval text comes from gateway-owned structured fields; free text is escaped; no raw HTML from agent notes; rupees have two decimal places; action status and business errors are visible; customer account IDs are masked when full IDs are unnecessary. Display risk details only in authorized customer/ops views, not agent responses. Disable repeated actions for usability, while relying on backend idempotency for correctness.

Customer receipt support is a simple visible/copyable `audit_ref` on completed actions; a sophisticated persistent activity dashboard is optional. Keep two application routes with tabs/panels rather than expanding into extra products. Rich animations, a large chart dashboard and printable evidence are optional.

**D handoff:** mock/real startup steps, expected environment variables, generated-client refresh command, fixture list, UI state coverage, demo script and recording location. B/A/C should be able to validate their endpoint with D’s UI without editing frontend code.

**D acceptance:** every required customer/ops control works with the real backend; no fake “approved” or “executed” state is manufactured in the UI; an independent teammate completes the demo from the guide.

## 12. GitHub workflow and compatibility gates

### 12.1 Branch and PR workflow

Use short-lived branches into protected `main`: `track-a/A04-policy`, `track-b/B03-proof`, `track-c/C05-approval`, `track-d/D03-inbox`. All four can push their branches to the same repository at any time. Direct concurrent pushes to `main` are not the default workflow.

```text
git switch main
git pull --ff-only
git switch -c track-b/B03-proof
git add <owned-paths>
git commit -m "Implement agent proof validation"
git push -u origin track-b/B03-proof
```

Open a PR with: behavior changed, owned paths, contract version, tests run, provider mode, new environment/schema requirements, and one runnable example. Keep changes focused; the plan’s roughly 300-line preference is a guideline, not a reason to split an inseparable security fix into broken commits.

Before merge, update the branch against current `main` using the team’s chosen rebase or merge policy, rerun affected checks, and inspect the proposed merged result. If rebasing a published personal branch is necessary, coordinate and use `--force-with-lease`; never force-push shared `main`. Revert an isolated bad merge if required rather than leaving the demo branch broken.

Normal module PR: one reviewer from a different track plus the owning member’s agreement if another member authored it. Contract/shared semantics PR: author sign-off and the other three members’ approvals, migration/fixture updates and `docs/CONTRACT_CHANGES.md`. Store all four acknowledgements against the latest contract-changing revision. Do not block private module refactoring with a four-person approval rule.

### 12.2 CODEOWNERS policy

Replace placeholders with actual handles at kickoff. Member A/B/C/D are role names; this document does not assume earlier project handle assignments still apply.

```text
# Illustrative excerpt; substitute valid GitHub handles before committing.
/gateway/main.py              @MEMBER_A
/gateway/deps.py              @MEMBER_A
/gateway/pipeline.py          @MEMBER_A
/gateway/platform/            @MEMBER_A
/gateway/mandates/            @MEMBER_A
/gateway/executor/            @MEMBER_A
/gateway/events/              @MEMBER_A
/bank_mock/                   @MEMBER_A
/gateway/identity/            @MEMBER_B
/gateway/audit/               @MEMBER_B
/agent_sdk/                   @MEMBER_B
/gateway/risk/                @MEMBER_C
/gateway/escalation/          @MEMBER_C
/simulator/                   @MEMBER_C
/frontend/                    @MEMBER_D
/tests/a/                     @MEMBER_A
/tests/b/                     @MEMBER_B
/tests/c/                     @MEMBER_C
/contracts/                   @MEMBER_A @MEMBER_B @MEMBER_C @MEMBER_D
/pyproject.toml               @MEMBER_A
/uv.lock                      @MEMBER_A
/config.yaml                  @MEMBER_A
/docs/DEMO.md                 @MEMBER_D
/README.md                    @MEMBER_D
/.github/workflows/integration.yml @MEMBER_A
/.github/CODEOWNERS            @MEMBER_A @MEMBER_B @MEMBER_C @MEMBER_D
```

A expands this excerpt using every path in Section 4, including owner-specific test/tool/workflow files. GitHub CODEOWNERS is review routing, not file locking. When several owners are listed, one owner’s approval can satisfy the native code-owner rule; enforce unanimous contract agreement with the explicit PR checklist/merge steward, or an additional check if available. Branch protection/ruleset availability depends on repository/account capabilities; if unavailable, follow the same PR-only merge discipline manually. See official GitHub documentation in Section 15.

### 12.3 Required CI/check sequence

| Gate | What runs | What it catches |
|---|---|---|
| 1. Static and schema | Python lint/import/type checks as configured; frontend typecheck/build; reason enum validation | Broken imports, schema drift, typos, unsupported fields |
| 2. Contract suite | Valid/invalid fixtures, hash vectors, protocol conformance, fake response validation | Different names, money units, timestamp/enum conventions, inconsistent hashes |
| 3. Provider suite | Real owner implementation unit/security tests; fakes only as neighbour substitutes | Algorithm and lifecycle bugs that shape checks cannot catch |
| 4. API compatibility | Export real OpenAPI; compare methods/paths/security/schemas/statuses to frozen spec; regenerate TS client and require a clean diff | Backend/client drift; missing routes and wrong auth/error shapes |
| 5. Real integration | Actual identity, policy, risk, approval, audit, bank adapter; real SQLite transactions | Dependency injection mistakes, transaction deadlocks, missing event propagation |
| 6. Scenario and race suite | S1–S11, repeated budget races, approval/revoke races, timeout/restart/audit faults | Semantically incompatible modules even when compilation and Git merge succeed |
| 7. UI smoke | Customer and Ops flows against real backend | Wrong endpoints, missing state transitions, stale views, auth/SSE problems |

Each owner supplies a small parameterized port suite that its fake and real provider both satisfy where behavior overlaps. Security-only tests—signature validation, DB durability, replay protection, real race handling—run **only against real providers** and cannot be replaced by fixture checks.

During the walking-skeleton phase, label checks `skeleton` or `mock`; passing them does not make the real integration check green. Once IC2 is reached, required real-component checks cannot be downgraded to fakes. Full integration runs on every proposed merge that changes contracts/config/platform or crosses modules, and on every `main` update; owner tests also run on their own PRs. Night-before-demo execution must use the exact final commit and locked dependencies.

### 12.4 Conflict and change handling

1. Do not edit a teammate’s file “just to unblock” an integration issue. Reproduce it with a failing fixture/test and notify the owner.
2. A contracts-only hotfix describes the old/new behavior and affected consumers. Update providers, consumers and golden fixtures together or introduce a backward-compatible adapter.
3. A is the merge coordinator for shared Python config, contract aggregates and composition root. B/C/D keep their own implementation ownership.
4. When a file conflict occurs, both owners inspect the intended behavior. Do not blindly choose all “ours” or all “theirs”; run the relevant contract and integration tests after resolution.
5. Unresolved blockers older than 30 minutes are raised at the next short team check-in; continue independent work against the existing contract while the owner fixes the provider.

## 13. Integration schedule and scenario acceptance

### 13.1 Checkpoints for a 24-hour build

| Checkpoint | Target | Concrete gate | Responsible members |
|---|---|---|---|
| IC0 | Hour 2 | Contract v1, fixtures, owner fakes, commands and contract-only OpenAPI agreed; each member runs own harness | All; A assembles |
| IC1 | Hour 6 | SDK-shaped client → gateway skeleton → mock bank → event → basic UI; failure/pending fixtures also demonstrated | A wires; B/C supply fakes; D consumes |
| IC2 | Hours 12–14 | Real identity, policy and audit; real SDK happy path; S1/S2/S4/S5/S6/S10 provider checks; no real-mode fake dependencies hidden | A/B lead; C/D adapt |
| IC3 | Hours 17–18 | Real risk and approval integrated; S3/S7/S8/S9; UI approve/reject/revoke and receipt; evidence S11 | C leads; A/B/D fix own modules |
| IC4 | Hour 20 | All S1–S11 and critical race/failure cases pass on real stack; feature freeze | All |
| Release rehearsal | Hours 20–24 | Fixes only, clean reset, three rehearsals, measured results, backup video | D coordinates; all participate |

The source plan also places IC2 at hour 12 while allowing the underlying phase through hour 14; this schedule uses that as a window rather than incompatible deadlines. Freeze at hour 20 protects the full final four hours. For a 36-hour event, allow more implementation time but preserve at least the last four hours for verification and rehearsal. Do not spend the extra hours on P2 while core gates are red.

### 13.2 Scenario acceptance matrix

Every scenario gets an independent clean seed or isolated database, deterministic clock and explicit configuration. Assertions include HTTP/state, **bank ledger**, mandate usage and audit—not just a response label.

| Scenario | Test driver / fixing owner | Required result |
|---|---|---|
| S1 — Happy path | C driver; A/B fixes | ₹1,850 electricity payment executes once; initial 12,500,000 paise becomes 12,315,000; lifetime budget 1,000,000 becomes 815,000; audit shows identity, decision, risk and bank result |
| S2a–c — Scope | C / A | ₹7,000 denied by per-txn cap; unlisted mobile biller denied; unauthorized transfer denied; no balance/usage change |
| S2d — Probing | C / C+A+B | Five hard policy denials within ten minutes cause suspension by the fifth or earlier; next protected action fails; audit explains why |
| S3 — Concurrency | A test, C scenario | Total 400,000 paise, per-txn 200,000, five parallel 150,000 requests, unique keys, shadow risk, no soft step-up: exactly two execute, three fail budget; repeat 100 randomized runs |
| S4 — Expiry | C / A+B | Action at +30 seconds succeeds, +91 fails for a 90-second mandate; separate not-yet-valid and exact-boundary tests |
| S5 — Revoke | C / A+B+C | Revoke acknowledgement follows committed status/token/cancel changes; next action fails immediately; pending approval cancelled; no new dispatch |
| S6a–h — Identity | B tests, C scenarios | Missing credentials, stolen token/wrong key, proof replay, unknown agent, wrong mandate binding, token expiry, changed signed body, agent attempting human routes all fail correctly |
| S7 — Prompt injection | C / C+A+B | Scripted poisoned-bill instruction attempts ₹40,000 transfer to unverified PAYEE-X; hard DENY plus intent/new-payee evidence; breaker suspends; no money moves |
| S8 — Within-scope structuring | C / C+A | Six ₹1,990 attempts at ~20-second intervals: first low/allow, second–third pending, fourth high/deny/suspend under tuned fixture; pending cancelled; at most one payment executes |
| S9a — Approve | C; A/B backend; D UI | ₹4,800 action is pending; correct customer/PIN approves; gateway executes stored action once and returns receipt |
| S9b–c — Reject/expire | C / C | Rejected or expired approval never debits; clock advance suffices; UI and polling show terminal status |
| S9d — Reuse/hash | C / C+A | Duplicate approval/consume cannot repeat execution; substituted hash/action rejected; simultaneous clicks have one winner |
| S9e — PIN/spam | C / C+B | Wrong PIN audited without execution; fourth pending request exceeds cap; concurrent creation also respects cap |
| S10a–d — Audit tamper | B / B | Content edit, middle deletion, rehashed rows without signing key, and tail truncation below retained anchor all detected with meaningful first failure |
| S11 — Evidence | B+C / B+D | JSON and basic Ops view contain the exact historical mandate, public agent identity, signed proof evidence, risk, approval metadata and bank result; verification scope stated |

The demo seed uses the original plan’s personas: `cust_001`, `ACC-001`, `BILL-ELEC`, `BILL-MOBILE`, `BILL-INS`, `PAYEE-X`, `PAYEE-CRYPTO`, registered `agt_billpal` and `agt_paywise`, and an unregistered rogue key. Add a second isolated customer/account only as a **security-test fixture** for cross-customer checks. Demo PIN `4821` is disposable seed data, never a production credential.

### 13.3 Cross-module failure and compatibility tests

| Test | Owner | Pass condition |
|---|---|---|
| Bank bypass | A | Direct browser/agent call without service credential rejected; no frontend secret |
| Auth-domain/IDOR matrix | B | Agent/customer/ops credentials cannot cross roles or read another customer’s mandate, approval, stream or action |
| Signed bytes and hashes | B | SDK signs transmitted bytes; server and golden vectors agree; one-paise mutation rejected |
| Idempotency versus proof replay | A+B | Fresh proof + same business key returns existing operation; same proof rejected; same key + different body conflicts; two simultaneous duplicates debit once |
| Audit pre-execution outage | A+B | Forced append failure causes no bank call and no committed reservation/approval dispatch |
| Audit post-bank outage | A+B | Payment is not falsely released/repeated; unresolved result recovers via same bank key and later receipt |
| Risk module error | A+C | Risk error explicitly escalates a policy-allowed action; hard denial remains denial; ops notified |
| Approval/revoke and approval/expiry races | A+C | At most one dispatch; whichever state transition wins is respected and audited |
| Pending approval concurrency | C | Simultaneous creates cannot exceed three pending records per mandate |
| Real SQLite contention | A+B | Parallel writes do not create a broken audit chain, duplicate reservation, infinite lock wait or deadlock |
| Crash/restart | A+C | Pending approvals persist; due approvals expire; uncertain dispatched operations resolve without double debit |
| Stream disconnect | A+D | Restored UI refetches current server state and deduplicates events; no authorization depends on SSE delivery |
| Baseline contamination | C | Auth-failed traffic never attributed as verified history; denied/pending activity affects attempt signals but not normal-success baseline |
| Contract/client drift | A+D | Real OpenAPI matches agreed route/auth/error schema; generated TypeScript client is current |

### 13.4 Measurement rules

C reports actual numbers, configuration, dataset seed and final commit. Targets retained from the plan: zero overspends over 100 race runs; local p95 decision latency under 100 ms excluding human wait; verification of 1,000 audit entries under one second; four of four anchored tamper variants detected. These are **targets until measured**, not claims to put on slides automatically.

If there is time for the proposed roughly 2,000 normal / 200 attack-event dataset, keep evaluation traces separate from tuning traces and report attack-sequence detection and normal-event false positives with explicit denominators. The plan’s 90% detection / 2% false-positive goal is unvalidated until measured. A small rules-only evaluation is preferable to inventing accuracy or rushing an ML model.

## 14. Final checklist, cut line and handoff

### 14.1 Ready for parallel implementation

- [ ] Each person knows their paths, interfaces, command and first task.
- [ ] Python/frontend versions and lockfiles are pinned on the demo machine.
- [ ] Contract v1, HTTP/error/status enums and exact hash vectors are committed.
- [ ] Every consumer can exercise success, denial, pending and dependency-failure cases without a teammate’s running module.
- [ ] Database table writers, transaction participation and router registration ownership are explicit.
- [ ] Real-mode configuration rejects permissive fakes and shadow risk.

### 14.2 Ready to merge each module

- [ ] Owner tests and relevant shared contract tests pass.
- [ ] No unreviewed change to another member’s paths, frozen schema or root lockfile.
- [ ] Required state mutations emit audit events with no PIN/token/private-key leaks.
- [ ] Error codes and money/time representations match the shared contract.
- [ ] Required environment/schema changes and one working request example are documented.
- [ ] PR tested against current target branch; cross-track reviewer checked integration assumptions.

### 14.3 Ready for the real demonstration

- [ ] Start from a clean clone/reset using the documented commands and final commit.
- [ ] All five features are implemented, including the minimal S11 evidence bundle.
- [ ] No fake security provider or direct bank bypass in the real run.
- [ ] Real SDK, schema/API comparison and S1–S11 pass.
- [ ] Concurrency, duplicate payment, expiry, revocation, approval reuse and wrong-customer cases pass.
- [ ] Audit write-ahead, tamper checks and uncertain-bank recovery pass.
- [ ] Customer/Ops UI shows actual committed backend state; pending never appears as executed.
- [ ] After a revoked agent loses financial authority, read-only status handling does not grant new bank access.
- [ ] Tamper demo uses a disposable copy and retained checkpoint/receipt.
- [ ] Local latency and integrity metrics are measured; unmeasured targets are labelled.
- [ ] Dev reset/clock controls are isolated from normal real mode; no network dependency for scripted demo.
- [ ] Demo rehearsed three times; another member can run it; successful backup video recorded.

### 14.4 If behind schedule

Cut in this order: live LLM/NL mandate helper and all P2 features; Isolation Forest; large synthetic evaluation; printable HTML/rich evidence visualizations; elaborate charts/animations/activity dashboard; then replace SSE delivery with two-second polling. Preserve a basic JSON evidence view and action receipt.

Do not cut identity/key binding/replay checks, scoped authority, atomic caps/idempotency, hard-policy precedence, hash-bound single-use approval, revocation, audit write-ahead/verification, real-stack scenarios, or the two usable human screens. If the core cannot pass, reduce demo presentation complexity and report the unresolved gap; do not mark a mock as implemented security.

### 14.5 Unavoidable dependencies and how they stop blocking development

| Real dependency | Development unblocker | Final proof required |
|---|---|---|
| All modules share representations | Contract v1 and golden vectors at IC0 | Schema validation and matching hashes across providers |
| Identity needs current mandate status | FixtureMandateReader | Real revoked/expired/ownership cases |
| Gateway needs identity and audit | Injected verifier/audit fakes | Real signature and no-log/no-action tests |
| Approval needs human identity and execution | Fake HumanAuth and recording ExecutionPort | Real PIN/ownership, single-use and bank-ledger tests |
| Risk needs trusted context/history | Fixed catalog/history DTOs and clock | Real denied-attempt and suspension propagation tests |
| Simulator needs SDK and API | ScenarioClient abstraction | Real signed SDK calls for full S1–S11 |
| Frontend needs every backend | Stateful fixture transport and contract-only OpenAPI | Real generated client and UI smoke path |
| Final application needs all providers | Early skeleton, staged real swaps and repeated merge checks | All-real final commit, isolated resets, rehearsal |

**First action for each member:** A scaffolds the common/core harness; B starts proof/hash vectors and registry/auth tests; C starts risk/approval fixtures and pure scoring tests; D starts the two-screen UI in mock mode. None requires another member’s finished implementation. Final integration succeeds only when contracts, state transitions, database effects and security outcomes are verified together.

## 15. Source basis and technical references

### Supplied project sources

- Problem-statement image supplied in this request: “Cybersecurity: Securing Banks Against Rogue AI Agents.”
- [RESEARCH_AND_DEVELOPMENT_PLAN(1).md](https://chatgpt.com/library?file_id=libfile_634181eea61c819198a76fdd7ad8615a&account_id=personal) — AgentGate v1.0, especially §§1.5, 2.5–2.9, 3.1–3.6, 4–7 and Appendices A–E.
- [RESEARCH_AND_DEVELOPMENT_PLAN.md](https://chatgpt.com/library?file_id=libfile_32fa663b114081918ac09397fc739caa&account_id=personal) — identical supplied copy.

### Primary technical references checked for integration details

- [RFC 9449 — DPoP, §§4.2–4.3 and 7](https://www.rfc-editor.org/rfc/rfc9449.html): proof structure, key binding, method/URI and access-token checks. AgentGate’s `bsh`, restricted algorithms and token request format are project choices.
- [SQLite — Transactions](https://sqlite.org/lang_transaction.html): single-writer behavior and `BEGIN IMMEDIATE`; informs short shared transactions and bounded contention handling.
- [GitHub — About code owners](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners) and [available rules for rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets): review routing and required approvals; multiple listed code owners do not automatically mean unanimous approval.

This distribution does not repeat the source plan’s time-sensitive regulatory or payment-network claims. They are not prerequisites for implementation and should be checked independently before being quoted in a pitch. The proposal is a hackathon prototype using standard primitives and explicit local contracts, not a claim of banking compliance or production security certification.


