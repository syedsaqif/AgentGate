# Member D verification — 21 November 2026 IST

Acceptance follows §11 of the [team distribution](../TEAM_TASK_DISTRIBUTION%20%2828%29.md) and the research plan’s seven-minute demo. The integration started at `c1877a2141a7ef43d3bffde70c30cbce51d68c59`. All remote teammate branch tips, including the heads of PRs #2 and #3, were already ancestors. At that revision all eight providers reported real mode. The original smoke created a mandate and exited 1 because its parent process lacked the SDK key path and Ops login inputs.

| Milestone | Verification |
| --- | --- |
| D01 | Verified both browser sign-ins on the real profile. Fixture isolation, generated bindings, TypeScript and the Vite production build passed. |
| D02 | Verified live mandate creation, usage and refetched revocation. Fixture tests cover amount parsing, IST display and form validation. |
| D03 | Verified the PIN approval, one POST, executed receipt and matching Customer SSE. Component tests cover remaining display states. The real-provider ASGI test covers reject-and-suspend and scoped revocation. |
| D04 | Verified the live request, score, registry suspension and reinstatement in Ops. Disconnect, resync and duplicate-event behavior have fixture coverage. |
| D05 | Verified live chain report and downloaded evidence against the Customer receipt. Provider tests on disposable copies cover four tamper cases. The smoke reports its actual retained checkpoint coverage. |
| D06 | Verified one fresh ₹4,800 payment using the browser-created mandate and the signed SDK. See the correlation record below. |
| D07 | Windows startup, separate workspaces, seven-minute script and results content verified or prepared. An independent teammate clean-reset run and final presentation remain pending. |
| D08 | Automated real browser smoke and scenario status verified. Three presenter rehearsals, a physical projector check and a full backup recording remain pending. |

## Correlation record

One successful isolated Windows run produced the following public identifiers. Every execution of the smoke generates new IDs. GitHub Actions also prints the evidence for its exact checkout.

| Item | Observed |
| --- | --- |
| Customer mandate | `mnd_5b081ab42229`, historical snapshot ACTIVE, refreshed list REVOKED |
| Signed request and approval | `req_208f1f5bf5794f04`, `esc_req_208f1f5bf5794f04`, one decision POST, HTTP 200, approval audit sequence 6 |
| Bank outcome | `bank_op_f54d1de2a20c`, one debit `led_a12478e17428` of 480000 paise, matching signed reference and persisted bank operation |
| Balance and usage | 12500000 → 12020000 paise, one committed use / 480000 paise, zero reserved uses / zero paise |
| Customer and Ops receipt | Sequence 8, `042ea4cb24bf4b226322d4c5623df6b9046d33feb4b8f91006b93bf10dde00b5`, matched by decision response, Customer stream and downloaded evidence |
| Report | Valid through entry 10, retained checkpoint through 0 |

## Test scope

The configured `uv run mypy` contract check passed for 22 files. The wider README diagnostic, `mypy contracts gateway simulator tools tests/c tests/a`, exited 1 with 172 errors across 22 files. The same diagnostic at the starting SHA reported 174 errors across 23 files. This repository-wide typing backlog remains open.

The Python suite passed 247 tests with zero skips. The previous real-stack placeholders now run as ASGI integration tests, including the signed S1/S9 scenario and execution handoff. The real browser smoke separately uses HTTP, two service processes and disk-backed SQLite. The frontend suite passed 114 tests across 10 files. Frozen contracts, golden vectors, generated bindings, strict contract typing, Ruff and the production build passed. Starlette emits a TestClient/httpx deprecation warning.

The scripted simulator passed all 18 fixture rows across S1–S11, including 100 simulated S3 races. The general-purpose SDK scenario driver is unavailable. The verified live browser path covers S9a and the corresponding S11 evidence. The metrics tool times synchronous risk scoring and a synthetic SHA-256 chain. End-to-end latency, 1,000 signed entries on the live service, and a full live run of every scenario remain unmeasured.

Earlier D08 fixture walkthroughs inspected 1280×720 and 1920×1080 plus half-width windows. Evidence JSON wraps at 640 pixels; maximize Ops for the audience. Recorded presenter rehearsals 0/3. Independent teammate runs 0. Physical projector checks 0. Backup recordings 0.
