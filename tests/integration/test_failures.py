"""Member A integration failure suite: security bypass, timeouts, and revoke races.

Guarantees tested:
1. Bank direct access bypass protection: direct calls to /bank/* without service key return 401.
2. Pre-execution write-ahead audit failure: audit failure rolls back tx, zero debit.
3. Post-bank uncertain timeout & recovery: bank debited, recorded as EXECUTION_UNKNOWN,
   reservation retained, recover_unresolved() commits reservation, retry returns 200 idempotently.
4. Revoke / dispatch race: concurrent mandate revoke and payment dispatch.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from bank_mock.main import create_bank_app
from bank_mock.schema import AccountTable, LedgerTable
from contracts.core_models import BankResult, BankStatus, MandateStatus
from contracts.identity_models import AuthContext
from contracts.reason_codes import ReasonCode
from gateway.executor.bank_client import HttpBankClient
from gateway.main import create_app
from gateway.mandates.schema import MandateTable, MandateUsageTable
from gateway.risk.history import RiskHistoryStore
from gateway.risk.scoring import DeterministicRiskEngine
from simulator.personas import (
    ACCOUNT_PRIMARY,
    CUSTOMER_ID_PRIMARY,
    CUSTOMER_PIN_PRIMARY,
    PAYEE_ELECTRICITY,
)
from tests.support.a.providers import (
    FakeAgentRegistry,
    FakeCatalog,
    FakeHumanAuth,
    FakeTokenService,
)
from tests.support.b.providers import FixtureIdentityVerifier, MemoryAuditLog
from tests.support.platform import FrozenClock

pytestmark = pytest.mark.integration

TEST_BANK_KEY = "test-bank-service-key-failures"


@pytest.fixture
async def failure_env(tmp_path: Path):
    """Spins up isolated bank_mock and gateway with SQLite WAL databases."""
    bank_db = tmp_path / "bank_fail.db"
    gw_db = tmp_path / "gateway_fail.db"

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

    # Seed baseline history so ₹1,850 utility bills are low risk (ALLOW)
    for i in range(16):
        t = clock.now() - timedelta(days=58 - (i * 3))
        history_store.record_attempt(
            attempt_id=f"fail_hist_att_{i}",
            request_id=f"fail_hist_req_{i}",
            customer_id=CUSTOMER_ID_PRIMARY,
            agent_id="agt_billpal",
            mandate_id="mnd_fail_test",
            amount_paise=185000,
            payee_id=PAYEE_ELECTRICITY.payee_id,
            verdict="ALLOW",
            decision="ALLOW",
            attempted_at=t,
            is_synthetic=True,
        )
        history_store.record_outcome(
            request_id=f"fail_hist_req_{i}",
            customer_id=CUSTOMER_ID_PRIMARY,
            agent_id="agt_billpal",
            mandate_id="mnd_fail_test",
            amount_paise=185000,
            payee_id=PAYEE_ELECTRICITY.payee_id,
            status="EXECUTED",
            executed_at=t,
            is_synthetic=True,
        )
    risk_engine = DeterministicRiskEngine(history_store=history_store)

    default_auth = AuthContext(
        customer_id=CUSTOMER_ID_PRIMARY,
        agent_id="agt_billpal",
        mandate_id="mnd_fail_test",
        mandate_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
        jkt="jkt_fail_test",
        token_jti="tok_fail_test",
        proof_jti="prf_fail_test",
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
                "clock": clock,
                "audit_log": audit_log,
                "history_store": history_store,
            }


@pytest.mark.asyncio
async def test_bank_direct_access_bypass_protection(failure_env: dict[str, Any]) -> None:
    """Security bypass test: direct unauthenticated access to /bank/* is rejected."""
    bank_app = failure_env["bank_app"]
    transport = ASGITransport(app=bank_app)

    async with AsyncClient(transport=transport, base_url="http://bank") as client:
        # 1. No service key -> 401 E_AUTH_MISSING
        res_no_key = await client.get(
            f"/bank/accounts/{ACCOUNT_PRIMARY.account_id}/balance?customer_id={CUSTOMER_ID_PRIMARY}"
        )
        assert res_no_key.status_code == 401
        assert res_no_key.json()["error"]["code"] == ReasonCode.E_AUTH_MISSING.value

        # 2. Invalid service key -> 403 E_AUTH_KEY_MISMATCH
        res_bad_key = await client.get(
            f"/bank/accounts/{ACCOUNT_PRIMARY.account_id}/balance?customer_id={CUSTOMER_ID_PRIMARY}",
            headers={"X-Bank-Service-Key": "invalid-secret-key"},
        )
        assert res_bad_key.status_code == 403
        assert res_bad_key.json()["error"]["code"] == ReasonCode.E_AUTH_KEY_MISMATCH.value

        # 3. Direct payment without key -> 401
        res_pay_no_key = await client.post(
            "/bank/payments",
            json={
                "customer_id": CUSTOMER_ID_PRIMARY,
                "from_account": ACCOUNT_PRIMARY.account_id,
                "payee_id": PAYEE_ELECTRICITY.payee_id,
                "amount_paise": 185000,
                "currency": "INR",
                "reference": "BYPASS-TEST",
                "bank_idempotency_key": "bank_bypass_key",
            },
        )
        assert res_pay_no_key.status_code == 401

        # 4. Valid service key -> 200 Success
        res_valid = await client.get(
            f"/bank/accounts/{ACCOUNT_PRIMARY.account_id}/balance?customer_id={CUSTOMER_ID_PRIMARY}",
            headers={"X-Bank-Service-Key": TEST_BANK_KEY},
        )
        assert res_valid.status_code == 200
        assert res_valid.json()["balance_paise"] == 12500000


@pytest.mark.asyncio
async def test_pre_execution_write_ahead_audit_failure(failure_env: dict[str, Any]) -> None:
    """Injected failure in write-ahead audit append must abort the transaction.

    Guarantees:
    - Gateway returns 503 Service Unavailable (E_AUDIT_WRITE_FAILED).
    - Zero bank debit occurs.
    - Zero committed or reserved budget in gateway mandate usage.
    """
    env = failure_env
    app = env["app"]
    bank_app = env["bank_app"]
    deps = env["deps"]
    clock: FrozenClock = env["clock"]
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://gateway") as client:
        # Create mandate
        valid_from = clock.now() - timedelta(hours=1)
        valid_until = clock.now() + timedelta(days=30)
        mandate_payload = {
            "agent_id": "agt_billpal",
            "valid_from": valid_from.isoformat(),
            "valid_until": valid_until.isoformat(),
            "grants": [
                {
                    "grant_id": "grant_fail_1",
                    "action": "bill_pay",
                    "account_ids": [ACCOUNT_PRIMARY.account_id],
                    "payee_ids": [PAYEE_ELECTRICITY.payee_id],
                    "currency": "INR",
                    "max_per_txn_paise": 500000,
                    "max_total_paise": 1000000,
                    "max_count": 10,
                }
            ],
            "step_up": {"new_payee_requires_approval": False},
            "risk_profile": "balanced",
            "pin": CUSTOMER_PIN_PRIMARY,
        }
        create_resp = await client.post(
            "/v1/customer/mandates",
            headers={"Authorization": "Bearer cust_session_001"},
            json=mandate_payload,
        )
        assert create_resp.status_code == 201
        mnd_data = create_resp.json()
        mandate_id = mnd_data["mandate"]["terms"]["mandate_id"]
        mandate_hash = mnd_data["mandate"]["mandate_hash"]

        env["identity_verifier"].result = AuthContext(
            customer_id=CUSTOMER_ID_PRIMARY,
            agent_id="agt_billpal",
            mandate_id=mandate_id,
            mandate_hash=mandate_hash,
            jkt="jkt_fail_test",
            token_jti="tok_fail_test",
            proof_jti="prf_fail_test",
        )

        # Inject audit failure for action.dispatched (write-ahead audit)
        original_append = deps.audit_log.append

        async def failing_append(event: Any, tx: Any = None) -> Any:
            if getattr(event, "event_type", "") == "action.dispatched":
                raise RuntimeError("Simulated write-ahead audit storage failure")
            return await original_append(event, tx=tx)

        deps.audit_log.append = failing_append

        # Attempt bill payment
        pay_payload = {
            "from_account": ACCOUNT_PRIMARY.account_id,
            "payee_id": PAYEE_ELECTRICITY.payee_id,
            "amount_paise": 185000,
            "currency": "INR",
            "reference": "FAIL-AUDIT-TEST",
            "intent": {
                "task": "pay_electricity",
                "category": "utilities.electricity",
            },
        }
        pay_res = await client.post(
            "/v1/payments/bills",
            headers={
                "Authorization": "DPoP agt_token",
                "Idempotency-Key": "idemp_audit_fail_001",
            },
            json=pay_payload,
        )
        assert pay_res.status_code in (500, 502, 503)

        # Restore original append
        deps.audit_log.append = original_append

        # Verify ZERO bank debit
        async with bank_app.state.session_factory() as session:
            bank_acc = await session.scalar(
                select(AccountTable).where(AccountTable.account_id == ACCOUNT_PRIMARY.account_id)
            )
            assert bank_acc.balance_paise == 12500000

            ledgers = list(
                (
                    await session.scalars(
                        select(LedgerTable).where(
                            LedgerTable.account_id == ACCOUNT_PRIMARY.account_id
                        )
                    )
                ).all()
            )
            # Zero ledger entries were created for the failed dispatch
            assert len(ledgers) == 0

        # Verify ZERO committed or reserved reservation in gateway usage
        async with deps.unit_of_work as tx:
            usage = await tx.session.scalar(
                select(MandateUsageTable).where(MandateUsageTable.mandate_id == mandate_id)
            )
            assert usage is not None
            assert usage.committed_paise == 0
            assert usage.reserved_paise == 0


@pytest.mark.asyncio
async def test_post_bank_uncertain_timeout_and_recovery(failure_env: dict[str, Any]) -> None:
    """Uncertain execution timeout & recovery lifecycle test.

    Guarantees:
    - When BankClient experiences timeout after debited bank, returns 202 EXECUTION_UNKNOWN.
    - Reservation is retained in UNKNOWN state.
    - recover_unresolved() scans pending actions, queries bank via bank_idempotency_key.
    - Bank confirms EXECUTED -> recover_unresolved() commits reservation and marks EXECUTED.
    - Subsequent idempotent retry returns 200 EXECUTED with matching bank_operation_id.
    """
    env = failure_env
    app = env["app"]
    bank_app = env["bank_app"]
    deps = env["deps"]
    real_bank_client: HttpBankClient = env["bank_client"]
    clock: FrozenClock = env["clock"]
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://gateway") as client:
        # Create mandate
        valid_from = clock.now() - timedelta(hours=1)
        valid_until = clock.now() + timedelta(days=30)
        mandate_payload = {
            "agent_id": "agt_billpal",
            "valid_from": valid_from.isoformat(),
            "valid_until": valid_until.isoformat(),
            "grants": [
                {
                    "grant_id": "grant_timeout_1",
                    "action": "bill_pay",
                    "account_ids": [ACCOUNT_PRIMARY.account_id],
                    "payee_ids": [PAYEE_ELECTRICITY.payee_id],
                    "currency": "INR",
                    "max_per_txn_paise": 500000,
                    "max_total_paise": 1000000,
                    "max_count": 10,
                }
            ],
            "step_up": {"new_payee_requires_approval": False},
            "risk_profile": "balanced",
            "pin": CUSTOMER_PIN_PRIMARY,
        }
        create_resp = await client.post(
            "/v1/customer/mandates",
            headers={"Authorization": "Bearer cust_session_001"},
            json=mandate_payload,
        )
        assert create_resp.status_code == 201
        mnd_data = create_resp.json()
        mandate_id = mnd_data["mandate"]["terms"]["mandate_id"]
        mandate_hash = mnd_data["mandate"]["mandate_hash"]

        env["identity_verifier"].result = AuthContext(
            customer_id=CUSTOMER_ID_PRIMARY,
            agent_id="agt_billpal",
            mandate_id=mandate_id,
            mandate_hash=mandate_hash,
            jkt="jkt_timeout_test",
            token_jti="tok_timeout_test",
            proof_jti="prf_timeout_test",
        )

        # Mock bank_client.pay_bill to simulate timeout AFTER the real bank has processed
        # the payment
        real_pay_bill = real_bank_client.pay_bill

        async def simulated_timeout_pay_bill(action: Any, bank_idempotency_key: str) -> BankResult:
            # First, execute payment against the real bank so bank records the debit
            await real_pay_bill(action, bank_idempotency_key)
            # Then simulate network disconnect/timeout returning UNKNOWN to gateway
            return BankResult(
                status=BankStatus.UNKNOWN,
                bank_idempotency_key=bank_idempotency_key,
                reason_code=ReasonCode.E_BANK_UNAVAILABLE.value,
            )

        deps.bank_client.pay_bill = simulated_timeout_pay_bill

        # Dispatch bill payment
        idemp_key = "idemp_uncertain_timeout_001"
        pay_payload = {
            "from_account": ACCOUNT_PRIMARY.account_id,
            "payee_id": PAYEE_ELECTRICITY.payee_id,
            "amount_paise": 185000,
            "currency": "INR",
            "reference": "TIMEOUT-TEST",
            "intent": {
                "task": "pay_electricity",
                "category": "utilities.electricity",
            },
        }
        pay_res = await client.post(
            "/v1/payments/bills",
            headers={
                "Authorization": "DPoP agt_token",
                "Idempotency-Key": idemp_key,
            },
            json=pay_payload,
        )
        assert pay_res.status_code == 202
        pay_body = pay_res.json()
        assert pay_body["status"] == "EXECUTION_UNKNOWN"
        req_id = pay_body["request_id"]

        # Restore original bank client pay_bill
        deps.bank_client.pay_bill = real_pay_bill

        # Bank balance is debited
        async with bank_app.state.session_factory() as session:
            bank_acc = await session.scalar(
                select(AccountTable).where(AccountTable.account_id == ACCOUNT_PRIMARY.account_id)
            )
            assert bank_acc.balance_paise == 12500000 - 185000

        # Gateway usage: reservation held, committed is still 0
        async with deps.unit_of_work as tx:
            usage = await tx.session.scalar(
                select(MandateUsageTable).where(MandateUsageTable.mandate_id == mandate_id)
            )
            assert usage.committed_paise == 0
            assert usage.reserved_paise == 185000

        # Trigger recovery (reconcile with bank)
        resolved = await deps.execution_port.recover_unresolved()
        assert req_id in resolved

        # Gateway usage: reservation committed
        async with deps.unit_of_work as tx:
            usage = await tx.session.scalar(
                select(MandateUsageTable).where(MandateUsageTable.mandate_id == mandate_id)
            )
            assert usage.committed_paise == 185000
            assert usage.reserved_paise == 0

        # Subsequent idempotent retry by the agent
        retry_res = await client.post(
            "/v1/payments/bills",
            headers={
                "Authorization": "DPoP agt_token",
                "Idempotency-Key": idemp_key,
            },
            json=pay_payload,
        )
        assert retry_res.status_code == 200
        retry_body = retry_res.json()
        assert retry_body["status"] == "EXECUTED"
        assert retry_body["request_id"] == req_id
        assert retry_body["bank_result"]["status"] == "EXECUTED"
        assert retry_body["bank_result"]["bank_operation_id"] is not None


@pytest.mark.asyncio
async def test_revoke_dispatch_race(failure_env: dict[str, Any]) -> None:
    """Concurrent race between customer revoke and agent payment dispatch.

    Guarantees:
    - In parallel race: payment completes before revoke or is rejected with 403.
    - Zero payments can succeed after mandate is marked REVOKED.
    - Total gateway usage committed matches bank balance exactly.
    """
    env = failure_env
    app = env["app"]
    bank_app = env["bank_app"]
    deps = env["deps"]
    clock: FrozenClock = env["clock"]
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://gateway") as client:
        # Create mandate
        valid_from = clock.now() - timedelta(hours=1)
        valid_until = clock.now() + timedelta(days=30)
        mandate_payload = {
            "agent_id": "agt_billpal",
            "valid_from": valid_from.isoformat(),
            "valid_until": valid_until.isoformat(),
            "grants": [
                {
                    "grant_id": "grant_race_1",
                    "action": "bill_pay",
                    "account_ids": [ACCOUNT_PRIMARY.account_id],
                    "payee_ids": [PAYEE_ELECTRICITY.payee_id],
                    "currency": "INR",
                    "max_per_txn_paise": 500000,
                    "max_total_paise": 2000000,
                    "max_count": 10,
                }
            ],
            "step_up": {"new_payee_requires_approval": False},
            "risk_profile": "balanced",
            "pin": CUSTOMER_PIN_PRIMARY,
        }
        create_resp = await client.post(
            "/v1/customer/mandates",
            headers={"Authorization": "Bearer cust_session_001"},
            json=mandate_payload,
        )
        assert create_resp.status_code == 201
        mnd_data = create_resp.json()
        mandate_id = mnd_data["mandate"]["terms"]["mandate_id"]
        mandate_hash = mnd_data["mandate"]["mandate_hash"]

        env["identity_verifier"].result = AuthContext(
            customer_id=CUSTOMER_ID_PRIMARY,
            agent_id="agt_billpal",
            mandate_id=mandate_id,
            mandate_hash=mandate_hash,
            jkt="jkt_race_test",
            token_jti="tok_race_test",
            proof_jti="prf_race_test",
        )

        initial_bank_balance = 12500000

        # Concurrent workers: 1 revoker, 4 payment dispatchers
        async def do_revoke() -> dict[str, Any]:
            await asyncio.sleep(0.0005)
            res = await client.post(
                f"/v1/customer/mandates/{mandate_id}/revoke",
                headers={"Authorization": "Bearer cust_session_001"},
            )
            return {"type": "revoke", "status_code": res.status_code, "body": res.json()}

        async def do_pay(idx: int) -> dict[str, Any]:
            await asyncio.sleep(0.0005)
            res = await client.post(
                "/v1/payments/bills",
                headers={
                    "Authorization": "DPoP agt_token",
                    "Idempotency-Key": f"idemp_race_pay_{idx}",
                },
                json={
                    "from_account": ACCOUNT_PRIMARY.account_id,
                    "payee_id": PAYEE_ELECTRICITY.payee_id,
                    "amount_paise": 185000,
                    "currency": "INR",
                    "reference": f"RACE-PAY-{idx}",
                    "intent": {
                        "task": "pay_electricity",
                        "category": "utilities.electricity",
                    },
                },
            )
            return {"type": "pay", "idx": idx, "status_code": res.status_code, "body": res.json()}

        tasks = [do_revoke(), do_pay(1), do_pay(2), do_pay(3), do_pay(4)]
        results = await asyncio.gather(*tasks)

        revoke_res = [r for r in results if r["type"] == "revoke"][0]
        assert revoke_res["status_code"] == 200
        assert revoke_res["body"]["status"] == "REVOKED"

        pay_results = [r for r in results if r["type"] == "pay"]
        executed_payments = [p for p in pay_results if p["status_code"] == 200]
        denied_payments = [p for p in pay_results if p["status_code"] == 403]

        # Every payment must either be 200 EXECUTED (before revoke) or 403 E_MANDATE_REVOKED
        assert len(executed_payments) + len(denied_payments) == len(pay_results)
        for d in denied_payments:
            err_code = d["body"]["error"]["code"]
            assert err_code in (
                ReasonCode.E_MANDATE_REVOKED.value,
                ReasonCode.E_AUTH_AGENT_SUSPENDED.value,
            )

        # Post-revoke assertion: ANY new payment MUST be rejected immediately with 403
        post_revoke_pay = await client.post(
            "/v1/payments/bills",
            headers={
                "Authorization": "DPoP agt_token",
                "Idempotency-Key": "idemp_post_race_pay",
            },
            json={
                "from_account": ACCOUNT_PRIMARY.account_id,
                "payee_id": PAYEE_ELECTRICITY.payee_id,
                "amount_paise": 185000,
                "currency": "INR",
                "reference": "POST-RACE-PAY",
                "intent": {
                    "task": "pay_electricity",
                    "category": "utilities.electricity",
                },
            },
        )
        assert post_revoke_pay.status_code == 403
        assert post_revoke_pay.json()["error"]["code"] == ReasonCode.E_MANDATE_REVOKED.value

        # Bank and Gateway consistency
        num_executed = len(executed_payments)
        expected_spent = num_executed * 185000

        async with bank_app.state.session_factory() as session:
            bank_acc = await session.scalar(
                select(AccountTable).where(AccountTable.account_id == ACCOUNT_PRIMARY.account_id)
            )
            assert bank_acc.balance_paise == initial_bank_balance - expected_spent

        async with deps.unit_of_work as tx:
            usage = await tx.session.scalar(
                select(MandateUsageTable).where(MandateUsageTable.mandate_id == mandate_id)
            )
            assert usage.committed_paise == expected_spent
            assert usage.reserved_paise == 0

            mandate_row = await tx.session.scalar(
                select(MandateTable).where(MandateTable.mandate_id == mandate_id)
            )
            assert mandate_row.status == MandateStatus.REVOKED.value
