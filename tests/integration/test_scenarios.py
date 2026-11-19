"""Integration tests driving scenarios S1-S11 and verifying 4-dimension assertions."""

from __future__ import annotations

import pytest

from simulator.client_port import FixtureScenarioClient
from simulator.scenarios.s01 import run_s01
from simulator.scenarios.s02 import run_s02, run_s02a, run_s02b, run_s02c, run_s02d
from simulator.scenarios.s03 import run_s03
from simulator.scenarios.s04 import run_s04
from simulator.scenarios.s05 import run_s05
from simulator.scenarios.s06 import run_s06
from simulator.scenarios.s07 import run_s07
from simulator.scenarios.s08 import run_s08
from simulator.scenarios.s09 import (
    run_s09,
    run_s09a,
    run_s09b,
    run_s09c,
    run_s09d,
    run_s09e,
)
from simulator.scenarios.s10 import run_s10
from simulator.scenarios.s11 import run_s11

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_scenario_s01_happy_path() -> None:
    """Scenario S1: Happy path electricity payment executes once and updates ledger/usage/audit."""
    client = FixtureScenarioClient()
    result = await run_s01(client)

    assert result.passed, result.message
    assert result.http_verified, "HTTP verification failed for S1"
    assert result.ledger_verified, "Bank ledger verification failed for S1"
    assert result.usage_verified, "Mandate usage verification failed for S1"
    assert result.audit_verified, "Audit trail verification failed for S1"

    # Specific unit assertions from the plan
    assert result.details["initial_balance_paise"] == 12_500_000
    assert result.details["post_balance_paise"] == 12_315_000
    assert result.details["post_remaining_paise"] == 815_000
    assert result.details["post_committed_paise"] == 185_000


@pytest.mark.asyncio
async def test_scenario_s02_scope_denials() -> None:
    """Scenario S2: S2a (cap), S2b (payee), S2c (action) scope denials leave ledger/usage intact."""
    client = FixtureScenarioClient()

    # S2a: Over per-transaction cap (₹7,000 > ₹5,000)
    res_a = await run_s02a(client)
    assert res_a.passed, res_a.message
    assert res_a.http_verified
    assert res_a.ledger_verified
    assert res_a.usage_verified
    assert res_a.audit_verified
    assert res_a.details["error_code"] == "E_POLICY_AMOUNT_EXCEEDS_CAP"

    # S2b: Unlisted mobile biller (BILL-MOBILE)
    res_b = await run_s02b(client)
    assert res_b.passed, res_b.message
    assert res_b.http_verified
    assert res_b.ledger_verified
    assert res_b.usage_verified
    assert res_b.audit_verified
    assert res_b.details["error_code"] == "E_POLICY_PAYEE_NOT_ALLOWED"

    # S2c: Unauthorized action type (TRANSFER)
    res_c = await run_s02c(client)
    assert res_c.passed, res_c.message
    assert res_c.http_verified
    assert res_c.ledger_verified
    assert res_c.usage_verified
    assert res_c.audit_verified
    assert res_c.details["error_code"] == "E_POLICY_ACTION_NOT_ALLOWED"

    # S2d: Probing 5 denials within 10 minutes trip breaker & suspend agent
    res_d = await run_s02d(client)
    assert res_d.passed, res_d.message
    assert res_d.http_verified
    assert res_d.ledger_verified
    assert res_d.usage_verified
    assert res_d.audit_verified
    assert res_d.details["has_breaker"] is True
    assert res_d.details["has_suspension"] is True

    # Batch driver verification
    all_s2 = await run_s02(client)
    assert len(all_s2) == 4
    assert all(r.passed for r in all_s2)


@pytest.mark.asyncio
async def test_scenario_s03_concurrency_race() -> None:
    """Scenario S3: 5 parallel requests on 400,000 paise budget; exactly 2 win across 100 runs."""
    client = FixtureScenarioClient()
    result = await run_s03(client, runs=100)

    assert result.passed, result.message
    assert result.http_verified
    assert result.ledger_verified
    assert result.usage_verified
    assert result.audit_verified
    assert result.details["runs_executed"] == 100
    assert len(result.details["failed_runs"]) == 0


@pytest.mark.asyncio
async def test_scenario_s04_expiry_and_boundaries() -> None:
    """Scenario S4: Premature (-10s), active (+30s), boundary (+90s), and expired (+91s) actions."""
    client = FixtureScenarioClient()
    result = await run_s04(client)

    assert result.passed, result.message
    assert result.http_verified
    assert result.ledger_verified
    assert result.usage_verified
    assert result.audit_verified
    assert result.details["post_balance"] == 12_500_000 - (2 * 185_000)
    assert result.details["committed_paise"] == 2 * 185_000
    assert result.details["remaining_paise"] == 1_000_000 - (2 * 185_000)


@pytest.mark.asyncio
async def test_scenario_s05_revocation() -> None:
    """Scenario S5: Revocation cancels pending approvals and blocks subsequent actions."""
    client = FixtureScenarioClient()
    result = await run_s05(client)

    assert result.passed, result.message
    assert result.http_verified
    assert result.ledger_verified
    assert result.usage_verified
    assert result.audit_verified
    assert result.details["escalation_state_after_revoke"] == "CANCELLED"


@pytest.mark.asyncio
async def test_scenario_s06_identity_vectors() -> None:
    """Scenario S6: Drives missing, replayed, wrong-key, suspended, and binding failure vectors."""
    client = FixtureScenarioClient()
    result = await run_s06(client)

    assert result.passed, result.message
    assert result.http_verified
    assert result.ledger_verified
    assert result.usage_verified
    assert result.audit_verified
    assert all(v["passed"] for v in result.details["vector_results"])


@pytest.mark.asyncio
async def test_scenario_s07_prompt_injection() -> None:
    """Scenario S7: Poisoned bill to unverified payee trips breaker in HIGH band; suspends agent."""
    client = FixtureScenarioClient()
    result = await run_s07(client)

    assert result.passed, result.message
    assert result.http_verified
    assert result.ledger_verified
    assert result.usage_verified
    assert result.audit_verified
    assert result.details["response"]["risk"]["level"] == "HIGH"
    assert result.details["response"]["risk"]["breaker_recommended"] is True
    assert result.details["subsequent_blocked"] is True


@pytest.mark.asyncio
async def test_scenario_s08_structuring() -> None:
    """Scenario S8: Rapid attempts escalate LOW -> MEDIUM -> HIGH; breaker suspends agent."""
    client = FixtureScenarioClient()
    result = await run_s08(client)

    assert result.passed, result.message
    assert result.http_verified
    assert result.ledger_verified
    assert result.usage_verified
    assert result.audit_verified
    assert result.details["pending_cancelled"] is True
    # At most 1 payment executed
    assert result.details["attempts"][0]["ok"] is True
    assert result.details["attempts"][3]["res"]["risk"]["level"] == "HIGH"


@pytest.mark.asyncio
async def test_scenario_s09_approval_lifecycle() -> None:
    """Scenario S9: Human-in-the-loop approval variants (S9a-S9e)."""
    client = FixtureScenarioClient()

    # S9a: Valid PIN approval and execution receipt
    res_a = await run_s09a(client)
    assert res_a.passed, res_a.message
    assert res_a.details["decide_response"]["receipt"]["status"] == "EXECUTED"

    # S9b: Customer rejection
    res_b = await run_s09b(client)
    assert res_b.passed, res_b.message
    assert res_b.details["decide_response"]["state"] == "REJECTED"

    # S9c: Expiration sweeper
    res_c = await run_s09c(client)
    assert res_c.passed, res_c.message
    assert res_c.details["final_state"] == "EXPIRED"

    # S9d: Duplicate approval & race single winner
    res_d = await run_s09d(client)
    assert res_d.passed, res_d.message

    # S9e: Bad PIN & 3-pending cap limit
    res_e = await run_s09e(client)
    assert res_e.passed, res_e.message
    assert res_e.details["bad_pin_res"]["error"]["code"] == "E_ESC_BAD_PIN"
    assert res_e.details["limit_res"]["error"]["code"] == "E_ESC_LIMIT_REACHED"

    # Batch driver verification
    all_s9 = await run_s09(client)
    assert len(all_s9) == 5
    assert all(r.passed for r in all_s9)


@pytest.mark.asyncio
async def test_scenario_s10_audit_tamper() -> None:
    """Scenario S10: Untampered audit validates; tampered audit fails verification."""
    client = FixtureScenarioClient()
    result = await run_s10(client)

    assert result.passed, result.message
    assert result.http_verified
    assert result.ledger_verified
    assert result.usage_verified
    assert result.audit_verified
    assert result.details["verify_clean"]["valid"] is True
    assert result.details["verify_tampered"]["valid"] is False


@pytest.mark.asyncio
async def test_scenario_s11_evidence_bundle() -> None:
    """Scenario S11: Cryptographic dispute evidence bundle contains all required components."""
    client = FixtureScenarioClient()
    result = await run_s11(client)

    assert result.passed, result.message
    assert result.http_verified
    assert result.ledger_verified
    assert result.usage_verified
    assert result.audit_verified
    b = result.details["evidence_bundle"]
    assert "mandate" in b
    assert "agent_identity" in b
    assert "proof_evidence" in b
    assert "risk_assessment" in b
    assert "bank_result" in b
    assert "bundle_sig" in b


@pytest.mark.skip(
    reason=(
        "Real SDK / Member A+B gateway stack not yet landed: "
        "missing real agent_sdk.client and real bank_client"
    )
)
def test_scenarios_real_stack_placeholder() -> None:
    """Real-stack scenario execution placeholder pending Member A & B implementations."""
    pass
