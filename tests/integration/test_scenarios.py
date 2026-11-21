"""Integration tests driving scenarios S1-S11 and verifying 4-dimension assertions."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

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


@pytest.mark.asyncio
async def test_real_stack_scenarios_e2e(tmp_path: Path) -> None:
    """Real-stack scenario execution (S1 and S9) driving real SDK through real gateway."""
    from httpx import ASGITransport, AsyncClient

    from agent_sdk import AgentGateClient, generate_agent_keypair
    from bank_mock.main import create_bank_app
    from bank_mock.schema import BankBase, create_bank_db_engine, create_bank_session_factory
    from bank_mock.service import seed_bank_data
    from contracts.common import HumanRole
    from gateway.audit.schema import create_audit_schema
    from gateway.executor.bank_client import HttpBankClient
    from gateway.identity.schema import (
        create_identity_schema,
        create_sqlite_identity_engine,
    )
    from gateway.main import create_app
    from gateway.platform.db import Base, create_db_engine
    from gateway.platform.startup import init_database

    # 1. Setup real simulated bank
    bank_db = tmp_path / "bank_scenario.db"
    bank_engine = create_bank_db_engine(f"sqlite+aiosqlite:///{bank_db.as_posix()}")
    async with bank_engine.begin() as conn:
        await conn.run_sync(BankBase.metadata.create_all)
    b_sf = create_bank_session_factory(bank_engine)
    async with b_sf() as b_sess:
        await seed_bank_data(b_sess)
    await bank_engine.dispose()

    bank_app = create_bank_app(
        db_url=f"sqlite+aiosqlite:///{bank_db.as_posix()}",
        service_key="test-key-scenario",
    )
    bank_client = HttpBankClient(
        base_url="http://bank",
        service_key="test-key-scenario",
        transport=ASGITransport(app=bank_app),
    )

    # 2. Setup gateway DB & initialize schema
    gw_db = tmp_path / "gw_scenario.db"
    gw_url = f"sqlite+aiosqlite:///{gw_db.as_posix()}"
    gw_sync_engine = create_sqlite_identity_engine(gw_url)
    create_identity_schema(gw_sync_engine)
    create_audit_schema(gw_sync_engine)

    gw_async_engine = create_db_engine(gw_url)
    await init_database(gw_async_engine, Base.metadata)
    await gw_async_engine.dispose()

    # 3. Create real app
    app = create_app(
        profile="real",
        overrides={
            "database": {"gateway_url": gw_url, "bank_url": f"sqlite+aiosqlite:///{bank_db.as_posix()}"},
            "bank_client": bank_client,
        },
    )

    deps = app.state.deps
    agent_kp = generate_agent_keypair()
    agent_id = "agt_billpal"

    # Seed synthetic history so 185,000 paise is recognized as low-risk
    from simulator.history_seed import seed_synthetic_history
    if hasattr(deps.risk_engine, "history_store") and deps.risk_engine.history_store:
        seed_synthetic_history(deps.risk_engine.history_store, now=deps.clock.now())

    # Seed Customer, Ops, and Agent in real identity DB
    await deps.human_auth.seed_customer(
        customer_id="cust_001",
        display_name="Demo Customer",
        pin="4821",
    )
    await deps.human_auth.seed_operator(
        operator_id="ops_admin",
        display_name="Ops Administrator",
        role=HumanRole.OPS,
        password="ops-secret-2026",
    )
    await deps.agent_registry.register(
        agent_id=agent_id,
        name="BillPal",
        vendor="Acme Billing",
        public_jwk=agent_kp.public_jwk(),
        max_scopes=["bill_pay", "balance_read"],
    )

    gw_transport = ASGITransport(app=app)

    async with AsyncClient(transport=gw_transport, base_url="http://127.0.0.1:8000") as http_client:
        # 4. Customer logs in and creates mandate
        login_res = await http_client.post(
            "/v1/customer/login",
            json={"customer_id": "cust_001", "pin": "4821"},
        )
        assert login_res.status_code == 200
        customer_token = login_res.json()["session_token"]
        auth_hdr = {"Authorization": f"Bearer {customer_token}"}

        now = deps.clock.now()
        mandate_res = await http_client.post(
            "/v1/customer/mandates",
            json={
                "agent_id": agent_id,
                "valid_from": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                "valid_until": (now + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
                "grants": [
                    {
                        "grant_id": "grt_bill_01",
                        "action": "bill_pay",
                        "account_ids": ["ACC-001"],
                        "payee_ids": ["BILL-ELEC"],
                        "max_per_txn_paise": 500000,
                        "max_total_paise": 2000000,
                        "max_count": 5,
                        "category": "utilities.electricity",
                    }
                ],
                "step_up": {"amount_above_paise": 400000},
                "risk_profile": "balanced",
                "pin": "4821",
            },
            headers=auth_hdr,
        )
        assert mandate_res.status_code == 201
        mandate_id = mandate_res.json()["mandate"]["terms"]["mandate_id"]

        # 5. SDK Client drives S1 (Happy path under ₹4,000 threshold -> executes directly)
        async with AgentGateClient(
            gateway_url="http://127.0.0.1:8000",
            agent_id=agent_id,
            mandate_id=mandate_id,
            keypair=agent_kp,
            transport=gw_transport,
        ) as sdk_client:
            s1_res = await sdk_client.pay_bill(
                from_account="ACC-001",
                payee_id="BILL-ELEC",
                amount_paise=185000,
                task="Pay electricity bill",
                category="utilities.electricity",
            )
            assert s1_res["status"] == "EXECUTED"
            assert s1_res["audit_ref"]["seq"] >= 1

            # 6. S9a exceeds the ₹4,000 step-up threshold and waits for Customer approval.
            s9_res = await sdk_client.pay_bill(
                from_account="ACC-001",
                payee_id="BILL-ELEC",
                amount_paise=480000,
                task="Pay large electricity bill",
                category="utilities.electricity",
            )
            assert s9_res["status"] == "PENDING_APPROVAL"
            assert s9_res["decision"] == "ESCALATE"
            escalation_id = s9_res["escalation_id"]
            request_id = s9_res["request_id"]
            status = await sdk_client.get_escalation(escalation_id)
            assert status["request_id"] == request_id
            assert status["state"] == "PENDING"

        unauthorized = await http_client.get(f"/v1/escalations/{escalation_id}")
        assert unauthorized.status_code == 401

        # 7. Customer sees escalation in inbox and approves with PIN
        inbox_res = await http_client.get("/v1/customer/escalations", headers=auth_hdr)
        assert inbox_res.status_code == 200
        pending_ids = [item["escalation_id"] for item in inbox_res.json()["items"]]
        assert escalation_id in pending_ids

        decide_res = await http_client.post(
            f"/v1/customer/escalations/{escalation_id}/decision",
            json={"decision": "APPROVE", "pin": "4821"},
            headers=auth_hdr,
        )
        assert decide_res.status_code in (200, 202)
        dec_body = decide_res.json()
        assert dec_body["state"] == "EXECUTED"
        assert dec_body["result"]["status"] == "EXECUTED"
        assert dec_body["result"]["audit_ref"]["seq"] >= 1

        # 8. Ops logs in and verifies audit chain
        ops_login = await http_client.post(
            "/v1/ops/login",
            json={"operator_id": "ops_admin", "password": "ops-secret-2026"},
        )
        assert ops_login.status_code == 200
        ops_token = ops_login.json()["session_token"]
        ops_hdr = {"Authorization": f"Bearer {ops_token}"}

        verify_res = await http_client.get("/v1/audit/verify", headers=ops_hdr)
        assert verify_res.status_code == 200
        assert verify_res.json()["valid"] is True

        # Check evidence bundle for S9 payment
        ev_res = await http_client.get(f"/v1/audit/evidence/{request_id}", headers=ops_hdr)
        assert ev_res.status_code == 200
        bundle = ev_res.json()
        assert bundle["request_id"] == request_id
        assert bundle["bank_outcome"]["status"] == "EXECUTED"

        # A later step-up can be rejected while withdrawing the customer's delegation.
        async with AgentGateClient(
            gateway_url="http://127.0.0.1:8000",
            agent_id=agent_id,
            mandate_id=mandate_id,
            keypair=agent_kp,
            transport=gw_transport,
        ) as sdk_client:
            pending = await sdk_client.pay_bill(
                from_account="ACC-001",
                payee_id="BILL-ELEC",
                amount_paise=410000,
                task="Pay electricity bill",
                category="utilities.electricity",
            )
        assert pending["status"] == "PENDING_APPROVAL"
        rejected = await http_client.post(
            f"/v1/customer/escalations/{pending['escalation_id']}/decision",
            json={"decision": "REJECT", "suspend_agent": True},
            headers=auth_hdr,
        )
        assert rejected.status_code == 200
        assert rejected.json()["state"] == "REJECTED"
        mandates = await http_client.get("/v1/customer/mandates", headers=auth_hdr)
        [mandate] = mandates.json()["items"]
        assert mandate["terms"]["mandate_id"] == mandate_id
        assert mandate["status"] == "REVOKED"
        assert mandate["usage"][0]["committed_paise"] == 665000
        assert mandate["usage"][0]["reserved_paise"] == 0
        agents = await http_client.get("/v1/ops/agents", headers=ops_hdr)
        assert agents.json()["items"][0]["status"] == "ACTIVE"

    gw_sync_engine.dispose()
