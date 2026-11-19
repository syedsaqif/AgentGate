"""Scenario S1: Happy path utility bill payment.

Verifies the four mandatory dimensions:
1. HTTP responses: decision ALLOW, status EXECUTED, valid audit_ref returned.
2. Bank ledger: ACC-001 decrements from 12,500,000 to 12,315,000 paise (-185,000 paise).
3. Mandate usage: mnd_demo_1 lifetime budget 1,000,000 decrements to 815,000 remaining,
   committed becomes 185,000 paise.
4. Audit trail: records identity.verified, policy.evaluated, risk.scored, and bank.executed.
"""

from __future__ import annotations

from datetime import UTC, datetime
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

PAYMENT_AMOUNT_PAISE = 185_000  # ₹1,850.00
MANDATE_ID = "mnd_demo_1"
MANDATE_INITIAL_BUDGET_PAISE = 1_000_000  # ₹10,000.00


async def run_s01(
    client: ScenarioClient,
    now: datetime | None = None,
) -> ScenarioResult:
    """Execute scenario S1 and return structured verification result."""
    current_time = now or datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)
    details: dict[str, Any] = {}

    # 1. Reset state for clean deterministic seed
    await client.reset_state()

    # 2. Customer login & verify seed units
    cust_res = await client.customer_login(CUSTOMER_ID_PRIMARY, CUSTOMER_PIN_PRIMARY)
    session_token = cust_res.get("session_token", f"sess_{CUSTOMER_ID_PRIMARY}")

    bal_pre = await client.get_balance(ACCOUNT_PRIMARY.account_id)
    initial_balance = bal_pre.get("balance_paise", 0)
    details["initial_balance_paise"] = initial_balance
    assert initial_balance == ACCOUNT_INITIAL_BALANCE_PAISE, (
        f"Seed unit mismatch: expected {ACCOUNT_INITIAL_BALANCE_PAISE}, got {initial_balance}"
    )

    mnds_pre = await client.get_customer_mandates(session_token)
    mnd_items = {m["mandate_id"]: m for m in mnds_pre.get("items", [])}
    assert MANDATE_ID in mnd_items, f"Mandate {MANDATE_ID} not found in seeded customer mandates"
    initial_budget = mnd_items[MANDATE_ID].get("remaining_paise", 0)
    details["initial_remaining_budget_paise"] = initial_budget
    assert initial_budget == MANDATE_INITIAL_BUDGET_PAISE, (
        f"Seed unit mismatch: expected {MANDATE_INITIAL_BUDGET_PAISE}, got {initial_budget}"
    )

    # 3. Agent requests delegation token
    tok_res = await client.request_token(AGENT_BILLPAL.agent_id, MANDATE_ID)
    token = tok_res.get("access_token")

    # 4. Construct canonical action for ₹1,850 electricity bill
    request_id = "req_s01_electricity_001"
    action = CanonicalAction(
        request_id=request_id,
        customer_id=CUSTOMER_ID_PRIMARY,
        agent_id=AGENT_BILLPAL.agent_id,
        mandate_id=MANDATE_ID,
        mandate_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
        action_type=ActionType.BILL_PAY,
        params=ActionParams(
            from_account=ACCOUNT_PRIMARY.account_id,
            payee_id=PAYEE_ELECTRICITY.payee_id,
            amount_paise=PAYMENT_AMOUNT_PAISE,
            currency="INR",
            reference="State Electricity Board Nov 2026",
        ),
        intent=Intent(
            task="Pay electricity bill",
            category="utilities",
            note="Routine monthly electricity payment",
        ),
        idempotency_key="idemp_s01_001",
        received_at=current_time,
    )

    # 5. Send action through gateway
    http_res = await client.send_action(action, token=token)
    details["http_response"] = http_res

    # Dimension 1: HTTP response assertions
    decision = http_res.get("decision")
    status = http_res.get("status")
    audit_ref = http_res.get("audit_ref")
    http_verified = (
        decision == "ALLOW"
        and status == "EXECUTED"
        and audit_ref is not None
        and "entry_hash" in audit_ref
    )

    # Dimension 2: Bank ledger assertions (12,500,000 -> 12,315,000)
    bal_post = await client.get_balance(ACCOUNT_PRIMARY.account_id)
    balance_post = bal_post.get("balance_paise", 0)
    details["post_balance_paise"] = balance_post
    expected_balance = ACCOUNT_INITIAL_BALANCE_PAISE - PAYMENT_AMOUNT_PAISE  # 12,315,000
    ledger_verified = balance_post == expected_balance

    # Dimension 3: Mandate usage assertions (1,000,000 -> 815,000)
    mnds_post = await client.get_customer_mandates(session_token)
    mnd_post_items = {m["mandate_id"]: m for m in mnds_post.get("items", [])}
    mnd_after = mnd_post_items.get(MANDATE_ID, {})
    remaining_paise = mnd_after.get("remaining_paise", 0)
    committed_paise = mnd_after.get("committed_paise", 0)
    details["post_remaining_paise"] = remaining_paise
    details["post_committed_paise"] = committed_paise
    expected_remaining = MANDATE_INITIAL_BUDGET_PAISE - PAYMENT_AMOUNT_PAISE  # 815,000
    usage_verified = (
        remaining_paise == expected_remaining
        and committed_paise == PAYMENT_AMOUNT_PAISE
    )

    # Dimension 4: Audit trail assertions
    audit_data = await client.get_audit_entries(session_token)
    audit_events = audit_data.get("items", [])
    details["audit_events_count"] = len(audit_events)
    event_types = {e.get("event_type") for e in audit_events if e.get("request_id") == request_id}
    has_identity = "identity.verified" in event_types
    has_policy = "policy.evaluated" in event_types
    has_risk = "risk.scored" in event_types
    has_bank = "bank.executed" in event_types
    audit_verified = has_identity and has_policy and has_risk and has_bank

    passed = http_verified and ledger_verified and usage_verified and audit_verified
    msg = (
        "S1 passed all 4 dimensions"
        if passed
        else (
            f"S1 verification failed: http={http_verified}, ledger={ledger_verified}, "
            f"usage={usage_verified}, audit={audit_verified}"
        )
    )

    return ScenarioResult(
        scenario_id="S1",
        name="Happy Path Electricity Bill Payment",
        passed=passed,
        http_verified=http_verified,
        ledger_verified=ledger_verified,
        usage_verified=usage_verified,
        audit_verified=audit_verified,
        message=msg,
        details=details,
    )


__all__ = ["run_s01"]
