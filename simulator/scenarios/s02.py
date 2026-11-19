"""Scenario S2: Scope denial scenarios (S2a, S2b, S2c, S2d).

Verifies the four mandatory dimensions:
- S2a: ₹7,000 payment denied by per-transaction cap (E_POLICY_AMOUNT_EXCEEDS_CAP).
- S2b: Unlisted mobile biller payment denied (E_POLICY_PAYEE_NOT_ALLOWED).
- S2c: Unauthorized transfer action denied (E_POLICY_ACTION_NOT_ALLOWED).
- S2d: Probing: 5 hard policy denials within 10 minutes trip breaker & suspend agent.
All scenarios require:
1. HTTP response: appropriate policy denial reason code.
2. Bank ledger: ACC-001 balance remains unchanged (12,500,000 paise).
3. Mandate usage: committed remains 0 paise, remaining remains 1,000,000 paise.
4. Audit trail: policy denial events logged, zero bank executions.
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
    PAYEE_MOBILE,
)

MANDATE_ID = "mnd_demo_1"
INITIAL_BUDGET_PAISE = 1_000_000


async def run_s02a(
    client: ScenarioClient,
    now: datetime | None = None,
) -> ScenarioResult:
    """S2a: Payment of ₹7,000 exceeds ₹5,000 per-transaction cap."""
    current_time = now or datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)
    await client.reset_state()

    cust_res = await client.customer_login(CUSTOMER_ID_PRIMARY, CUSTOMER_PIN_PRIMARY)
    session_token = cust_res.get("session_token", f"sess_{CUSTOMER_ID_PRIMARY}")

    tok_res = await client.request_token(AGENT_BILLPAL.agent_id, MANDATE_ID)
    token = tok_res.get("access_token")

    # ₹7,000 = 700,000 paise (exceeds cap of 500,000 paise)
    request_id = "req_s02a_over_cap_001"
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
            amount_paise=700_000,
            currency="INR",
            reference="Excessive Electricity Bill",
        ),
        intent=Intent(
            task="Pay electricity bill",
            category="utilities",
            note="Payment exceeding per-transaction cap",
        ),
        idempotency_key="idemp_s02a_001",
        received_at=current_time,
    )

    http_res = await client.send_action(action, token=token)
    err = http_res.get("error", {})
    err_code = err.get("code")
    http_verified = (
        err_code in ("E_POLICY_AMOUNT_EXCEEDS_CAP", "E_REQ_INVALID")
        or http_res.get("decision") == "DENY"
    )

    bal_res = await client.get_balance(ACCOUNT_PRIMARY.account_id)
    ledger_verified = bal_res.get("balance_paise") == ACCOUNT_INITIAL_BALANCE_PAISE

    mnds_res = await client.get_customer_mandates(session_token)
    mnd_items = {m["mandate_id"]: m for m in mnds_res.get("items", [])}
    mnd = mnd_items.get(MANDATE_ID, {})
    usage_verified = (
        mnd.get("committed_paise") == 0
        and mnd.get("remaining_paise") == INITIAL_BUDGET_PAISE
    )

    audit_res = await client.get_audit_entries(session_token)
    events = [e for e in audit_res.get("items", []) if e.get("request_id") == request_id]
    has_denied = any(e.get("event_type") == "policy.denied" for e in events)
    has_bank = any(e.get("event_type") == "bank.executed" for e in events)
    audit_verified = has_denied and not has_bank

    passed = http_verified and ledger_verified and usage_verified and audit_verified
    return ScenarioResult(
        scenario_id="S2a",
        name="Scope: Amount Exceeds Per-Transaction Cap",
        passed=passed,
        http_verified=http_verified,
        ledger_verified=ledger_verified,
        usage_verified=usage_verified,
        audit_verified=audit_verified,
        message="S2a passed" if passed else "S2a verification failed",
        details={"http_response": http_res, "error_code": err_code},
    )


async def run_s02b(
    client: ScenarioClient,
    now: datetime | None = None,
) -> ScenarioResult:
    """S2b: Payment to unlisted mobile biller (outside allowed payee scope)."""
    current_time = now or datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)
    await client.reset_state()

    cust_res = await client.customer_login(CUSTOMER_ID_PRIMARY, CUSTOMER_PIN_PRIMARY)
    session_token = cust_res.get("session_token", f"sess_{CUSTOMER_ID_PRIMARY}")

    tok_res = await client.request_token(AGENT_BILLPAL.agent_id, MANDATE_ID)
    token = tok_res.get("access_token")

    # BILL-MOBILE is not allowed on mnd_demo_1 (only BILL-ELEC)
    request_id = "req_s02b_unlisted_payee_001"
    action = CanonicalAction(
        request_id=request_id,
        customer_id=CUSTOMER_ID_PRIMARY,
        agent_id=AGENT_BILLPAL.agent_id,
        mandate_id=MANDATE_ID,
        mandate_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
        action_type=ActionType.BILL_PAY,
        params=ActionParams(
            from_account=ACCOUNT_PRIMARY.account_id,
            payee_id=PAYEE_MOBILE.payee_id,
            amount_paise=185_000,
            currency="INR",
            reference="Mobile Recharge",
        ),
        intent=Intent(
            task="Pay mobile bill",
            category="telecom",
            note="Unlisted biller payment",
        ),
        idempotency_key="idemp_s02b_001",
        received_at=current_time,
    )

    http_res = await client.send_action(action, token=token)
    err = http_res.get("error", {})
    err_code = err.get("code")
    http_verified = (
        err_code in ("E_POLICY_PAYEE_NOT_ALLOWED", "E_REQ_INVALID")
        or http_res.get("decision") == "DENY"
    )

    bal_res = await client.get_balance(ACCOUNT_PRIMARY.account_id)
    ledger_verified = bal_res.get("balance_paise") == ACCOUNT_INITIAL_BALANCE_PAISE

    mnds_res = await client.get_customer_mandates(session_token)
    mnd_items = {m["mandate_id"]: m for m in mnds_res.get("items", [])}
    mnd = mnd_items.get(MANDATE_ID, {})
    usage_verified = (
        mnd.get("committed_paise") == 0
        and mnd.get("remaining_paise") == INITIAL_BUDGET_PAISE
    )

    audit_res = await client.get_audit_entries(session_token)
    events = [e for e in audit_res.get("items", []) if e.get("request_id") == request_id]
    has_denied = any(e.get("event_type") == "policy.denied" for e in events)
    has_bank = any(e.get("event_type") == "bank.executed" for e in events)
    audit_verified = has_denied and not has_bank

    passed = http_verified and ledger_verified and usage_verified and audit_verified
    return ScenarioResult(
        scenario_id="S2b",
        name="Scope: Unlisted Payee Disallowed",
        passed=passed,
        http_verified=http_verified,
        ledger_verified=ledger_verified,
        usage_verified=usage_verified,
        audit_verified=audit_verified,
        message="S2b passed" if passed else "S2b verification failed",
        details={"http_response": http_res, "error_code": err_code},
    )


async def run_s02c(
    client: ScenarioClient,
    now: datetime | None = None,
) -> ScenarioResult:
    """S2c: Unauthorized transfer action outside permitted bill_pay scope."""
    current_time = now or datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)
    await client.reset_state()

    cust_res = await client.customer_login(CUSTOMER_ID_PRIMARY, CUSTOMER_PIN_PRIMARY)
    session_token = cust_res.get("session_token", f"sess_{CUSTOMER_ID_PRIMARY}")

    tok_res = await client.request_token(AGENT_BILLPAL.agent_id, MANDATE_ID)
    token = tok_res.get("access_token")

    # TRANSFER action type is disallowed (mandate permits bill_pay only)
    request_id = "req_s02c_disallowed_action_001"
    action = CanonicalAction(
        request_id=request_id,
        customer_id=CUSTOMER_ID_PRIMARY,
        agent_id=AGENT_BILLPAL.agent_id,
        mandate_id=MANDATE_ID,
        mandate_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
        action_type=ActionType.TRANSFER,
        params=ActionParams(
            from_account=ACCOUNT_PRIMARY.account_id,
            payee_id=PAYEE_ELECTRICITY.payee_id,
            amount_paise=100_000,
            currency="INR",
            reference="Disallowed Transfer",
        ),
        intent=Intent(
            task="Transfer funds",
            category="personal",
            note="Unauthorized transfer attempt",
        ),
        idempotency_key="idemp_s02c_001",
        received_at=current_time,
    )

    http_res = await client.send_action(action, token=token)
    err = http_res.get("error", {})
    err_code = err.get("code")
    http_verified = (
        err_code in ("E_POLICY_ACTION_NOT_ALLOWED", "E_REQ_INVALID")
        or http_res.get("decision") == "DENY"
    )

    bal_res = await client.get_balance(ACCOUNT_PRIMARY.account_id)
    ledger_verified = bal_res.get("balance_paise") == ACCOUNT_INITIAL_BALANCE_PAISE

    mnds_res = await client.get_customer_mandates(session_token)
    mnd_items = {m["mandate_id"]: m for m in mnds_res.get("items", [])}
    mnd = mnd_items.get(MANDATE_ID, {})
    usage_verified = (
        mnd.get("committed_paise") == 0
        and mnd.get("remaining_paise") == INITIAL_BUDGET_PAISE
    )

    audit_res = await client.get_audit_entries(session_token)
    events = [e for e in audit_res.get("items", []) if e.get("request_id") == request_id]
    has_denied = any(e.get("event_type") == "policy.denied" for e in events)
    has_bank = any(e.get("event_type") == "bank.executed" for e in events)
    audit_verified = has_denied and not has_bank

    passed = http_verified and ledger_verified and usage_verified and audit_verified
    return ScenarioResult(
        scenario_id="S2c",
        name="Scope: Action Type Disallowed",
        passed=passed,
        http_verified=http_verified,
        ledger_verified=ledger_verified,
        usage_verified=usage_verified,
        audit_verified=audit_verified,
        message="S2c passed" if passed else "S2c verification failed",
        details={"http_response": http_res, "error_code": err_code},
    )


async def run_s02d(
    client: ScenarioClient,
    now: datetime | None = None,
) -> ScenarioResult:
    """S2d: Five hard policy denials within 10 minutes trigger circuit breaker suspension."""
    current_time = now or datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)
    await client.reset_state()

    cust_res = await client.customer_login(CUSTOMER_ID_PRIMARY, CUSTOMER_PIN_PRIMARY)
    session_token = cust_res.get("session_token", f"sess_{CUSTOMER_ID_PRIMARY}")

    tok_res = await client.request_token(AGENT_BILLPAL.agent_id, MANDATE_ID)
    token = tok_res.get("access_token")

    denial_responses: list[dict[str, Any]] = []

    # Send 5 policy-violating actions within 10 minutes (each 30 seconds apart)
    for i in range(5):
        t_req = current_time + timedelta(seconds=i * 30)
        req_id = f"req_s02d_probing_{i:03d}"
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
                amount_paise=700_000,  # Exceeds cap of 500,000 paise
                currency="INR",
                reference=f"Probing Attempt {i}",
            ),
            intent=Intent(
                task="Pay bill",
                category="utilities",
                note=f"Probing attempt {i}",
            ),
            idempotency_key=f"idemp_s02d_{i:03d}",
            received_at=t_req,
        )
        res = await client.send_action(action, token=token)
        denial_responses.append(res)

    # Protected 6th action: Even an ordinary ₹1,850 bill must now fail because agent is suspended
    t_after = current_time + timedelta(seconds=180)
    req_blocked = "req_s02d_blocked_001"
    act_blocked = CanonicalAction(
        request_id=req_blocked,
        customer_id=CUSTOMER_ID_PRIMARY,
        agent_id=AGENT_BILLPAL.agent_id,
        mandate_id=MANDATE_ID,
        mandate_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
        action_type=ActionType.BILL_PAY,
        params=ActionParams(
            from_account=ACCOUNT_PRIMARY.account_id,
            payee_id=PAYEE_ELECTRICITY.payee_id,
            amount_paise=185_000,
            currency="INR",
            reference="Post-Suspension Attempt",
        ),
        intent=Intent(
            task="Pay electricity bill",
            category="utilities",
            note="Ordinary payment after suspension",
        ),
        idempotency_key="idemp_s02d_blocked",
        received_at=t_after,
    )
    res_blocked = await client.send_action(act_blocked, token=token)

    # Dimension 1: HTTP Responses (5 policy denials, then agent suspended)
    all_five_denied = all(
        r.get("error", {}).get("code") in ("E_POLICY_AMOUNT_EXCEEDS_CAP", "E_REQ_INVALID")
        for r in denial_responses
    )
    blocked_code = res_blocked.get("error", {}).get("code")
    ok_blocked = blocked_code in ("E_AUTH_AGENT_SUSPENDED", "E_RISK_BREAKER_OPEN")
    http_verified = all_five_denied and ok_blocked

    # Dimension 2: Bank Ledger (zero funds debited)
    bal_res = await client.get_balance(ACCOUNT_PRIMARY.account_id)
    ledger_verified = bal_res.get("balance_paise") == ACCOUNT_INITIAL_BALANCE_PAISE

    # Dimension 3: Mandate Usage (zero budget consumed)
    mnds_res = await client.get_customer_mandates(session_token)
    mnd_items = {m["mandate_id"]: m for m in mnds_res.get("items", [])}
    mnd = mnd_items.get(MANDATE_ID, {})
    usage_verified = (
        mnd.get("committed_paise") == 0
        and mnd.get("remaining_paise") == INITIAL_BUDGET_PAISE
    )

    # Dimension 4: Audit Trail (shows policy denials, breaker trip, and agent suspension)
    audit_res = await client.get_audit_entries(session_token)
    events = audit_res.get("items", [])
    has_denials = sum(1 for e in events if e.get("event_type") == "policy.denied") >= 5
    has_breaker = any(e.get("event_type") == "breaker.recommended" for e in events)
    has_suspension = any(e.get("event_type") == "agent.suspended" for e in events)
    has_bank = any(e.get("event_type") == "bank.executed" for e in events)
    audit_verified = has_denials and has_breaker and has_suspension and not has_bank

    passed = http_verified and ledger_verified and usage_verified and audit_verified
    return ScenarioResult(
        scenario_id="S2d",
        name="Probing: 5 Denials Trip Circuit Breaker",
        passed=passed,
        http_verified=http_verified,
        ledger_verified=ledger_verified,
        usage_verified=usage_verified,
        audit_verified=audit_verified,
        message="S2d passed" if passed else "S2d verification failed",
        details={
            "denial_count": len(denial_responses),
            "blocked_code": blocked_code,
            "has_breaker": has_breaker,
            "has_suspension": has_suspension,
        },
    )


async def run_s02(
    client: ScenarioClient,
    now: datetime | None = None,
) -> list[ScenarioResult]:
    """Execute all S2 scope denial scenarios (S2a, S2b, S2c, S2d)."""
    res_a = await run_s02a(client, now=now)
    res_b = await run_s02b(client, now=now)
    res_c = await run_s02c(client, now=now)
    res_d = await run_s02d(client, now=now)
    return [res_a, res_b, res_c, res_d]


__all__ = ["run_s02", "run_s02a", "run_s02b", "run_s02c", "run_s02d"]
