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
