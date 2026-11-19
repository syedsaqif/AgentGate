# Member C Handoff & Technical Notes

## 1. Database & Persistence
- `gateway/risk/models.py` defines a local `Base = DeclarativeBase()` for Member C tables (`risk_attempts`, `risk_baselines`). When Member A provides the shared `Base` in `gateway/platform/db.py`, these models can inherit from it directly.
- `gateway/risk/schema.py` exposes `register_risk_tables(metadata)` for Member A's composition root.

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
