# Member C Handoff & Technical Notes

## 1. Database & Persistence
- `gateway/risk/models.py` defines a local `Base = DeclarativeBase()` for Member C tables (`risk_attempts`, `risk_baselines`). When Member A provides the shared `Base` in `gateway/platform/db.py`, these models can inherit from it directly.
- `gateway/risk/schema.py` exposes `register_risk_tables(metadata)` for Member A's composition root.
- `gateway/escalation/models.py` defines `EscalationRecord` mapping to table `escalations`.
- `gateway/escalation/schema.py` exposes `register_escalation_tables(metadata)` for Member A's composition root.

## 2. Robust Statistics & Edge Cases
- **Median & MAD (Median Absolute Deviation)**:
  - Computed on confirmed successful transaction amounts in integer paise.
  - Robust z-score: `0.6745 * |amount - median| / MAD`.
- **Zero-History / Warm-up (< 10 samples)**:
  - Robust z-score is undefined and set to `0.0`.
  - Prior rules apply: amounts are compared against `confirm_above` or `max_per_txn_paise`.
- **MAD = 0 (Identical historical payments)**:
  - If `amount == median`, z-score is `0.0`.
  - If `amount != median`, fallback scale uses `max(10% of median, 100 paise)` to prevent division by zero while correctly flagging sudden step-ups.

## 3. History Isolation & Baseline Protection
- **Normal baseline**: Updated exclusively from confirmed successful executions (`status == 'EXECUTED'`).
- **Denied & pending attempts**: Recorded in `risk_attempts` and tracked in recent attempt/velocity counters, but never added to the normal-success baseline.
- **Auth-failed traffic**: Tagged with `is_auth_failure=True` (and attributed to `unauthenticated`); never attributed to verified customer/agent baseline history.
- **Failed / uncertain bank outcomes**: Never enter the successful baseline.
- **Idempotency**: Attempts are deduplicated on `attempt_id`.

## 4. Signal Catalog & Noisy-OR Combination (Task C03)
- **10 Deterministic Signals** (`gateway/risk/signals.py`):
  1. `new_payee` (severity 50, weight 0.40)
  2. `amount_outlier` (severity 80, weight 0.625)
  3. `near_limit_structuring` (severity 60, weight 0.40)
  4. `velocity` (severity 70, weight 0.50)
  5. `request_rate` (severity 50, weight 0.40)
  6. `off_pattern_time` (severity 40, weight 0.25)
  7. `sequence_anomaly` (severity 50, weight 0.30)
  8. `probing` (severity 95, weight 0.90) — hard trip
  9. `intent_mismatch` (severity 75, weight 0.60)
  10. `read_enumeration` (severity 60, weight 0.35)
- **Noisy-OR Formula**:
  $$P = 1 - \prod_{i \in \text{fired}} \left(1 - w_i \cdot \frac{s_i}{100}\right), \quad \text{Score} = \text{round}(100 \times P)$$
- **Profile Thresholds**:
  - `balanced`: $35 / 70$
  - `conservative`: $25 / 60$
  - `permissive`: $45 / 80$
- **Calibration**:
  - Ordinary ₹1,850 electricity bill with seeded history: score $< 35$ (**LOW** / `ALLOW`).
  - Unusual ₹4,800 payment: score $50$ (**MEDIUM** / `ESCALATE`, approvable step-up).
- **Rule Neutrality**: No rule checks `scenario_id`.

## 5. Escalation & Approval Lifecycle (Task C05)
- **Table ownership**: Member C writes exclusively to `escalations`.
- **Atomic state transitions**:
  - `PENDING -> APPROVED`: Requires valid customer PIN via `HumanAuth.verify_pin`. Emits `escalation.approved` audit event.
  - `APPROVED -> EXECUTED`: Single-use atomic `claim_execution` verifying `action_hash`. Hands off to `ExecutionPort.execute_stored`.
  - `PENDING -> REJECTED`: Terminal rejection. Optional `suspend_agent=True` invokes `Lifecycle.suspend_customer_delegation`.
  - `PENDING -> EXPIRED`: Swept by `expire_due` on startup and periodically.
  - `PENDING / APPROVED -> CANCELLED`: Invoked on mandate revocation (`cancel_for_mandate`) or agent suspension (`cancel_for_agent`).
- **Pending limit**: Maximum 3 pending escalations per mandate. 4th attempt returns `E_ESC_LIMIT_REACHED` (HTTP 409).
- **Trusted summary security**:
  - Account numbers masked (`**1234`).
  - Payee verified against trusted registry.
  - Agent free-text note strictly segregated under `agent_note` with `"unverified": true`. Never merged into trusted fields.
- **Enumeration defense**: Queries for non-existent IDs or unauthorized subjects return identical HTTP 404 responses.

## 6. Execution Handoff & Crash Recovery (Task C06)
- **Pre-dispatch constraint re-evaluation (`claim_execution`)**:
  - Validates stored action hash matches expected authority projection.
  - Validates live mandate status is `ACTIVE` and within validity window via `MandateReader`.
  - Validates live agent status is `ACTIVE` via `AgentRegistry`.
  - Validates approval deadline has not expired (`expires_at > now`).
  - Validates remaining money budget and transaction count via `UsageReader`.
  - Enforces single-use claim: subsequent claims raise `E_ESC_ALREADY_CONSUMED`.
- **Handoff guarantees (`execute_approved`)**:
  - Passes only stored IDs (`request_id`, `escalation_id`) to `ExecutionPort.execute_stored`.
  - Strictly forbids parameter replacement or reconstructing actions from incoming client payloads.
  - Never invokes `decide()` recursively from execution.
  - Returns stored outcome on duplicate invocations without repeating bank operations.
  - Maps bank outcomes: `EXECUTED` -> `state = EXECUTED`, `REJECTED`/`FAILED` -> `state = FAILED`, `EXECUTION_UNKNOWN` -> retains unknown outcome without being marked rejected.
- **Crash recovery (`recover_undispatched`)**:
  - Scans for `APPROVED`-but-undispatched records on startup.
  - Re-evaluates every constraint before dispatch to ensure authority has not been revoked during outage.
  - Dispatches each record exactly once.

## 7. Contract Questions & Integration Notes
- **PIN & Lifecycle separation**: `contracts/escalation_ports.py` defines `EscalationService.decide(escalation_id, decision, subject_id, tx)`. To preserve clean separation of concerns, `HumanAuth.verify_pin(customer_id, pin)` is evaluated in the route handler prior to calling `decide()`. If wrong PIN is supplied, the escalation remains `PENDING`, an `auth.pin_failed` audit event is emitted, and HTTP 403 `E_ESC_BAD_PIN` is returned.
- **Reject-and-suspend coordination**: If a customer selects `suspend_agent=true` on rejection, the route calls `Lifecycle.suspend_customer_delegation` to suspend that customer's delegation without affecting other customers.

## 8. Scenario Runner Foundation & Scenarios S1-S4 (Task C07a)
- **Deterministic Orchestration**:
  - `simulator/runner.py`: CLI entry point supporting `python -m simulator.runner --all --profile scripted`.
  - Clean state isolation: Each scenario begins with `reset_state()` (or `POST /v1/dev/reset` under scripted profile).
  - Deterministic clock: Clock is injected across all scenarios without real sleeps.
  - Zero rule coupling: No risk signal or policy rule inspects `scenario_id`.
- **Four Verification Dimensions**:
  Every scenario asserts all four dimensions directly without relying only on labels:
  1. `HTTP responses`: Status codes, verdicts (`ALLOW`, `DENY`, `ESCALATE`), error reason codes, and audit references.
  2. `Bank ledger`: Exact integer paise balances before and after operations.
  3. `Mandate usage`: Committed paise, remaining paise, and lifetime budget assertions.
  4. `Audit trail`: Required event sequence (`identity.verified`, `policy.evaluated`, `risk.scored`, `bank.executed` or `policy.denied`).
- **Scenarios S1 through S4**:
  - **S1 (Happy Path)**: ₹1,850 electricity payment to `BILL-ELEC`. Balance: 12,500,000 -> 12,315,000 paise. Mandate budget: 1,000,000 -> 815,000 remaining, 185,000 committed. Audit trail shows identity, policy, risk, and bank result.
  - **S2a-c (Scope Denials)**:
    - *S2a*: ₹7,000 payment denied by per-transaction cap (`E_POLICY_AMOUNT_EXCEEDS_CAP`).
    - *S2b*: ₹1,850 payment to unlisted `BILL-MOBILE` denied (`E_POLICY_PAYEE_NOT_ALLOWED`).
    - *S2c*: Unauthorized `TRANSFER` action type denied (`E_POLICY_ACTION_NOT_ALLOWED`).
    - *Invariance*: Ledger balance (12,500,000 paise) and mandate usage (0 committed, 1,000,000 remaining) unchanged across all denials. Zero bank contact.
  - **S3 (High-Concurrency Race)**:
    - Budget: 400,000 paise, per-transaction cap: 200,000 paise.
    - Five parallel 150,000 paise requests with unique idempotency keys.
    - Exactly two execute (spending 300,000 paise), and three fail with `E_BANK_INSUFFICIENT_FUNDS`.
    - Repeated and verified across 100 randomized iterations.
  - **S4 (Mandate Expiry & Temporal Boundaries)**:
    - 90-second mandate validity window ($T_0$ to $T_0 + 90s$).
    - $T_0 - 10s$: Premature action denied with `E_MANDATE_REVOKED` (not yet valid).
    - $T_0 + 30s$: Active action succeeds (`ALLOW` / `EXECUTED`).
    - $T_0 + 90s$: Boundary action evaluated at exact deadline.
    - $T_0 + 91s$: Expired action denied with `E_MANDATE_REVOKED`.
- **Provider Validation & Staged Execution**:
  - Phase 1 runs against `FixtureScenarioClient` to debug orchestration logic.
  - Phase 2 inspects runtime providers against the active profile (`scripted` / `real`). If any provider is a fake or missing (e.g. `agent_sdk.client` placeholder, `FakeBank`), the runner explicitly reports which provider is a fake and marks real-stack scenarios as skipped pending Member A and B deliverables.

## 9. Advanced Security & Attack Scenarios S5-S8 & S2d (Task C07b)
- **S2d (Probing & Rapid Denials)**:
  - 5 hard policy denials within 10 minutes trigger circuit breaker suspension at or before the 5th attempt.
  - The subsequent protected action fails immediately (`E_AUTH_AGENT_SUSPENDED` / `E_RISK_BREAKER_OPEN`).
  - Audit trail explains denial history and breaker trip event.
- **S5 (Mandate Revocation & Approval Cancellation)**:
  - Revocation acknowledgement returned only after status `REVOKED`, tokens, and pending approvals are cancelled in one atomic unit.
  - Subsequent requests under revoked mandate fail immediately (`E_MANDATE_REVOKED`).
  - Cancelled approvals cannot be approved or dispatched; zero bank operations.
- **S6 (Identity & Cryptographic Vectors - Member C drives, B owns checks)**:
  - Validates authentication error vectors aligned with `contracts/fixtures/b/auth_audit_cases.json`:
    - Missing credentials (`E_AUTH_MISSING`, 401)
    - Replayed proof (`E_AUTH_REPLAY`, 401)
    - Key mismatch (`E_AUTH_KEY_MISMATCH`, 401)
    - Suspended agent (`E_AUTH_AGENT_SUSPENDED`, 403)
    - Unknown agent (`E_AUTH_INVALID_TOKEN`, 401)
    - Mandate binding mismatch (`E_AUTH_AGENT_MANDATE_MISMATCH`, 403)
    - Expired token (`E_AUTH_EXPIRED`, 401)
    - Tampered signature body (`E_AUTH_PROOF_INVALID`, 401)
    - Agent calling human customer routes (`E_AUTH_AGENT_SUSPENDED`, 403)
- **S7 (Prompt Injection & Malicious Redirection)**:
  - Scripted invoice attempts to redirect ₹40,000 to unverified `PAYEE-X`.
  - Fires `intent_mismatch`, `new_payee`, and `amount_outlier` signals.
  - Trips circuit breaker in HIGH band (`breaker_recommended=True`), globally suspending rogue agent.
  - Zero money moves; balance and usage remain intact.
- **S8 (Structuring & Velocity Breaker Trip)**:
  - Six ₹1,990 attempts spaced ~20s apart:
    - Attempt 1: LOW band (`ALLOW`) -> executes.
    - Attempts 2 & 3: MEDIUM band (`ESCALATE`) -> step-up approval pending.
    - Attempt 4: HIGH band (`DENY`) -> trips circuit breaker, suspending agent.
    - Breaker cancellation: Pending approvals for attempts 2 & 3 are cancelled.
    - Attempts 5 & 6: Denied immediately (agent suspended).
  - Assertions test risk bands and payment counts (at most one payment executes), not fragile exact scores.

## 10. Human-in-the-Loop Approvals & Evidence Scenarios S9-S11 (Task C07c)
- **S9a-e (Approval Lifecycle Variants)**:
  - *S9a (Approve)*: ₹4,800 step-up approved with customer PIN `4821`; stored action executes once; receipt returned.
  - *S9b (Reject)*: Customer rejects action; terminal status `REJECTED`; zero bank execution.
  - *S9c (Expire)*: Advance clock past 300s TTL; sweeper moves record to `EXPIRED`; zero bank execution.
  - *S9d (Reuse & Race)*: Duplicate approval returns `E_ESC_ALREADY_CONSUMED` (409); simultaneous approvals produce exactly one winner.
  - *S9e (PIN & Spam)*: Invalid PIN returns `E_ESC_BAD_PIN` (403), audits failure, leaves record pending; 4th pending creation on same mandate returns `E_ESC_LIMIT_REACHED` (409).
- **S10 (Audit Log Integrity & Tamper Verification)**:
  - Drives `client.verify_audit(session_token)`.
  - Verifies that genuine audit log validates with `valid == True`.
  - Tampered/modified audit block is detected by hash chain verification, returning `valid == False`.
- **S11 (Cryptographic Dispute Evidence Bundle)**:
  - Shared deliverable: Member C drives, Member B builds bundle, Member D renders views.
  - Asserts full JSON bundle: historical mandate terms & hash, public agent identity, cryptographic proof metadata, risk scoring diagnostics, approval metadata, and bank receipt.
