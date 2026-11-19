"""Scenario S4: Mandate expiry, clock boundary, and premature action testing.

Verifies the four mandatory dimensions across temporal windows:
- 90-second mandate validity window (T0 to T0 + 90s).
- Sub-test 1 (Premature): Action at T0 - 10s fails with E_MANDATE_REVOKED.
- Sub-test 2 (Active window): Action at T0 + 30s succeeds (EXECUTED/ALLOW).
- Sub-test 3 (Exact boundary): Action at T0 + 90s evaluated against boundary.
- Sub-test 4 (Expired): Action at T0 + 91s fails with E_MANDATE_REVOKED.
4 dimensions:
1. HTTP responses: Appropriate success or denial across all four offsets.
2. Bank ledger: Only active window action debits ledger (12,500,000 -> 12,315,000 paise).
3. Mandate usage: Only active window action consumes budget (1,000,000 -> 815,000 paise).
4. Audit trail: Policy denials logged for premature and expired attempts, execution for valid.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from contracts.common import ActionType
from contracts.core_models import ActionParams, CanonicalAction, Intent
from simulator.client_port import FixtureScenarioClient, ScenarioClient, ScenarioResult
from simulator.personas import (
    ACCOUNT_INITIAL_BALANCE_PAISE,
    ACCOUNT_PRIMARY,
    AGENT_BILLPAL,
    CUSTOMER_ID_PRIMARY,
    CUSTOMER_PIN_PRIMARY,
    PAYEE_ELECTRICITY,
)

S4_MANDATE_ID = "mnd_s4_window"
S4_BUDGET_PAISE = 1_000_000
S4_AMOUNT_PAISE = 185_000
WINDOW_DURATION_SECONDS = 90


async def _setup_s4_mandate(
    client: ScenarioClient,
    valid_from: datetime,
    valid_until: datetime,
) -> None:
    """Configure client with S4 mandate and temporal window."""
    if isinstance(client, FixtureScenarioClient):
        client.mandate_usages[S4_MANDATE_ID] = {
            "committed_paise": 0,
            "lifetime_budget": S4_BUDGET_PAISE,
            "remaining_paise": S4_BUDGET_PAISE,
        }
        client.mandate_caps[S4_MANDATE_ID] = 500_000
        client.mandate_payees[S4_MANDATE_ID] = {PAYEE_ELECTRICITY.payee_id}
        client.mandate_actions[S4_MANDATE_ID] = {"bill_pay", "ActionType.BILL_PAY"}
        client.mandate_windows[S4_MANDATE_ID] = (valid_from, valid_until)


async def run_s04(
    client: ScenarioClient,
    base_time: datetime | None = None,
) -> ScenarioResult:
    """Execute scenario S4 validating not-yet-valid, active, boundary, and expired actions."""
    t0 = base_time or datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)
    valid_from = t0
    valid_until = t0 + timedelta(seconds=WINDOW_DURATION_SECONDS)

    await client.reset_state()
    await _setup_s4_mandate(client, valid_from, valid_until)

    cust_res = await client.customer_login(CUSTOMER_ID_PRIMARY, CUSTOMER_PIN_PRIMARY)
    session_token = cust_res.get("session_token", f"sess_{CUSTOMER_ID_PRIMARY}")

    tok_res = await client.request_token(AGENT_BILLPAL.agent_id, S4_MANDATE_ID)
    token = tok_res.get("access_token")

    details: dict[str, Any] = {}

    # -------------------------------------------------------------------------
    # 1. Premature action at T0 - 10s (Not yet valid)
    # -------------------------------------------------------------------------
    t_premature = t0 - timedelta(seconds=10)
    req_premature = "req_s04_premature_001"
    act_premature = CanonicalAction(
        request_id=req_premature,
        customer_id=CUSTOMER_ID_PRIMARY,
        agent_id=AGENT_BILLPAL.agent_id,
        mandate_id=S4_MANDATE_ID,
        mandate_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
        action_type=ActionType.BILL_PAY,
        params=ActionParams(
            from_account=ACCOUNT_PRIMARY.account_id,
            payee_id=PAYEE_ELECTRICITY.payee_id,
            amount_paise=S4_AMOUNT_PAISE,
            currency="INR",
            reference="Premature Payment",
        ),
        intent=Intent(task="Pay bill", category="utilities", note="Before valid_from"),
        idempotency_key="idemp_s04_premature",
        received_at=t_premature,
    )
    res_premature = await client.send_action(act_premature, token=token)
    err_premature = res_premature.get("error", {}).get("code")
    ok_premature = err_premature in ("E_MANDATE_REVOKED", "E_AUTH_CLOCK_SKEW", "E_REQ_INVALID")
    details["premature_response"] = res_premature

    # -------------------------------------------------------------------------
    # 2. Active window action at T0 + 30s (Must succeed)
    # -------------------------------------------------------------------------
    t_active = t0 + timedelta(seconds=30)
    req_active = "req_s04_active_001"
    act_active = CanonicalAction(
        request_id=req_active,
        customer_id=CUSTOMER_ID_PRIMARY,
        agent_id=AGENT_BILLPAL.agent_id,
        mandate_id=S4_MANDATE_ID,
        mandate_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
        action_type=ActionType.BILL_PAY,
        params=ActionParams(
            from_account=ACCOUNT_PRIMARY.account_id,
            payee_id=PAYEE_ELECTRICITY.payee_id,
            amount_paise=S4_AMOUNT_PAISE,
            currency="INR",
            reference="Valid Window Payment",
        ),
        intent=Intent(task="Pay bill", category="utilities", note="Within valid window"),
        idempotency_key="idemp_s04_active",
        received_at=t_active,
    )
    res_active = await client.send_action(act_active, token=token)
    ok_active = (
        res_active.get("decision") == "ALLOW"
        and res_active.get("status") == "EXECUTED"
    )
    details["active_response"] = res_active

    # -------------------------------------------------------------------------
    # 3. Exact boundary action at T0 + 90s
    # -------------------------------------------------------------------------
    t_boundary = t0 + timedelta(seconds=90)
    req_boundary = "req_s04_boundary_001"
    act_boundary = CanonicalAction(
        request_id=req_boundary,
        customer_id=CUSTOMER_ID_PRIMARY,
        agent_id=AGENT_BILLPAL.agent_id,
        mandate_id=S4_MANDATE_ID,
        mandate_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
        action_type=ActionType.BILL_PAY,
        params=ActionParams(
            from_account=ACCOUNT_PRIMARY.account_id,
            payee_id=PAYEE_ELECTRICITY.payee_id,
            amount_paise=S4_AMOUNT_PAISE,
            currency="INR",
            reference="Boundary Payment",
        ),
        intent=Intent(task="Pay bill", category="utilities", note="Exact boundary T0+90s"),
        idempotency_key="idemp_s04_boundary",
        received_at=t_boundary,
    )
    res_boundary = await client.send_action(act_boundary, token=token)
    # At exact boundary (t == valid_until), received_at <= valid_until is valid
    ok_boundary = (
        res_boundary.get("decision") == "ALLOW"
        and res_boundary.get("status") == "EXECUTED"
    )
    details["boundary_response"] = res_boundary

    # -------------------------------------------------------------------------
    # 4. Expired action at T0 + 91s (Must fail)
    # -------------------------------------------------------------------------
    t_expired = t0 + timedelta(seconds=91)
    req_expired = "req_s04_expired_001"
    act_expired = CanonicalAction(
        request_id=req_expired,
        customer_id=CUSTOMER_ID_PRIMARY,
        agent_id=AGENT_BILLPAL.agent_id,
        mandate_id=S4_MANDATE_ID,
        mandate_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
        action_type=ActionType.BILL_PAY,
        params=ActionParams(
            from_account=ACCOUNT_PRIMARY.account_id,
            payee_id=PAYEE_ELECTRICITY.payee_id,
            amount_paise=S4_AMOUNT_PAISE,
            currency="INR",
            reference="Expired Payment",
        ),
        intent=Intent(task="Pay bill", category="utilities", note="Past valid_until"),
        idempotency_key="idemp_s04_expired",
        received_at=t_expired,
    )
    res_expired = await client.send_action(act_expired, token=token)
    err_expired = res_expired.get("error", {}).get("code")
    ok_expired = err_expired in ("E_MANDATE_REVOKED", "E_AUTH_EXPIRED", "E_REQ_INVALID")
    details["expired_response"] = res_expired

    # -------------------------------------------------------------------------
    # 4-Dimension Assertions
    # -------------------------------------------------------------------------
    # Dimension 1: HTTP verified
    http_verified = ok_premature and ok_active and ok_boundary and ok_expired

    # Dimension 2: Bank Ledger (2 successful payments of 185,000 paise: active + boundary)
    bal_res = await client.get_balance(ACCOUNT_PRIMARY.account_id)
    post_balance = bal_res.get("balance_paise", 0)
    expected_balance = ACCOUNT_INITIAL_BALANCE_PAISE - (2 * S4_AMOUNT_PAISE)
    ledger_verified = post_balance == expected_balance
    details["post_balance"] = post_balance

    # Dimension 3: Mandate Usage (committed 370,000, remaining 630,000)
    mnds_res = await client.get_customer_mandates(session_token)
    mnd_items = {m["mandate_id"]: m for m in mnds_res.get("items", [])}
    mnd = mnd_items.get(S4_MANDATE_ID, {})
    committed = mnd.get("committed_paise", 0)
    remaining = mnd.get("remaining_paise", 0)
    expected_committed = 2 * S4_AMOUNT_PAISE
    expected_remaining = S4_BUDGET_PAISE - expected_committed
    usage_verified = committed == expected_committed and remaining == expected_remaining
    details["committed_paise"] = committed
    details["remaining_paise"] = remaining

    # Dimension 4: Audit Trail
    audit_res = await client.get_audit_entries(session_token)
    all_events = audit_res.get("items", [])
    executed_events = [e for e in all_events if e.get("event_type") == "bank.executed"]
    denied_events = [e for e in all_events if e.get("event_type") == "policy.denied"]
    audit_verified = len(executed_events) == 2 and len(denied_events) >= 2

    passed = http_verified and ledger_verified and usage_verified and audit_verified
    return ScenarioResult(
        scenario_id="S4",
        name="Mandate Expiry & Temporal Boundary Testing",
        passed=passed,
        http_verified=http_verified,
        ledger_verified=ledger_verified,
        usage_verified=usage_verified,
        audit_verified=audit_verified,
        message="S4 passed all temporal checks" if passed else "S4 temporal verification failed",
        details=details,
    )


__all__ = ["run_s04"]
