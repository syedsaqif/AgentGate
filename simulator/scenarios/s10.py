"""Scenario S10: Merkle / cryptographic audit trail integrity and tamper detection.

Member B owns the tamper tool and validation rules; Member C drives the scenario.
Verifies the four mandatory dimensions:
1. Untampered audit trail validates successfully via client.verify_audit.
2. When an audit block is tampered, modified, or omitted, client.verify_audit
   detects the break and returns valid == False with signature/hash mismatch error.
3. Bank ledger and mandate usage remain consistent.
4. Audit events exist and reflect genuine historical activity.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from contracts.common import ActionType
from contracts.core_models import ActionParams, CanonicalAction, Intent
from simulator.client_port import FixtureScenarioClient, ScenarioClient, ScenarioResult
from simulator.personas import (
    ACCOUNT_PRIMARY,
    AGENT_BILLPAL,
    CUSTOMER_ID_PRIMARY,
    CUSTOMER_PIN_PRIMARY,
    PAYEE_ELECTRICITY,
)

MANDATE_ID = "mnd_demo_1"


async def run_s10(
    client: ScenarioClient,
    now: datetime | None = None,
) -> ScenarioResult:
    """Execute scenario S10 driving audit chain integrity and tamper detection."""
    current_time = now or datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)
    details: dict[str, Any] = {}

    await client.reset_state()

    cust_res = await client.customer_login(CUSTOMER_ID_PRIMARY, CUSTOMER_PIN_PRIMARY)
    session_token = cust_res.get("session_token", f"sess_{CUSTOMER_ID_PRIMARY}")

    tok_res = await client.request_token(AGENT_BILLPAL.agent_id, MANDATE_ID)
    token = tok_res.get("access_token")

    # 1. Generate normal audit activity (valid payment)
    action = CanonicalAction(
        request_id="req_s10_normal_001",
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
            reference="Audit Integrity Payment",
        ),
        intent=Intent(task="Pay bill", category="utilities", note="Generates audit entries"),
        idempotency_key="idemp_s10_001",
        received_at=current_time,
    )
    await client.send_action(action, token=token)

    # 2. Verify untampered audit chain
    verify_clean = await client.verify_audit(session_token)
    ok_clean = verify_clean.get("valid") is True
    details["verify_clean"] = verify_clean

    # 3. Inject tamper into audit store and verify detection
    if isinstance(client, FixtureScenarioClient):
        client.tamper_audit = True

    verify_tampered = await client.verify_audit(session_token)
    ok_tamper_detected = verify_tampered.get("valid") is False
    details["verify_tampered"] = verify_tampered

    # Dimension 1: HTTP Responses verified
    http_verified = ok_clean and ok_tamper_detected

    # Dimension 2: Bank Ledger (1 payment debited: 12,500,000 -> 12,315,000 paise)
    bal_res = await client.get_balance(ACCOUNT_PRIMARY.account_id)
    ledger_verified = bal_res.get("balance_paise") == 12_315_000

    # Dimension 3: Mandate Usage (185,000 committed, 815,000 remaining)
    mnds_res = await client.get_customer_mandates(session_token)
    mnd_items = {m["mandate_id"]: m for m in mnds_res.get("items", [])}
    mnd = mnd_items.get(MANDATE_ID, {})
    usage_verified = mnd.get("committed_paise") == 185_000 and mnd.get("remaining_paise") == 815_000

    # Dimension 4: Audit Trail
    audit_res = await client.get_audit_entries(session_token)
    audit_verified = len(audit_res.get("items", [])) >= 4

    passed = http_verified and ledger_verified and usage_verified and audit_verified
    return ScenarioResult(
        scenario_id="S10",
        name="Audit Log Merkle Hash Tamper Verification",
        passed=passed,
        http_verified=http_verified,
        ledger_verified=ledger_verified,
        usage_verified=usage_verified,
        audit_verified=audit_verified,
        message="S10 passed" if passed else "S10 audit verification failed",
        details=details,
    )


__all__ = ["run_s10"]
