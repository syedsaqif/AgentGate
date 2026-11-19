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
