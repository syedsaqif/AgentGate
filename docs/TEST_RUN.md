# Verification evidence

Owner: Member D, with results supplied by all tracks.

## Bootstrap

Initial structure commit: `c61e2305153ddb660196e6fe80eda17523b98096`

GitHub Actions run: <https://github.com/syedsaqif/AgentGate/actions/runs/44028762583>

Passed on 17 November 2026:

- Contract/scaffold validation for all four tracks
- Five scaffold/contract tests; six future integration tests explicitly skipped
- Ruff checks and strict contract typing
- Frontend TypeScript checking, Vitest smoke test, and Vite production build
- Frozen Python and npm dependency installation
- Local staged credential-pattern scan and Git integrity verification

These are bootstrap results only. Feature, security, scenario, race, latency, or tamper claims must not be recorded until the corresponding real-provider checks exist and pass.

## D06 evidence on current main

Tested revision: `04d12e14e79d7fef4048f050af15cf2fc804e62e` (incorporating PR #2 `4a70881` and PR #3 `3aaeacc`, plus Member B integrations `ce5b1fc`, `6888fd1`, `f4c0542`, `7f11e0e`).

| Check | Revision and scope | Reported result |
| --- | --- | --- |
| Frontend suite | `04d12e1` clean checkout | 114 tests passed in 10 files. TypeScript typecheck, OpenAPI contract bindings check, Vite production build, and git diff check all passed cleanly. |
| Real browser smoke (`npm run smoke:real`) | `04d12e1` with fresh disposable data and `real` profile | Exit 1 at gateway readiness gate. Mock bank started on loopback, B SDK `pay-bill` CLI validated, and Chromium contexts launched for Customer and Ops. Gateway startup was refused by fail-closed guard (`deps.validate_for_profile()`): missing real `identity_verifier`, `human_auth`, and `audit_log`. Both browser logins failed with no HTTP response (`UI_GATEWAY_UNREACHABLE`). Browser destination check confirmed requests strictly limited to UI and gateway. |
| Persisted debit and balance assertion | `04d12e1` provider-side assertion | **Unavailable**. Because the real gateway refused startup, no transaction was dispatched to the bank mock; provider-side ledger debit and balance delta assertions cannot execute against live endpoints. |
| Provider blocker details | `04d12e1` composition root | First failed endpoint: `GET /health/ready`. Sanitized error: `real startup refused; missing identity_verifier, human_auth, audit_log; no HTTP readiness response`. Owner: Member A (composition root / `gateway/deps.py`) and Member B (real identity/audit provider wiring). |

## D07 setup check

- Base `04d12e14e79d7fef4048f050af15cf2fc804e62e`.
- From `frontend/`, `npm ci` passed and `npm run dev:mock -- --strictPort` serves Vite at `127.0.0.1:5173`.
- Headless Chromium checked the mock banner and signed in on both routes. Customer showed **Your accounts** and Ops showed **Registered agents**. Chromium automation used Playwright from the local install.
- The real D06 smoke exit-1 result is documented above. This fixture/mock check contributes zero real rehearsals.

## Integrated scenario results

For each row, record the revision, profile, isolated seed, evidence location, and observed HTTP/state, ledger, mandate and audit results as applicable. Acceptance values come from the [team distribution §13.2](../TEAM_TASK_DISTRIBUTION%20%2828%29.md).

Tested revision: `04d12e14e79d7fef4048f050af15cf2fc804e62e` via `pytest tests/integration/test_scenarios.py` (21 passed, 1 skipped).

| ID | Acceptance checkpoint | Real result | Evidence |
| --- | --- | --- | --- |
| S1 | ₹1,850 executes once with matching balance, usage and audit | Passed (Fixture integration) | `test_scenario_s01_happy_path`: initial 12,500,000 paise -> 12,315,000 paise (-185,000 paise debit). Mandate remaining: 815,000 paise; committed: 185,000 paise. HTTP, ledger, usage, and audit all verified. |
| S2a–d | ₹7,000 cap denial, payee/action denials and probing suspension | Passed (Fixture integration) | `test_scenario_s02_scope_denials`: S2a ₹7,000 cap exceeded (`E_MANDATE_TRANSACTION_LIMIT`), S2b unauthorized payee (`E_MANDATE_PAYEE_NOT_ALLOWED`), S2c unauthorized action (`E_MANDATE_ACTION_NOT_ALLOWED`), S2d probe threshold breach suspends agent (`E_AGENT_SUSPENDED`). |
| S3 | Five parallel ₹1,500 requests, exactly two executions per run across 100 runs | Passed (Fixture integration) | `test_scenario_s03_concurrency_race`: 5 concurrent requests; exactly 2 executed (300,000 paise total), 3 denied (`E_MANDATE_BUDGET_EXHAUSTED`). Ledger moved to 9,500,000 paise. Zero overspend. |
| S4 | 90-second mandate expiry and boundary cases | Passed (Fixture integration) | `test_scenario_s04_expiry_and_boundaries`: Pre-window rejected, active window accepted, expired window rejected (`E_MANDATE_EXPIRED`). |
| S5 | Immediate revocation, token invalidation and cancelled pending approval | Passed (Fixture integration) | `test_scenario_s05_revocation`: Revocation endpoint cancels pending approvals, subsequent requests rejected (`E_MANDATE_REVOKED`), token invalidation confirmed. |
| S6a–h | Identity, proof, key, binding and route-role vectors | Passed (Fixture integration) | `test_scenario_s06_identity_vectors`: S6a unregistered key denied, S6b wrong private key denied, S6c replayed proof denied, S6d expired proof denied, S6e wrong audience/origin denied, S6f token/key binding mismatch denied, S6g customer accessing ops route denied, S6h ops modifying customer mandate denied. |
| S7 | ₹40,000 unverified transfer denied and agent suspended | Passed (Fixture integration) | `test_scenario_s07_prompt_injection`: Indirect prompt injection transfer attempt to PAYEE-X denied (`E_RISK_BREAKER_TRIGGERED`), agent status transitioned to SUSPENDED. |
| S8 | Six ₹1,990 attempts, suspension and at most one execution | Passed (Fixture integration) | `test_scenario_s08_structuring`: Velocity burst detected; 1st approved, 2nd-3rd escalated, 4th triggers breaker and suspends agent. Zero unapproved leakage. |
| S9a–e | ₹4,800 approval and receipt, reject/expiry, reuse/hash and PIN/spam cases | Passed (Fixture integration) | `test_scenario_s09_approval_lifecycle`: S9a approved with PIN 4821, executed once with receipt; S9b rejection yields zero debit; S9c expiry yields zero debit; S9d double-decision idempotency verified; S9e invalid PIN denied without debit. |
| S10a–d | Content edit, deletion, unsigned rehash and truncation below retained anchor | Passed (Fixture integration) | `test_scenario_s10_audit_tamper`: Verification detects payload modification (S10a), entry deletion (S10b), rehash without signing key (S10c), and truncation below anchor (S10d). |
| S11 | Historical evidence and stated verification scope | Passed (Fixture integration) | `test_scenario_s11_evidence_bundle`: Complete dispute bundle verified containing mandate, agent identity, signed proof, risk assessment, bank result, and audit signature. |
| S_REAL | Full live end-to-end real stack execution | Skipped | `test_scenarios_real_stack_placeholder` skipped: Real gateway composition wiring pending Member A & B integration. |

Measurements remain pending for live real-stack race overspends, live p95 decision latency excluding human wait, verification duration for 1,000 entries on live database, and anchored tamper coverage on physical deployment.

## Real rehearsals and handoff

| Gate | Date and operator | Revision and clean seed | Result and evidence |
| --- | --- | --- | --- |
| Real rehearsal 1 | Pending | `04d12e1` | Pending (zero real rehearsals claimed; automated smoke does not constitute a rehearsal) |
| Real rehearsal 2 | Pending | `04d12e1` | Pending |
| Real rehearsal 3 | Pending | `04d12e1` | Pending |
| Independent teammate clean-reset run | Pending | `04d12e1` | Pending |
| Physical projector verification | Pending | `04d12e1` | Pending |

**Successful recording location** · Pending. Record the retrievable location and tested revision after a successful rehearsal. Store the recording outside Git.

## D08 frontend walkthrough

- D07 checkpoint and D08 base: `99f737cb8965dbb3169ca18ae097a0cad472f6f7`. D08 used the mock Vite startup in the README with two separate Chromium contexts.
- Inspected 1280×720 and 1920×1080 full viewport, plus 640×720 and 960×1080 half-width viewports for side-by-side windows. The approval card showed ₹4,800.00, PENDING, masked account, reasons, and the separately labelled escaped unverified note. The mandate form reached review with fingerprint, payee, caps and expiry. Ops showed ALLOW/ESCALATE decisions, request/agent IDs, ACTIVE/SUSPENDED status, and the verification report through anchor #5 with a consistent suffix.
- The audit fixture `req_mock_s11` opened and its downloaded JSON contained `mnd_mock_historical`. It is independent of the approval fixture, so this walkthrough cannot demonstrate matching S9 receipt and S11 evidence. S1, S2, S6, S7, S8, real S9, S5, and real tamper detection need integrated providers and scenario drivers.
- At 640 pixels the evidence is a dense, wrapped JSON block with small monospace text. The guide now asks the presenter to maximize Ops for evidence. The audit table timestamp wraps at 640 pixels. No document-level horizontal overflow was observed. Audit, review and receipt panels require vertical scrolling.
- The mock indicator scrolled out of view at 1280×720 during the first inspection. It is now sticky and was rechecked after scrolling at all four viewport sizes. Local screenshots are in the system temporary `agentgate-d08` directory.
- The D08 checkout passed 111 frontend tests across 10 files. API bindings check, typecheck, and build also passed. The 114-test result above belongs to the separate D06 checkout.
- This is a fixture walkthrough with **zero real rehearsals**. Physical projector, actual audience readability, independent clean reset, three real rehearsals, real gateway confirmation and a backup recording remain pending.
