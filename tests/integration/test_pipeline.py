"""Member A integration test: end-to-end pipeline flow with real bank_mock and Member C.

Note: Member B (identity and audit) is still stubbed on main; using test doubles
(FixtureIdentityVerifier, MemoryAuditLog, FakeHumanAuth, FakeAgentRegistry,
FakeTokenService, FakeCatalog).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from bank_mock.main import create_bank_app
from bank_mock.schema import AccountTable, LedgerTable
from contracts.identity_models import AuthContext
from contracts.reason_codes import ReasonCode
from gateway.executor.bank_client import HttpBankClient
from gateway.main import create_app
from gateway.mandates.schema import MandateUsageTable
from gateway.risk.history import RiskHistoryStore
from gateway.risk.scoring import DeterministicRiskEngine
from tests.support.a.providers import (
    FakeAgentRegistry,
    FakeCatalog,
    FakeHumanAuth,
    FakeTokenService,
)
from tests.support.b.providers import FixtureIdentityVerifier, MemoryAuditLog
from tests.support.platform import FrozenClock

pytestmark = pytest.mark.integration

TEST_BANK_KEY = "test-bank-service-key-pipeline"


@pytest.fixture
async def pipeline_integration_env(tmp_path: Path):
    """Spins up real bank_mock and gateway with real SQLite WAL databases and real Member C."""
    bank_db = tmp_path / "bank.db"
    gw_db = tmp_path / "gateway.db"

    bank_app = create_bank_app(
        db_url=f"sqlite+aiosqlite:///{bank_db}",
        service_key=TEST_BANK_KEY,
    )

    bank_transport = ASGITransport(app=bank_app)
    real_bank_client = HttpBankClient(
        base_url="http://bank",
        service_key=TEST_BANK_KEY,
        transport=bank_transport,
    )

    clock = FrozenClock(datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC))
    human_auth = FakeHumanAuth()
    agent_registry = FakeAgentRegistry()
    token_service = FakeTokenService()
    audit_log = MemoryAuditLog()
    catalog = FakeCatalog()
    history_store = RiskHistoryStore()
    from simulator.history_seed import seed_synthetic_history

    seed_synthetic_history(history_store, now=clock.now())
    risk_engine = DeterministicRiskEngine(history_store=history_store)

    default_auth = AuthContext(
        customer_id="cust_001",
        agent_id="agt_billpal",
        mandate_id="mnd_pipe_demo_1",
        mandate_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
        jkt="jkt_pipeline_test",
        token_jti="tok_pipeline_test",
        proof_jti="prf_pipeline_test",
    )
    identity_verifier = FixtureIdentityVerifier(result=default_auth)

    app = create_app(
        profile="scripted",
        overrides={
            "database": {"gateway_url": f"sqlite+aiosqlite:///{gw_db}", "wal": True},
            "bank_client": real_bank_client,
            "clock": clock,
            "human_auth": human_auth,
            "agent_registry": agent_registry,
            "token_service": token_service,
            "audit_log": audit_log,
            "catalog": catalog,
            "identity_verifier": identity_verifier,
            "risk_engine": risk_engine,
        },
    )

    async with bank_app.router.lifespan_context(bank_app):
        async with app.router.lifespan_context(app):
            yield {
                "app": app,
                "bank_app": bank_app,
                "bank_client": real_bank_client,
                "deps": app.state.deps,
                "identity_verifier": identity_verifier,
                "default_auth": default_auth,
                "clock": clock,
                "history_store": history_store,
            }


@pytest.mark.asyncio
async def test_end_to_end_pipeline_flow(pipeline_integration_env: dict[str, Any]) -> None:
    """Full cross-module pipeline:

    create mandate -> S1 happy path (1,850 bill pay, bank 12500000 -> 12315000,
    budget 1000000 -> 815000) -> S2a-c scope denials (cap, payee, action) ->
    revoke -> post-revoke denial.
    """
    env = pipeline_integration_env
    app = env["app"]
    bank_app = env["bank_app"]
    deps = env["deps"]
    clock: FrozenClock = env["clock"]
    transport = ASGITransport(app=app)

    # 1. Customer creates mandate
    valid_from = clock.now() - timedelta(hours=1)
    valid_until = clock.now() + timedelta(days=30)
    mandate_payload = {
        "agent_id": "agt_billpal",
        "valid_from": valid_from.isoformat(),
        "valid_until": valid_until.isoformat(),
        "grants": [
            {
                "grant_id": "grant_bill_1",
                "action": "bill_pay",
                "account_ids": ["ACC-001"],
                "payee_ids": ["BILL-ELEC", "BILL-MOBILE"],
                "currency": "INR",
                "max_per_txn_paise": 300000,  # ₹3,000 cap
                "max_total_paise": 1000000,   # ₹10,000 total budget
                "max_count": 5,
            },
            {
                "grant_id": "grant_read_1",
                "action": "balance_read",
                "account_ids": ["ACC-001"],
                "currency": "INR",
                "max_count": 10,
            },
        ],
        "step_up": {
            "new_payee_requires_approval": False,
        },
        "risk_profile": "balanced",
        "pin": "4821",
    }

    async with AsyncClient(transport=transport, base_url="http://gateway") as client:
        # Create mandate as authenticated customer
        create_resp = await client.post(
            "/v1/customer/mandates",
            headers={"Authorization": "Bearer cust_session_001"},
            json=mandate_payload,
        )
        assert create_resp.status_code == 201, create_resp.text
        mnd_data = create_resp.json()
        mandate_id = mnd_data["mandate"]["terms"]["mandate_id"]
        assert mnd_data["mandate"]["status"] == "ACTIVE"

        # Update auth context with the real created mandate_id
        env["identity_verifier"].result = AuthContext(
            customer_id="cust_001",
            agent_id="agt_billpal",
            mandate_id=mandate_id,
            mandate_hash=mnd_data["mandate"]["mandate_hash"],
            jkt="jkt_pipeline_test",
            token_jti="tok_pipeline_test",
            proof_jti="prf_pipeline_test",
        )

        # Initial bank balance check on real mock bank: ACC-001 has 12,500,000 paise
        async with bank_app.state.session_factory() as session:
            bank_acc = await session.scalar(
                select(AccountTable).where(AccountTable.account_id == "ACC-001")
            )
            assert bank_acc.balance_paise == 12500000

        # 2. S1 Happy Path: Pay ₹1,850 (185,000 paise) bill pay to BILL-ELEC
        payment_payload = {
            "from_account": "ACC-001",
            "payee_id": "BILL-ELEC",
            "amount_paise": 185000,
            "currency": "INR",
            "reference": "ELEC-NOV-2026",
            "intent": {
                "task": "pay_electricity",
                "category": "utilities.electricity",
            },
        }
        pay_resp = await client.post(
            "/v1/payments/bills",
            headers={
                "Authorization": "DPoP agt_token",
                "Idempotency-Key": "idemp_pipe_s1_001",
            },
            json=payment_payload,
        )
        assert pay_resp.status_code == 200, pay_resp.text
        pay_data = pay_resp.json()
        assert pay_data["status"] == "EXECUTED"
        assert pay_data.get("bank_result", {}).get("bank_operation_id") is not None

        # Verify real bank_mock balance debited: 12500000 -> 12315000
        async with bank_app.state.session_factory() as session:
            bank_acc = await session.scalar(
                select(AccountTable).where(AccountTable.account_id == "ACC-001")
            )
            assert bank_acc.balance_paise == 12315000

        # Verify gateway mandate_usage recorded: 185,000 spent out of 1,000,000
        async with deps.unit_of_work as tx:
            usage_row = await tx.session.scalar(
                select(MandateUsageTable).where(
                    MandateUsageTable.mandate_id == mandate_id,
                    MandateUsageTable.grant_id == "grant_bill_1",
                )
            )
            assert usage_row is not None
            assert usage_row.committed_paise == 185000
            assert 1000000 - usage_row.committed_paise == 815000

        # 3. S2a-c Scope Denials
        # S2a: Over per-transaction cap (₹3,500 = 350,000 paise > 300,000 cap)
        s2a_resp = await client.post(
            "/v1/payments/bills",
            headers={
                "Authorization": "DPoP agt_token",
                "Idempotency-Key": "idemp_pipe_s2a",
            },
            json={
                "from_account": "ACC-001",
                "payee_id": "BILL-ELEC",
                "amount_paise": 350000,
                "currency": "INR",
                "intent": {"task": "pay_over_cap", "category": "utilities.electricity"},
            },
        )
        assert s2a_resp.status_code == 403
        assert s2a_resp.json()["error"]["code"] in (
            "E_POLICY_PER_TXN_EXCEEDED",
            "E_POLICY_TOTAL_EXCEEDED",
            ReasonCode.E_REQ_INVALID.value,
        )

        # S2b: Disallowed payee (PAYEE-X not in allowed payees ["BILL-ELEC", "BILL-MOBILE"])
        s2b_resp = await client.post(
            "/v1/payments/bills",
            headers={
                "Authorization": "DPoP agt_token",
                "Idempotency-Key": "idemp_pipe_s2b",
            },
            json={
                "from_account": "ACC-001",
                "payee_id": "PAYEE-X",
                "amount_paise": 150000,
                "currency": "INR",
                "intent": {"task": "pay_unverified", "category": "personal.transfer"},
            },
        )
        assert s2b_resp.status_code == 403

        # S2c: Disallowed action (transfer is not in allowed actions)
        s2c_resp = await client.post(
            "/v1/transfers",
            headers={
                "Authorization": "DPoP agt_token",
                "Idempotency-Key": "idemp_pipe_s2c",
            },
            json={
                "from_account": "ACC-001",
                "payee_id": "ACC-002",
                "amount_paise": 100000,
                "currency": "INR",
                "intent": {"task": "transfer_funds", "category": "account.transfer"},
            },
        )
        assert s2c_resp.status_code == 403

        # Confirm bank balance and budget completely untouched by denials
        async with bank_app.state.session_factory() as session:
            bank_acc = await session.scalar(
                select(AccountTable).where(AccountTable.account_id == "ACC-001")
            )
            assert bank_acc.balance_paise == 12315000

        # 4. Revoke Mandate
        revoke_resp = await client.post(
            f"/v1/customer/mandates/{mandate_id}/revoke",
            headers={"Authorization": "Bearer cust_session_001"},
            json={"reason": "Testing lifecycle revoke"},
        )
        assert revoke_resp.status_code == 200, revoke_resp.text
        assert revoke_resp.json()["status"] == "REVOKED"

        # 5. Post-Revoke Denial
        post_revoke_resp = await client.post(
            "/v1/payments/bills",
            headers={
                "Authorization": "DPoP agt_token",
                "Idempotency-Key": "idemp_pipe_post_revoke",
            },
            json=payment_payload,
        )
        assert post_revoke_resp.status_code == 403
        assert post_revoke_resp.json()["error"]["code"] in (
            ReasonCode.E_MANDATE_REVOKED.value,
            ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH.value,
            ReasonCode.E_REQ_INVALID.value,
        )

        # Bank balance remains exactly 12,315,000 paise (zero additional debit)
        async with bank_app.state.session_factory() as session:
            bank_acc = await session.scalar(
                select(AccountTable).where(AccountTable.account_id == "ACC-001")
            )
            assert bank_acc.balance_paise == 12315000


@pytest.mark.parametrize("reserved", [False, True], ids=["committed", "reserved"])
@pytest.mark.parametrize(
    ("check", "budget", "count"),
    [
        ("total_cap", 369999, 5),
        ("count_cap", 1000000, 1),
    ],
)
async def test_money_policy_counts_prior_usage(
    pipeline_integration_env, monkeypatch, check, budget, count, reserved
):
    env = pipeline_integration_env
    now = env["clock"].now()
    deps = env["deps"]
    bank_entered = asyncio.Event()
    release_bank = asyncio.Event()
    pay_bill = env["bank_client"].pay_bill

    async def hold_reserved_payment(action, bank_key):
        # Dispatch has committed its reservation. Delay only the first bank call.
        if not bank_entered.is_set():
            bank_entered.set()
            await release_bank.wait()
        return await pay_bill(action, bank_key)

    async def bank_state():
        async with env["bank_app"].state.session_factory() as session:
            account = await session.get(AccountTable, "ACC-001")
            debits = await session.scalar(
                select(func.count())
                .select_from(LedgerTable)
                .where(LedgerTable.account_id == "ACC-001", LedgerTable.entry_type == "DEBIT")
            )
            return account.balance_paise, debits

    async with AsyncClient(
        transport=ASGITransport(app=env["app"]), base_url="http://gateway"
    ) as client:
        created = await client.post(
            "/v1/customer/mandates",
            headers={
                "Authorization": "Bearer cust_session_001",
            },
            json={
                "agent_id": "agt_billpal",
                "valid_from": (now - timedelta(hours=1)).isoformat(),
                "valid_until": (now + timedelta(days=1)).isoformat(),
                "risk_profile": "balanced",
                "pin": "4821",
                "step_up": {"new_payee_requires_approval": False},
                "grants": [
                    {
                        "grant_id": "electric",
                        "action": "bill_pay",
                        "currency": "INR",
                        "account_ids": ["ACC-001"],
                        "payee_ids": ["BILL-ELEC"],
                        "max_per_txn_paise": 250000,
                        "max_total_paise": budget,
                        "max_count": count,
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        mandate = created.json()["mandate"]
        mandate_id = mandate["terms"]["mandate_id"]
        env["identity_verifier"].result = env["default_auth"].model_copy(
            update={
                "mandate_id": mandate_id,
                "mandate_hash": mandate["mandate_hash"],
            }
        )

        async def pay(key):
            return await client.post(
                "/v1/payments/bills",
                headers={"Idempotency-Key": key},
                json={
                    "from_account": "ACC-001",
                    "payee_id": "BILL-ELEC",
                    "currency": "INR",
                    "amount_paise": 185000,
                    "intent": {"task": "pay bill", "category": "utilities.electricity"},
                },
            )

        if reserved:
            monkeypatch.setattr(env["bank_client"], "pay_bill", hold_reserved_payment)
        first = asyncio.create_task(pay("usage-first"))
        try:
            if reserved:
                await asyncio.wait_for(bank_entered.wait(), timeout=10)
            else:
                assert (await first).status_code == 200
            (usage,) = await deps.usage_reader.get(mandate_id)
            assert (usage.committed_paise, usage.reserved_paise) == (
                (0, 185000) if reserved else (185000, 0)
            )
            assert (usage.committed_count, usage.reserved_count) == ((0, 1) if reserved else (1, 0))
            before = await bank_state()
            denied = await pay("usage-second")
            assert await bank_state() == before
            error = denied.json()["error"]
            steps = [
                e.data
                for e in deps.event_bus.get_since(0)
                if e.event_type == "gate.step" and e.request_id == error["request_id"]
            ]
            (decision,) = [s["detail"]["verdict"] for s in steps if s["check"] == "final_decision"]
            assert (decision, denied.status_code, error["code"]) == ("DENY", 403, "E_REQ_INVALID")
            (decisive,) = [s for s in steps if s["detail"].get("decisive")]
            assert (decisive["stage"], decisive["check"], decisive["status"]) == (
                "policy",
                check,
                "fail",
            )
            assert (await deps.usage_reader.get(mandate_id)) == (usage,)
        finally:
            release_bank.set()
            first_result = await first
        assert first_result.status_code == 200 and first_result.json()["status"] == "EXECUTED"
        assert await bank_state() == (12315000, 1)
