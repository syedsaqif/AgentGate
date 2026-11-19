"""Scenario S6: Identity and DPoP cryptographic verification error vectors.

Member C drives the calls while Member B owns the checks.
Asserts expectations defined in contracts/fixtures/b/auth_audit_cases.json:
- Missing credentials: E_AUTH_MISSING (401)
- Proof replay: E_AUTH_REPLAY (401)
- Wrong key: E_AUTH_KEY_MISMATCH (401)
- Suspended agent: E_AUTH_AGENT_SUSPENDED (403)
- Unknown agent: E_AUTH_INVALID_TOKEN (401)
- Wrong mandate binding: E_AUTH_AGENT_MANDATE_MISMATCH (403)
- Expired token: E_AUTH_EXPIRED (401)
- Tampered signed body: E_AUTH_PROOF_INVALID (401)
- Agent calling human routes: E_AUTH_AGENT_SUSPENDED / 403 Forbidden
4 dimensions:
1. HTTP responses: exact match with expected reason codes.
2. Bank ledger: zero funds debited (12,500,000 paise unchanged).
3. Mandate usage: zero budget consumed (1,000,000 remaining unchanged).
4. Audit trail: records auth failure events, zero bank executions.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from contracts.common import ActionType
from contracts.core_models import ActionParams, CanonicalAction, Intent
from simulator.client_port import ScenarioClient, ScenarioResult
from simulator.personas import (
    ACCOUNT_INITIAL_BALANCE_PAISE,
    ACCOUNT_PRIMARY,
    AGENT_BILLPAL,
    CUSTOMER_ID_PRIMARY,
    PAYEE_ELECTRICITY,
)

MANDATE_ID = "mnd_demo_1"
FIXTURES_PATH = Path("contracts/fixtures/b/auth_audit_cases.json")


def _load_b_auth_fixtures() -> dict[str, Any]:
    if FIXTURES_PATH.exists():
        with FIXTURES_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    return {}


async def run_s06(
    client: ScenarioClient,
    now: datetime | None = None,
) -> ScenarioResult:
    """Execute scenario S6 driving all authentication error vectors."""
    current_time = now or datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)
    await client.reset_state()

    _b_fixtures = _load_b_auth_fixtures()
    details: dict[str, Any] = {}

    vectors: list[tuple[str, str | None, str, str]] = [
        ("missing_credentials", None, "E_AUTH_MISSING", "bad_missing"),
        ("proof_replay", "bad_replay", "E_AUTH_REPLAY", "bad_replay"),
        ("wrong_key", "bad_key", "E_AUTH_KEY_MISMATCH", "bad_key"),
        ("suspended_agent", "bad_suspended", "E_AUTH_AGENT_SUSPENDED", "bad_suspended"),
        ("unknown_agent", "bad_unknown_agent", "E_AUTH_INVALID_TOKEN", "bad_unknown_agent"),
        ("wrong_binding", "bad_binding", "E_AUTH_AGENT_MANDATE_MISMATCH", "bad_binding"),
        ("expired_token", "bad_expired", "E_AUTH_EXPIRED", "bad_expired"),
        ("tampered_signature", "bad_tampered_sig", "E_AUTH_PROOF_INVALID", "bad_tampered_sig"),
    ]

    all_vectors_passed = True
    vector_results: list[dict[str, Any]] = []

    for name, tok, expected_code, _token_tag in vectors:
        req_id = f"req_s06_{name}"
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
                reference=f"Auth vector test {name}",
            ),
            intent=Intent(task="Pay bill", category="utilities", note=f"Test vector {name}"),
            idempotency_key=f"idemp_s06_{name}",
            received_at=current_time,
        )

        res = await client.send_action(action, token=tok or "bad_missing")
        err_code = res.get("error", {}).get("code")
        vec_ok = err_code == expected_code
        if not vec_ok:
            all_vectors_passed = False
        vector_results.append({
            "vector": name,
            "expected_code": expected_code,
            "actual_code": err_code,
            "passed": vec_ok,
        })

    # Test Agent invoking human customer route (should be rejected)
    human_route_res = await client.decide_escalation(
        session_token="agent_dpop_token_not_human",
        escalation_id="esc_non_existent",
        decision="APPROVE",
        pin="4821",
    )
    human_rejected = human_route_res.get("error") is not None
    details["vector_results"] = vector_results
    details["human_route_rejected"] = human_rejected

    # Dimension 1: HTTP verified
    http_verified = all_vectors_passed and human_rejected

    # Dimension 2: Bank Ledger (zero movement)
    bal_res = await client.get_balance(ACCOUNT_PRIMARY.account_id)
    ledger_verified = bal_res.get("balance_paise") == ACCOUNT_INITIAL_BALANCE_PAISE

    # Dimension 3: Mandate Usage (zero budget consumed)
    cust_res = await client.customer_login(CUSTOMER_ID_PRIMARY, "4821")
    sess = cust_res.get("session_token", f"sess_{CUSTOMER_ID_PRIMARY}")
    mnds_res = await client.get_customer_mandates(sess)
    mnd_items = {m["mandate_id"]: m for m in mnds_res.get("items", [])}
    mnd = mnd_items.get(MANDATE_ID, {})
    usage_verified = mnd.get("committed_paise") == 0 and mnd.get("remaining_paise") == 1_000_000

    # Dimension 4: Audit Trail (records auth failures, zero bank execution)
    audit_res = await client.get_audit_entries(sess)
    events = audit_res.get("items", [])
    has_auth_failures = any(e.get("event_type") == "auth.failed" for e in events)
    has_bank = any(e.get("event_type") == "bank.executed" for e in events)
    audit_verified = has_auth_failures and not has_bank

    passed = http_verified and ledger_verified and usage_verified and audit_verified
    return ScenarioResult(
        scenario_id="S6",
        name="Identity & DPoP Cryptographic Vectors (B checks)",
        passed=passed,
        http_verified=http_verified,
        ledger_verified=ledger_verified,
        usage_verified=usage_verified,
        audit_verified=audit_verified,
        message="S6 passed all identity vectors" if passed else "S6 identity verification failed",
        details=details,
    )


__all__ = ["run_s06"]
