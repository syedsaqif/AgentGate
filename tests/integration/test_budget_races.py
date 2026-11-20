"""Member A integration test: S3 concurrency stress test across 100 randomized runs.

Guarantees:
- Total cap: 400,000 paise (₹4,000), per-txn cap: 200,000 paise (₹2,000).
- Five parallel 150,000 paise requests with unique keys across 100 randomized runs.
- ZERO overspends in every single run (exactly 2 execute, exactly 3 fail budget).
- Exact agreement between gateway mandate_usage and real bank_mock balance/ledger.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from bank_mock.main import create_bank_app
from bank_mock.schema import AccountTable
from contracts.identity_models import AuthContext
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

TEST_BANK_KEY = "test-bank-service-key-races"


@pytest.fixture
async def budget_race_env(tmp_path: Path):
    """Spins up real bank_mock and gateway with real SQLite WAL databases."""
    bank_db = tmp_path / "bank_race.db"
    gw_db = tmp_path / "gateway_race.db"

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
    now_dt = clock.now()
    for i in range(16):
        t = now_dt - timedelta(days=58 - (i * 3))
        history_store.record_attempt(
            attempt_id=f"hist_att_{i}",
            request_id=f"hist_req_{i}",
            customer_id="cust_001",
            agent_id="agt_billpal",
            mandate_id="mnd_s3_stress",
            amount_paise=150000,
            payee_id="BILL-ELEC",
            verdict="ALLOW",
            decision="ALLOW",
            attempted_at=t,
            is_synthetic=True,
        )
        history_store.record_outcome(
            request_id=f"hist_req_{i}",
            customer_id="cust_001",
            agent_id="agt_billpal",
            mandate_id="mnd_s3_stress",
            amount_paise=150000,
            payee_id="BILL-ELEC",
            status="EXECUTED",
            executed_at=t,
            is_synthetic=True,
        )
    risk_engine = DeterministicRiskEngine(history_store=history_store)

    default_auth = AuthContext(
        customer_id="cust_001",
        agent_id="agt_billpal",
        mandate_id="mnd_s3_stress",
        mandate_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
        jkt="jkt_race_test",
        token_jti="tok_race_test",
        proof_jti="prf_race_test",
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
                "deps": app.state.deps,
                "identity_verifier": identity_verifier,
                "clock": clock,
                "history_store": history_store,
            }


@pytest.mark.asyncio
async def test_concurrency_budget_races_100_runs(budget_race_env: dict[str, Any]) -> None:
    """S3 Concurrency Stress Test:

    Total cap 400,000 paise, per-txn 200,000 paise.
    5 parallel 150,000 paise requests across 100 randomized runs.
    Assert:
    - In EVERY run: exactly 2 execute (300,000 paise), exactly 3 fail budget.
    - Zero overspends (never 3 execute, total spend never exceeds 400,000 paise).
    - Perfect consistency between gateway mandate_usage and real bank_mock balance.
    """
    env = budget_race_env
    app = env["app"]
    bank_app = env["bank_app"]
    deps = env["deps"]
    clock: FrozenClock = env["clock"]
    history_store = env["history_store"]
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://gateway") as client:
        # Create S3 mandate: Total cap 400,000 paise (₹4,000), per-txn cap 200,000 paise (₹2,000)
        valid_from = clock.now() - timedelta(hours=1)
        valid_until = clock.now() + timedelta(days=30)
        mandate_payload = {
            "agent_id": "agt_billpal",
            "valid_from": valid_from.isoformat(),
            "valid_until": valid_until.isoformat(),
            "grants": [
                {
                    "grant_id": "grant_s3",
                    "action": "bill_pay",
                    "account_ids": ["ACC-001"],
                    "payee_ids": ["BILL-ELEC"],
                    "currency": "INR",
                    "max_per_txn_paise": 200000,  # ₹2,000 per txn
                    "max_total_paise": 400000,    # ₹4,000 total cap
                    "max_count": 10,
                }
            ],
            "step_up": {"new_payee_requires_approval": False},
            "risk_profile": "balanced",
            "pin": "4821",
        }

        create_resp = await client.post(
            "/v1/customer/mandates",
            headers={"Authorization": "Bearer cust_session_001"},
            json=mandate_payload,
        )
        assert create_resp.status_code == 201, create_resp.text
        mnd_data = create_resp.json()
        mandate_id = mnd_data["mandate"]["terms"]["mandate_id"]
        mandate_hash = mnd_data["mandate"]["mandate_hash"]

        env["identity_verifier"].result = AuthContext(
            customer_id="cust_001",
            agent_id="agt_billpal",
            mandate_id=mandate_id,
            mandate_hash=mandate_hash,
            jkt="jkt_s3_stress",
            token_jti="tok_s3_stress",
            proof_jti="prf_s3_stress",
        )

        initial_bank_balance = 12500000

        # Execute 100 randomized concurrency runs
        for run_idx in range(100):
            # 1. Reset mandate usage, attempt history, and bank balance for clean run isolation
            clock.advance(seconds=600)
            history_store.attempts.clear()
            history_store.attempt_by_id.clear()

            async with deps.unit_of_work as tx:
                await tx.session.execute(
                    update(MandateUsageTable)
                    .where(MandateUsageTable.mandate_id == mandate_id)
                    .values(
                        committed_paise=0,
                        reserved_paise=0,
                        committed_count=0,
                        reserved_count=0,
                    )
                )

            async with bank_app.state.session_factory() as session:
                await session.execute(
                    update(AccountTable)
                    .where(AccountTable.account_id == "ACC-001")
                    .values(balance_paise=initial_bank_balance)
                )
                await session.commit()

            # 2. Build 5 parallel requests each asking for 150,000 paise (₹1,500)
            async def send_worker(
                worker_id: int,
                current_run: int = run_idx,
            ) -> dict[str, Any]:
                # Micro-jitter between 0.0001 and 0.001 seconds to randomize dispatch ordering
                await asyncio.sleep(random.uniform(0.0001, 0.001))
                req_payload = {
                    "from_account": "ACC-001",
                    "payee_id": "BILL-ELEC",
                    "amount_paise": 150000,
                    "currency": "INR",
                    "reference": f"S3-RUN-{current_run}-{worker_id}",
                    "intent": {
                        "task": "pay_electricity",
                        "category": "utilities.electricity",
                    },
                }
                res = await client.post(
                    "/v1/payments/bills",
                    headers={
                        "Authorization": "DPoP agt_token",
                        "Idempotency-Key": (
                            f"idemp_s3_{current_run}_{worker_id}_{uuid.uuid4().hex[:6]}"
                        ),
                    },
                    json=req_payload,
                )
                return {
                    "worker_id": worker_id,
                    "status_code": res.status_code,
                    "body": res.json(),
                }

            # Launch all 5 workers concurrently in randomized order
            worker_ids = list(range(5))
            random.shuffle(worker_ids)
            results = await asyncio.gather(*(send_worker(wid) for wid in worker_ids))

            # 3. Analyze results
            executed_count = sum(1 for r in results if r["status_code"] == 200)
            rejected_count = sum(1 for r in results if r["status_code"] in (400, 403, 422))

            # STRICT INVARIANCE ASSERTION:
            # Zero overspends! Exactly 2 execute (300,000 <= 400,000), exactly 3 rejected.
            assert executed_count == 2, (
                f"Run {run_idx}: Expected 2 executed, got {executed_count}. Results: {results}"
            )
            assert rejected_count == 3, (
                f"Run {run_idx}: Expected 3 rejected, got {rejected_count}. Results: {results}"
            )

            # 4. Verify exact agreement between gateway usage and bank balance
            async with deps.unit_of_work as tx:
                usage_row = await tx.session.scalar(
                    select(MandateUsageTable).where(
                        MandateUsageTable.mandate_id == mandate_id,
                        MandateUsageTable.grant_id == "grant_s3",
                    )
                )
                assert usage_row is not None
                assert usage_row.committed_paise == 300000, f"Run {run_idx}: usage not 300000"
                assert usage_row.reserved_paise == 0, f"Run {run_idx}: lingering reservation"

            async with bank_app.state.session_factory() as session:
                bank_acc = await session.scalar(
                    select(AccountTable).where(AccountTable.account_id == "ACC-001")
                )
                expected_balance = initial_bank_balance - 300000  # 12,200,000 paise
                assert bank_acc.balance_paise == expected_balance, (
                    f"Run {run_idx}: Bank {bank_acc.balance_paise} != expected {expected_balance}"
                )
