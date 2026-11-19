"""Integration tests driving scenarios S1-S4 and verifying 4-dimension assertions."""

from __future__ import annotations

import pytest

from simulator.client_port import FixtureScenarioClient
from simulator.scenarios.s01 import run_s01
from simulator.scenarios.s02 import run_s02, run_s02a, run_s02b, run_s02c
from simulator.scenarios.s03 import run_s03
from simulator.scenarios.s04 import run_s04

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

    # Batch driver verification
    all_s2 = await run_s02(client)
    assert len(all_s2) == 3
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
    # Two valid actions (active + boundary) debited 185,000 paise each:
    assert result.details["post_balance"] == 12_500_000 - (2 * 185_000)
    assert result.details["committed_paise"] == 2 * 185_000
    assert result.details["remaining_paise"] == 1_000_000 - (2 * 185_000)


@pytest.mark.skip(
    reason=(
        "Real SDK / Member A+B gateway stack not yet landed: "
        "missing real agent_sdk.client and real bank_client"
    )
)
def test_scenarios_real_stack_placeholder() -> None:
    """Real-stack scenario execution placeholder pending Member A & B implementations."""
    pass
