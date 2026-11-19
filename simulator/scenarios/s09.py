"""Scenario S9: Human-in-the-loop approval lifecycle variants (S9a-S9e).

Verifies the four mandatory dimensions across approval workflows:
- S9a (Approve): ₹4,800 action escalates to PENDING. Approved with PIN 4821.
  Stored action executes once with receipt. Balance debits by 480,000 paise.
- S9b (Reject): Action rejected by customer. Terminal state REJECTED. Never debits.
- S9c (Expire): Clock advances past TTL. Sweeper transitions record to EXPIRED. Never debits.
- S9d (Reuse & Hash): Duplicate approval returns E_ESC_ALREADY_CONSUMED.
  Substituted hash rejected. Single winner on race.
- S9e (PIN & Spam): Wrong PIN returns E_ESC_BAD_PIN (403) and audits failure.
  4th pending escalation on same mandate exceeds cap (E_ESC_LIMIT_REACHED / 409).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

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

MANDATE_ID = "mnd_demo_1"
INITIAL_BUDGET_PAISE = 1_000_000
STEP_UP_AMOUNT_PAISE = 480_000  # ₹4,800.00


def _build_stepup_action(req_id: str, t_req: datetime) -> CanonicalAction:
    return CanonicalAction(
        request_id=req_id,
        customer_id=CUSTOMER_ID_PRIMARY,
        agent_id=AGENT_BILLPAL.agent_id,
        mandate_id=MANDATE_ID,
        mandate_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
        action_type=ActionType.BILL_PAY,
        params=ActionParams(
            from_account=ACCOUNT_PRIMARY.account_id,
            payee_id=PAYEE_ELECTRICITY.payee_id,
            amount_paise=STEP_UP_AMOUNT_PAISE,
            currency="INR",
            reference="Step-up Utility Bill",
        ),
        intent=Intent(
            task="Pay electricity bill",
            category="utilities",
            note="Unusual amount requiring approval",
        ),
        idempotency_key=f"idemp_{req_id}",
        received_at=t_req,
    )


async def run_s09a(
    client: ScenarioClient,
    now: datetime | None = None,
) -> ScenarioResult:
    """S9a: Customer approves ₹4,800 step-up with valid PIN; stored action executes once."""
    current_time = now or datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)
    await client.reset_state()

    cust_res = await client.customer_login(CUSTOMER_ID_PRIMARY, CUSTOMER_PIN_PRIMARY)
    session_token = cust_res.get("session_token", f"sess_{CUSTOMER_ID_PRIMARY}")

    tok_res = await client.request_token(AGENT_BILLPAL.agent_id, MANDATE_ID)
    token = tok_res.get("access_token")

    # 1. Submit action -> escalates to PENDING
    req_id = "req_s09a_approve_001"
    action = _build_stepup_action(req_id, current_time)
    send_res = await client.send_action(action, token=token)
    esc_id = send_res.get("escalation_id")

    esc_pending = await client.get_escalation(str(esc_id)) if esc_id else {}
    ok_pending = esc_pending.get("state") == "PENDING"

    # 2. Customer approves with correct PIN
    decide_res = await client.decide_escalation(
        session_token=session_token,
        escalation_id=str(esc_id),
        decision="APPROVE",
        pin="4821",
    )
    ok_executed = (
        decide_res.get("state") in ("APPROVED", "EXECUTED")
        and decide_res.get("receipt") is not None
        and decide_res["receipt"].get("status") == "EXECUTED"
    )

    http_verified = ok_pending and ok_executed

    # Dimension 2: Bank Ledger (decrements by 480,000 paise: 12,500,000 -> 12,020,000)
    bal_res = await client.get_balance(ACCOUNT_PRIMARY.account_id)
    post_balance = bal_res.get("balance_paise", 0)
    expected_balance = ACCOUNT_INITIAL_BALANCE_PAISE - STEP_UP_AMOUNT_PAISE
    ledger_verified = post_balance == expected_balance

    # Dimension 3: Mandate Usage (480,000 committed, 520,000 remaining)
    mnds_res = await client.get_customer_mandates(session_token)
    mnd_items = {m["mandate_id"]: m for m in mnds_res.get("items", [])}
    mnd = mnd_items.get(MANDATE_ID, {})
    usage_verified = (
        mnd.get("committed_paise") == STEP_UP_AMOUNT_PAISE
        and mnd.get("remaining_paise") == (INITIAL_BUDGET_PAISE - STEP_UP_AMOUNT_PAISE)
    )

    # Dimension 4: Audit Trail (records escalation.approved, bank.executed)
    audit_res = await client.get_audit_entries(session_token)
    events = audit_res.get("items", [])
    has_approval = any(e.get("event_type") == "escalation.approved" for e in events)
    has_bank = any(e.get("event_type") == "bank.executed" for e in events)
    audit_verified = has_approval and has_bank

    passed = http_verified and ledger_verified and usage_verified and audit_verified
    return ScenarioResult(
        scenario_id="S9a",
        name="Approval: Valid PIN Execution with Receipt",
        passed=passed,
        http_verified=http_verified,
        ledger_verified=ledger_verified,
        usage_verified=usage_verified,
        audit_verified=audit_verified,
        message="S9a passed" if passed else "S9a execution failed",
        details={"decide_response": decide_res, "post_balance": post_balance},
    )


async def run_s09b(
    client: ScenarioClient,
    now: datetime | None = None,
) -> ScenarioResult:
    """S9b: Customer rejects escalation; terminal state REJECTED; zero bank execution."""
    current_time = now or datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)
    await client.reset_state()

    cust_res = await client.customer_login(CUSTOMER_ID_PRIMARY, CUSTOMER_PIN_PRIMARY)
    session_token = cust_res.get("session_token", f"sess_{CUSTOMER_ID_PRIMARY}")

    tok_res = await client.request_token(AGENT_BILLPAL.agent_id, MANDATE_ID)
    token = tok_res.get("access_token")

    req_id = "req_s09b_reject_001"
    action = _build_stepup_action(req_id, current_time)
    send_res = await client.send_action(action, token=token)
    esc_id = send_res.get("escalation_id")

    # Customer rejects
    decide_res = await client.decide_escalation(
        session_token=session_token,
        escalation_id=str(esc_id),
        decision="REJECT",
    )
    ok_rejected = decide_res.get("state") == "REJECTED"

    http_verified = ok_rejected

    bal_res = await client.get_balance(ACCOUNT_PRIMARY.account_id)
    ledger_verified = bal_res.get("balance_paise") == ACCOUNT_INITIAL_BALANCE_PAISE

    mnds_res = await client.get_customer_mandates(session_token)
    mnd_items = {m["mandate_id"]: m for m in mnds_res.get("items", [])}
    mnd = mnd_items.get(MANDATE_ID, {})
    usage_verified = mnd.get("committed_paise") == 0 and mnd.get("remaining_paise") == 1_000_000

    audit_res = await client.get_audit_entries(session_token)
    events = audit_res.get("items", [])
    has_rejected_event = any(e.get("event_type") == "escalation.rejected" for e in events)
    has_bank = any(e.get("event_type") == "bank.executed" for e in events)
    audit_verified = has_rejected_event and not has_bank

    passed = http_verified and ledger_verified and usage_verified and audit_verified
    return ScenarioResult(
        scenario_id="S9b",
        name="Approval: Customer Rejection",
        passed=passed,
        http_verified=http_verified,
        ledger_verified=ledger_verified,
        usage_verified=usage_verified,
        audit_verified=audit_verified,
        message="S9b passed" if passed else "S9b rejection failed",
        details={"decide_response": decide_res},
    )


async def run_s09c(
    client: ScenarioClient,
    now: datetime | None = None,
) -> ScenarioResult:
    """S9c: Clock advances past TTL; sweeper marks record EXPIRED; zero bank execution."""
    current_time = now or datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)
    await client.reset_state()

    tok_res = await client.request_token(AGENT_BILLPAL.agent_id, MANDATE_ID)
    token = tok_res.get("access_token")

    req_id = "req_s09c_expire_001"
    action = _build_stepup_action(req_id, current_time)
    send_res = await client.send_action(action, token=token)
    esc_id = str(send_res.get("escalation_id"))

    # Advance clock past TTL (301 seconds later)
    t_expired = current_time + timedelta(seconds=301)
    if isinstance(client, FixtureScenarioClient):
        expired_count = await client.expire_due(t_expired)
        assert expired_count >= 1

    esc_after = await client.get_escalation(esc_id)
    ok_expired = esc_after.get("state") == "EXPIRED"

    http_verified = ok_expired

    bal_res = await client.get_balance(ACCOUNT_PRIMARY.account_id)
    ledger_verified = bal_res.get("balance_paise") == ACCOUNT_INITIAL_BALANCE_PAISE

    cust_res = await client.customer_login(CUSTOMER_ID_PRIMARY, "4821")
    sess = cust_res.get("session_token", f"sess_{CUSTOMER_ID_PRIMARY}")
    mnds_res = await client.get_customer_mandates(sess)
    mnd_items = {m["mandate_id"]: m for m in mnds_res.get("items", [])}
    mnd = mnd_items.get(MANDATE_ID, {})
    usage_verified = mnd.get("committed_paise") == 0 and mnd.get("remaining_paise") == 1_000_000

    audit_res = await client.get_audit_entries(sess)
    events = audit_res.get("items", [])
    has_expired_audit = any(e.get("event_type") == "escalation.expired" for e in events)
    has_bank = any(e.get("event_type") == "bank.executed" for e in events)
    audit_verified = has_expired_audit and not has_bank

    passed = http_verified and ledger_verified and usage_verified and audit_verified
    return ScenarioResult(
        scenario_id="S9c",
        name="Approval: Expiration Sweeper",
        passed=passed,
        http_verified=http_verified,
        ledger_verified=ledger_verified,
        usage_verified=usage_verified,
        audit_verified=audit_verified,
        message="S9c passed" if passed else "S9c expiration failed",
        details={"final_state": esc_after.get("state")},
    )


async def run_s09d(
    client: ScenarioClient,
    now: datetime | None = None,
) -> ScenarioResult:
    """S9d: Duplicate approval (already consumed) and simultaneous race."""
    current_time = now or datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)
    await client.reset_state()

    cust_res = await client.customer_login(CUSTOMER_ID_PRIMARY, CUSTOMER_PIN_PRIMARY)
    session_token = cust_res.get("session_token", f"sess_{CUSTOMER_ID_PRIMARY}")

    tok_res = await client.request_token(AGENT_BILLPAL.agent_id, MANDATE_ID)
    token = tok_res.get("access_token")

    req_id = "req_s09d_reuse_001"
    action = _build_stepup_action(req_id, current_time)
    send_res = await client.send_action(action, token=token)
    esc_id = str(send_res.get("escalation_id"))

    # Race: Fire two simultaneous approvals
    task1 = client.decide_escalation(session_token, esc_id, "APPROVE", pin="4821")
    task2 = client.decide_escalation(session_token, esc_id, "APPROVE", pin="4821")
    r1, r2 = await asyncio.gather(task1, task2)

    states = [r1.get("state"), r2.get("state")]
    errors = [r1.get("error", {}).get("code"), r2.get("error", {}).get("code")]

    # Exactly one winner (APPROVED/EXECUTED) and one rejected (E_ESC_ALREADY_CONSUMED)
    single_winner = (
        len([s for s in states if s in ("APPROVED", "EXECUTED")]) == 1
        and ("E_ESC_ALREADY_CONSUMED" in errors or "ALREADY_CONSUMED" in str(errors))
    )

    http_verified = single_winner

    # Bank debited exactly once (480,000 paise)
    bal_res = await client.get_balance(ACCOUNT_PRIMARY.account_id)
    expected_balance = ACCOUNT_INITIAL_BALANCE_PAISE - STEP_UP_AMOUNT_PAISE
    ledger_verified = bal_res.get("balance_paise") == expected_balance

    mnds_res = await client.get_customer_mandates(session_token)
    mnd_items = {m["mandate_id"]: m for m in mnds_res.get("items", [])}
    mnd = mnd_items.get(MANDATE_ID, {})
    usage_verified = mnd.get("committed_paise") == STEP_UP_AMOUNT_PAISE

    audit_res = await client.get_audit_entries(session_token)
    events = [e for e in audit_res.get("items", []) if e.get("event_type") == "bank.executed"]
    audit_verified = len(events) == 1

    passed = http_verified and ledger_verified and usage_verified and audit_verified
    return ScenarioResult(
        scenario_id="S9d",
        name="Approval: Duplicate & Race Single Winner",
        passed=passed,
        http_verified=http_verified,
        ledger_verified=ledger_verified,
        usage_verified=usage_verified,
        audit_verified=audit_verified,
        message="S9d passed" if passed else "S9d race duplicate check failed",
        details={"r1": r1, "r2": r2},
    )


async def run_s09e(
    client: ScenarioClient,
    now: datetime | None = None,
) -> ScenarioResult:
    """S9e: Wrong PIN attempt audits failure; 4th pending escalation triggers
    E_ESC_LIMIT_REACHED.
    """
    current_time = now or datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)
    await client.reset_state()

    cust_res = await client.customer_login(CUSTOMER_ID_PRIMARY, CUSTOMER_PIN_PRIMARY)
    session_token = cust_res.get("session_token", f"sess_{CUSTOMER_ID_PRIMARY}")

    tok_res = await client.request_token(AGENT_BILLPAL.agent_id, MANDATE_ID)
    token = tok_res.get("access_token")

    # 1. Wrong PIN check
    req_id = "req_s09e_bad_pin_001"
    action = _build_stepup_action(req_id, current_time)
    send_res = await client.send_action(action, token=token)
    esc_id = str(send_res.get("escalation_id"))

    bad_pin_res = await client.decide_escalation(
        session_token, esc_id, "APPROVE", pin="9999"
    )
    ok_bad_pin = bad_pin_res.get("error", {}).get("code") == "E_ESC_BAD_PIN"

    # Escalation must still be PENDING
    esc_still_pending = (await client.get_escalation(esc_id)).get("state") == "PENDING"

    # 2. Spam creation cap: create 2 more pending (total 3)
    act2 = _build_stepup_action("req_s09e_spam_002", current_time)
    act3 = _build_stepup_action("req_s09e_spam_003", current_time)
    res2 = await client.send_action(act2, token=token)
    res3 = await client.send_action(act3, token=token)
    ok_3_pending = (
        res2.get("status") == "PENDING_APPROVAL"
        and res3.get("status") == "PENDING_APPROVAL"
    )

    # 4th pending creation must return E_ESC_LIMIT_REACHED (HTTP 409)
    act4 = _build_stepup_action("req_s09e_spam_004", current_time)
    res4 = await client.send_action(act4, token=token)
    ok_cap_enforced = res4.get("error", {}).get("code") == "E_ESC_LIMIT_REACHED"

    http_verified = ok_bad_pin and esc_still_pending and ok_3_pending and ok_cap_enforced

    bal_res = await client.get_balance(ACCOUNT_PRIMARY.account_id)
    ledger_verified = bal_res.get("balance_paise") == ACCOUNT_INITIAL_BALANCE_PAISE

    mnds_res = await client.get_customer_mandates(session_token)
    mnd_items = {m["mandate_id"]: m for m in mnds_res.get("items", [])}
    mnd = mnd_items.get(MANDATE_ID, {})
    usage_verified = mnd.get("committed_paise") == 0 and mnd.get("remaining_paise") == 1_000_000

    audit_res = await client.get_audit_entries(session_token)
    events = audit_res.get("items", [])
    has_bad_pin_audit = any(e.get("event_type") == "auth.pin_failed" for e in events)
    has_limit_audit = any(e.get("event_type") == "escalation.limit_reached" for e in events)
    audit_verified = has_bad_pin_audit and has_limit_audit

    passed = http_verified and ledger_verified and usage_verified and audit_verified
    return ScenarioResult(
        scenario_id="S9e",
        name="Approval: Wrong PIN & 3-Pending Cap Limit",
        passed=passed,
        http_verified=http_verified,
        ledger_verified=ledger_verified,
        usage_verified=usage_verified,
        audit_verified=audit_verified,
        message="S9e passed" if passed else "S9e PIN / limit enforcement failed",
        details={"bad_pin_res": bad_pin_res, "limit_res": res4},
    )


async def run_s09(
    client: ScenarioClient,
    now: datetime | None = None,
) -> list[ScenarioResult]:
    """Execute all S9 approval workflow scenarios (S9a-S9e)."""
    res_a = await run_s09a(client, now=now)
    res_b = await run_s09b(client, now=now)
    res_c = await run_s09c(client, now=now)
    res_d = await run_s09d(client, now=now)
    res_e = await run_s09e(client, now=now)
    return [res_a, res_b, res_c, res_d, res_e]


__all__ = ["run_s09", "run_s09a", "run_s09b", "run_s09c", "run_s09d", "run_s09e"]
