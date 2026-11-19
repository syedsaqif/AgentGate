"""Scenario S11: Cryptographic dispute evidence bundle generation.

Shared deliverable across Members B, C, and D:
- Member C drives the scenario.
- Member B builds and signs the cryptographic bundle.
- Member D renders the evidence views.
Verifies the four mandatory dimensions:
1. Complete JSON evidence bundle returned containing:
   - Historical mandate terms and canonical hash.
   - Public agent identity (JWK, jkt, status).
   - Signed DPoP proof and token evidence.
   - Risk scoring diagnostics (score, level, signals).
   - Approval metadata (if escalated).
   - Bank execution receipt and timestamp.
   - Explicit verification scope and bundle signature.
2. Bank ledger verified.
3. Mandate usage verified.
4. Audit trail verified.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from contracts.common import ActionType
from contracts.core_models import ActionParams, CanonicalAction, Intent
from simulator.client_port import ScenarioClient, ScenarioResult
from simulator.personas import (
    ACCOUNT_PRIMARY,
    AGENT_BILLPAL,
    CUSTOMER_ID_PRIMARY,
    CUSTOMER_PIN_PRIMARY,
    PAYEE_ELECTRICITY,
)

MANDATE_ID = "mnd_demo_1"


async def run_s11(
    client: ScenarioClient,
    now: datetime | None = None,
) -> ScenarioResult:
    """Execute scenario S11 verifying the end-to-end cryptographic evidence bundle."""
    current_time = now or datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)
    details: dict[str, Any] = {}

    await client.reset_state()

    cust_res = await client.customer_login(CUSTOMER_ID_PRIMARY, CUSTOMER_PIN_PRIMARY)
    session_token = cust_res.get("session_token", f"sess_{CUSTOMER_ID_PRIMARY}")

    tok_res = await client.request_token(AGENT_BILLPAL.agent_id, MANDATE_ID)
    token = tok_res.get("access_token")

    # 1. Dispatch action to generate full execution and audit trail
    req_id = "req_s11_evidence_001"
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
            amount_paise=185_000,
            currency="INR",
            reference="Dispute Evidence Baseline Payment",
        ),
        intent=Intent(task="Pay bill", category="utilities", note="Generates full evidence trail"),
        idempotency_key="idemp_s11_001",
        received_at=current_time,
    )
    await client.send_action(action, token=token)

    # 2. Retrieve cryptographic evidence bundle
    bundle = await client.get_evidence(session_token, req_id)
    details["evidence_bundle"] = bundle

    # Validate bundle schema and contents
    has_req = bundle.get("request_id") == req_id
    has_mandate = "mandate" in bundle and "mandate_hash" in bundle.get("mandate", {})
    has_agent = "agent_identity" in bundle and "agent_id" in bundle.get("agent_identity", {})
    has_proof = "proof_evidence" in bundle
    has_risk = "risk_assessment" in bundle and "level" in bundle.get("risk_assessment", {})
    has_bank = "bank_result" in bundle and bundle.get("bank_result", {}).get("status") == "EXECUTED"
    has_scope = "verification_scope" in bundle
    has_sig = "bundle_sig" in bundle

    http_verified = (
        has_req
        and has_mandate
        and has_agent
        and has_proof
        and has_risk
        and has_bank
        and has_scope
        and has_sig
    )

    # Dimension 2: Bank Ledger (12,315,000 paise)
    bal_res = await client.get_balance(ACCOUNT_PRIMARY.account_id)
    ledger_verified = bal_res.get("balance_paise") == 12_315_000

    # Dimension 3: Mandate Usage (815,000 remaining)
    mnds_res = await client.get_customer_mandates(session_token)
    mnd_items = {m["mandate_id"]: m for m in mnds_res.get("items", [])}
    mnd = mnd_items.get(MANDATE_ID, {})
    usage_verified = mnd.get("committed_paise") == 185_000 and mnd.get("remaining_paise") == 815_000

    # Dimension 4: Audit Trail
    audit_res = await client.get_audit_entries(session_token)
    audit_verified = len(audit_res.get("items", [])) >= 4

    passed = http_verified and ledger_verified and usage_verified and audit_verified
    return ScenarioResult(
        scenario_id="S11",
        name="Cryptographic Dispute Evidence Bundle",
        passed=passed,
        http_verified=http_verified,
        ledger_verified=ledger_verified,
        usage_verified=usage_verified,
        audit_verified=audit_verified,
        message="S11 passed" if passed else "S11 evidence bundle verification failed",
        details=details,
    )


__all__ = ["run_s11"]
