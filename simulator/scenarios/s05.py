"""Scenario S5: Mandate revocation and pending escalation cancellation.

Verifies the four mandatory dimensions:
1. Revocation acknowledgement comes only after status (REVOKED), tokens, and cancellations
   are committed.
2. Any pending approval for the revoked mandate is immediately marked CANCELLED.
3. The next action submitted under this mandate fails immediately with E_MANDATE_REVOKED.
4. No money is debited and no bank calls are dispatched.
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

MANDATE_ID = "mnd_demo_1"
INITIAL_BUDGET_PAISE = 1_000_000


async def run_s05(
    client: ScenarioClient,
    now: datetime | None = None,
) -> ScenarioResult:
    """Execute scenario S5 and return structured verification result."""
    current_time = now or datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)
    details: dict[str, Any] = {}

    # 1. Clean reset
    await client.reset_state()

    cust_res = await client.customer_login(CUSTOMER_ID_PRIMARY, CUSTOMER_PIN_PRIMARY)
    session_token = cust_res.get("session_token", f"sess_{CUSTOMER_ID_PRIMARY}")

    tok_res = await client.request_token(AGENT_BILLPAL.agent_id, MANDATE_ID)
    token = tok_res.get("access_token")

    # 2. Submit an approvable ₹4,800 action to create a PENDING escalation
    req_pending = "req_s05_pending_stepup"
    act_pending = CanonicalAction(
        request_id=req_pending,
        customer_id=CUSTOMER_ID_PRIMARY,
        agent_id=AGENT_BILLPAL.agent_id,
        mandate_id=MANDATE_ID,
        mandate_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
        action_type=ActionType.BILL_PAY,
        params=ActionParams(
            from_account=ACCOUNT_PRIMARY.account_id,
            payee_id=PAYEE_ELECTRICITY.payee_id,
            amount_paise=480_000,
            currency="INR",
            reference="Pending Step-up Bill",
        ),
        intent=Intent(task="Pay bill", category="utilities", note="Will be revoked"),
        idempotency_key="idemp_s05_pending",
        received_at=current_time,
    )
    res_pending = await client.send_action(act_pending, token=token)
    esc_id = res_pending.get("escalation_id")
    details["created_escalation_id"] = esc_id

    # Verify escalation is PENDING
    esc_before = await client.get_escalation(str(esc_id)) if esc_id else {}
    assert esc_before.get("state") == "PENDING", f"Expected PENDING escalation, got {esc_before}"

    # 3. Customer revokes the mandate
    revoke_res = await client.revoke_mandate(session_token, MANDATE_ID)
    ok_revoke_ack = revoke_res.get("status") == "REVOKED"
    details["revoke_response"] = revoke_res

    # 4. Verify pending escalation was CANCELLED upon revocation
    esc_after = await client.get_escalation(str(esc_id)) if esc_id else {}
    ok_cancelled = esc_after.get("state") == "CANCELLED"
    details["escalation_state_after_revoke"] = esc_after.get("state")

    # 5. Subsequent action submitted under revoked mandate must fail immediately
    req_next = "req_s05_post_revoke_attempt"
    act_next = CanonicalAction(
        request_id=req_next,
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
            reference="Post-Revoke Action",
        ),
        intent=Intent(task="Pay bill", category="utilities", note="Should be rejected"),
        idempotency_key="idemp_s05_next",
        received_at=current_time,
    )
    res_next = await client.send_action(act_next, token=token)
    err_code = res_next.get("error", {}).get("code")
    ok_next_denied = err_code == "E_MANDATE_REVOKED"
    details["next_action_response"] = res_next

    # 6. Verify cancelled approval cannot be approved or dispatched
    decide_res = await client.decide_escalation(
        session_token=session_token,
        escalation_id=str(esc_id),
        decision="APPROVE",
        pin="4821",
    )
    ok_no_consume = decide_res.get("error", {}).get("code") == "E_ESC_ALREADY_CONSUMED"

    # Dimension 1: HTTP Responses verified
    http_verified = ok_revoke_ack and ok_cancelled and ok_next_denied and ok_no_consume

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
        and mnd.get("status") == "REVOKED"
    )

    # Dimension 4: Audit Trail (records mandate.revoked, zero bank executions)
    audit_res = await client.get_audit_entries(session_token)
    events = audit_res.get("items", [])
    has_revoke_audit = any(e.get("event_type") == "mandate.revoked" for e in events)
    has_bank = any(e.get("event_type") == "bank.executed" for e in events)
    audit_verified = has_revoke_audit and not has_bank

    passed = http_verified and ledger_verified and usage_verified and audit_verified
    return ScenarioResult(
        scenario_id="S5",
        name="Mandate Revocation and Pending Cancellation",
        passed=passed,
        http_verified=http_verified,
        ledger_verified=ledger_verified,
        usage_verified=usage_verified,
        audit_verified=audit_verified,
        message="S5 passed" if passed else "S5 verification failed",
        details=details,
    )


__all__ = ["run_s05"]
