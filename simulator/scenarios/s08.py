"""Scenario S8: Rapid structuring attempts and circuit breaker trip.

Six ₹1,990 attempts spaced ~20 seconds apart:
- Attempt 1: LOW band (ALLOW) -> executes.
- Attempts 2 & 3: MEDIUM band (ESCALATE) -> step-up approval pending.
- Attempt 4: HIGH band (DENY) -> triggers circuit breaker and agent suspension.
- Breaker action: Pending approvals from attempts 2 & 3 are CANCELLED.
- Attempts 5 & 6: Denied immediately (agent is suspended).
- Outcome: At most ONE payment executes (199,000 paise).
- Verifies bands and payment counts (not fragile exact scores).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from contracts.common import ActionType
from contracts.core_models import ActionParams, CanonicalAction, Intent
from simulator.client_port import ScenarioClient, ScenarioResult
from simulator.personas import (
    ACCOUNT_INITIAL_BALANCE_PAISE,
    ACCOUNT_PRIMARY,
    AGENT_BILLPAL,
    CUSTOMER_ID_PRIMARY,
    CUSTOMER_PIN_PRIMARY,
    PAYEE_ELECTRICITY,
)

MANDATE_ID = "mnd_demo_1"
INITIAL_BUDGET_PAISE = 1_000_000
STRUCTURING_AMOUNT_PAISE = 199_000  # ₹1,990.00


async def run_s08(
    client: ScenarioClient,
    now: datetime | None = None,
) -> ScenarioResult:
    """Execute scenario S8 verifying structuring escalation, breaker trip, and cancellation."""
    current_time = now or datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)
    details: dict[str, Any] = {}

    await client.reset_state()

    cust_res = await client.customer_login(CUSTOMER_ID_PRIMARY, CUSTOMER_PIN_PRIMARY)
    session_token = cust_res.get("session_token", f"sess_{CUSTOMER_ID_PRIMARY}")

    tok_res = await client.request_token(AGENT_BILLPAL.agent_id, MANDATE_ID)
    token = tok_res.get("access_token")

    attempt_results: list[dict[str, Any]] = []
    created_escalation_ids: list[str] = []

    # Fire 6 attempts spaced 20 seconds apart
    for i in range(1, 7):
        t_req = current_time + timedelta(seconds=(i - 1) * 20)
        req_id = f"req_s08_structuring_{i:03d}"
        action = CanonicalAction(
            request_id=req_id,
            customer_id=CUSTOMER_ID_PRIMARY,
            agent_id=AGENT_BILLPAL.agent_id,
            mandate_id=MANDATE_ID,
            mandate_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
            action_type=ActionType.BILL_PAY,
            params=ActionParams(
                from_account=ACCOUNT_PRIMARY.account_id,
                payee_id=PAYEE_ELECTRICITY.payee_id,
                amount_paise=STRUCTURING_AMOUNT_PAISE,
                currency="INR",
                reference=f"Rapid Bill Attempt {i}",
            ),
            intent=Intent(
                task="Pay electricity bill",
                category="utilities",
                note=f"Rapid structuring payment {i}",
            ),
            idempotency_key=f"idemp_s08_{i:03d}",
            received_at=t_req,
        )

        res = await client.send_action(action, token=token)
        attempt_results.append(res)
        if res.get("escalation_id"):
            created_escalation_ids.append(res["escalation_id"])

    # Analyze responses per attempt
    res1, res2, res3, res4, res5, res6 = attempt_results

    # Attempt 1: Band LOW, status EXECUTED
    r1_ok = (
        res1.get("decision") == "ALLOW"
        and res1.get("status") == "EXECUTED"
        and res1.get("risk", {}).get("level") == "LOW"
    )

    # Attempts 2 & 3: Band MEDIUM, status PENDING_APPROVAL
    r2_ok = (
        res2.get("decision") == "ESCALATE"
        and res2.get("status") == "PENDING_APPROVAL"
        and res2.get("risk", {}).get("level") == "MEDIUM"
    )
    r3_ok = (
        res3.get("decision") == "ESCALATE"
        and res3.get("status") == "PENDING_APPROVAL"
        and res3.get("risk", {}).get("level") == "MEDIUM"
    )

    # Attempt 4: Band HIGH, status REJECTED/DENY, breaker tripped
    r4_ok = (
        res4.get("decision") == "DENY"
        and res4.get("status") == "REJECTED"
        and res4.get("risk", {}).get("level") == "HIGH"
        and res4.get("risk", {}).get("breaker_recommended") is True
    )

    # Attempts 5 & 6: Denied by agent suspension
    r5_ok = res5.get("error", {}).get("code") in ("E_AUTH_AGENT_SUSPENDED", "E_RISK_BREAKER_OPEN")
    r6_ok = res6.get("error", {}).get("code") in ("E_AUTH_AGENT_SUSPENDED", "E_RISK_BREAKER_OPEN")

    # Verify pending approvals from attempts 2 & 3 were CANCELLED by breaker
    pending_cancelled = True
    for esc_id in created_escalation_ids:
        esc_rec = await client.get_escalation(esc_id)
        if esc_rec.get("state") != "CANCELLED":
            pending_cancelled = False

    http_verified = r1_ok and r2_ok and r3_ok and r4_ok and r5_ok and r6_ok and pending_cancelled
    details["attempts"] = [
        {"attempt": 1, "ok": r1_ok, "res": res1},
        {"attempt": 2, "ok": r2_ok, "res": res2},
        {"attempt": 3, "ok": r3_ok, "res": res3},
        {"attempt": 4, "ok": r4_ok, "res": res4},
        {"attempt": 5, "ok": r5_ok, "res": res5},
        {"attempt": 6, "ok": r6_ok, "res": res6},
    ]
    details["pending_cancelled"] = pending_cancelled

    # Dimension 2: Bank Ledger (exactly 1 execution: 12,500,000 -> 12,301,000 paise)
    bal_res = await client.get_balance(ACCOUNT_PRIMARY.account_id)
    post_balance = bal_res.get("balance_paise", 0)
    expected_balance = ACCOUNT_INITIAL_BALANCE_PAISE - STRUCTURING_AMOUNT_PAISE
    ledger_verified = post_balance == expected_balance

    # Dimension 3: Mandate Usage (exactly 199,000 committed, 801,000 remaining)
    mnds_res = await client.get_customer_mandates(session_token)
    mnd_items = {m["mandate_id"]: m for m in mnds_res.get("items", [])}
    mnd = mnd_items.get(MANDATE_ID, {})
    expected_remaining = INITIAL_BUDGET_PAISE - STRUCTURING_AMOUNT_PAISE
    usage_verified = (
        mnd.get("committed_paise") == STRUCTURING_AMOUNT_PAISE
        and mnd.get("remaining_paise") == expected_remaining
    )

    # Dimension 4: Audit Trail (exactly 1 bank execution, breaker recommended, agent suspended)
    audit_res = await client.get_audit_entries(session_token)
    events = audit_res.get("items", [])
    bank_events = [e for e in events if e.get("event_type") == "bank.executed"]
    has_breaker = any(e.get("event_type") == "breaker.recommended" for e in events)
    has_suspension = any(e.get("event_type") == "agent.suspended" for e in events)
    audit_verified = len(bank_events) == 1 and has_breaker and has_suspension

    passed = http_verified and ledger_verified and usage_verified and audit_verified
    return ScenarioResult(
        scenario_id="S8",
        name="Structuring Sequence & Velocity Breaker Trip",
        passed=passed,
        http_verified=http_verified,
        ledger_verified=ledger_verified,
        usage_verified=usage_verified,
        audit_verified=audit_verified,
        message="S8 passed" if passed else "S8 structuring verification failed",
        details=details,
    )


__all__ = ["run_s08"]
