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

## D06 evidence carried forward

| Check | Revision and scope | Reported result |
| --- | --- | --- |
| Frontend | `468237b25976bfd0beb58d2ec546150fbaff8d48` plus the local unstaged D06 work | 114 tests passed in 10 files. Dependency install, API bindings check, typecheck, build, and diff check passed. Vitest reported a jsdom performance advisory. |
| Real browser smoke | Same D06 frontend with integrated backend `3a18e749056c4af1a46c6efa2c0385db6394970f` | Exit 1 before gateway readiness. The simulated bank started and both Chromium routes rendered. Both sign-in requests got no HTTP response. No live mandate, payment or receipt was produced. |
| Provider and fixture checks | D06 integrated checkout, separate test operations | A's simulated-bank same-key test verified one 185,000-paise ledger entry and matching balance delta. C's S9 fixture verified a 480,000-paise balance reduction. The D06 operation and live ledger are pending. |

Evidence source is the prior D06 report and local D06 runbook in the `track-d/D06-real-smoke` worktree. B's `f4c0542` CLI was inspected as a reference and its dispatch was checked with a stub client. Integrated `3a18e74` still contains the SDK and seed placeholders.

## D07 setup check

- Base `468237b25976bfd0beb58d2ec546150fbaff8d48`, which contains fetched `origin/main` at `3a18e74`.
- From `frontend/`, `npm ci` passed and `npm run dev:mock -- --strictPort` served Vite at `127.0.0.1:5173`.
- Headless Chromium checked the mock banner and signed in on both routes. Customer showed **Your accounts** and Ops showed **Registered agents**. Chromium automation used Playwright from the local D06 install.
- The D06 smoke command depends on the unstaged D06 changes. Its prior exit-1 result is carried forward. This mock check contributes zero real rehearsals.

## Integrated scenario results

For each row, record the revision, profile, isolated seed, evidence location, and observed HTTP/state, ledger, mandate and audit results as applicable. Acceptance values come from the [team distribution §13.2](../TEAM_TASK_DISTRIBUTION%20%2828%29.md).

| ID | Acceptance checkpoint | Real result | Evidence |
| --- | --- | --- | --- |
| S1 | ₹1,850 executes once with matching balance, usage and audit | Pending | Pending |
| S2a–d | ₹7,000 cap denial, payee/action denials and probing suspension | Pending | Pending |
| S3 | Five parallel ₹1,500 requests, exactly two executions per run across 100 runs | Pending | Pending |
| S4 | 90-second mandate expiry and boundary cases | Pending | Pending |
| S5 | Immediate revocation, token invalidation and cancelled pending approval | Pending | Pending |
| S6a–h | Identity, proof, key, binding and route-role vectors | Pending | Pending |
| S7 | ₹40,000 unverified transfer denied and agent suspended | Pending | Pending |
| S8 | Six ₹1,990 attempts, suspension and at most one execution | Pending | Pending |
| S9a–e | ₹4,800 approval and receipt, reject/expiry, reuse/hash and PIN/spam cases | Pending | Pending |
| S10a–d | Content edit, deletion, unsigned rehash and truncation below retained anchor | Pending | Pending |
| S11 | Historical evidence and stated verification scope | Pending | Pending |

Measurements remain pending for race overspends, p95 decision latency excluding human wait, verification duration for 1,000 entries, and anchored tamper coverage. Record environment, revision, dataset seed, and denominators with the measured values.

## Real rehearsals and handoff

| Gate | Date and operator | Revision and clean seed | Result and evidence |
| --- | --- | --- | --- |
| Real rehearsal 1 | Pending | Pending | Pending |
| Real rehearsal 2 | Pending | Pending | Pending |
| Real rehearsal 3 | Pending | Pending | Pending |
| Independent teammate clean-reset run | Pending | Pending | Pending |

**Successful recording location** · Pending. Record the retrievable location and tested revision after a successful rehearsal. Store the recording outside Git.

## D08 frontend walkthrough

- D07 checkpoint and D08 base: `99f737cb8965dbb3169ca18ae097a0cad472f6f7`. D08 used the mock Vite startup in the README with two separate Chromium contexts.
- Inspected 1280×720 and 1920×1080 full viewport, plus 640×720 and 960×1080 half-width viewports for side-by-side windows. The approval card showed ₹4,800.00, PENDING, masked account, reasons, and the separately labelled escaped unverified note. The mandate form reached review with fingerprint, payee, caps and expiry. Ops showed ALLOW/ESCALATE decisions, request/agent IDs, ACTIVE/SUSPENDED status, and the verification report through anchor #5 with a consistent suffix.
- The audit fixture `req_mock_s11` opened and its downloaded JSON contained `mnd_mock_historical`. It is independent of the approval fixture, so this walkthrough cannot demonstrate matching S9 receipt and S11 evidence. S1, S2, S6, S7, S8, real S9, S5, and real tamper detection need integrated providers and scenario drivers.
- At 640 pixels the evidence is a dense, wrapped JSON block with small monospace text. The guide now asks the presenter to maximize Ops for evidence. The audit table timestamp wraps at 640 pixels. No document-level horizontal overflow was observed. Audit, review and receipt panels require vertical scrolling.
- The mock indicator scrolled out of view at 1280×720 during the first inspection. It is now sticky and was rechecked after scrolling at all four viewport sizes. Local screenshots are in the system temporary `agentgate-d08` directory.
- The D08 checkout passed 111 frontend tests across 10 files. API bindings check, typecheck, and build also passed. The 114-test result above belongs to the separate D06 checkout.
- This is a fixture walkthrough with **zero real rehearsals**. Physical projector, actual audience readability, independent clean reset, three real rehearsals, real gateway confirmation and a backup recording remain pending.
