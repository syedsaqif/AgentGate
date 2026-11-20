"""Member A mock bank and BankClient test suite."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient

from bank_mock.main import create_bank_app
from contracts.common import ActionType
from contracts.core_models import (
    ActionParams,
    BankStatus,
    CanonicalAction,
    Intent,
)
from contracts.reason_codes import ReasonCode
from gateway.errors import AgentGateError
from gateway.executor.bank_client import HttpBankClient
from tests.support.a.providers import FakeBank

TEST_SERVICE_KEY = "test-bank-secret-key-1234"


def make_canonical_action(
    request_id: str = "req_test_1",
    customer_id: str = "cust_001",
    from_account: str = "ACC-001",
    payee_id: str = "BILL-ELEC",
    amount_paise: int = 185000,
    action_type: ActionType = ActionType.BILL_PAY,
    idempotency_key: str = "agent-idemp-001",
) -> CanonicalAction:
    return CanonicalAction(
        request_id=request_id,
        customer_id=customer_id,
        agent_id="agt_billpal",
        mandate_id="mnd_demo_1",
        mandate_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
        action_type=action_type,
        params=ActionParams(
            from_account=from_account,
            payee_id=payee_id,
            amount_paise=amount_paise,
            currency="INR",
            reference="TEST-NOV-2026",
        ),
        intent=Intent(task="pay_bill", category="utilities.electricity"),
        idempotency_key=idempotency_key,
        received_at=datetime.now(UTC),
    )


@pytest.fixture
async def bank_app(tmp_path: Path) -> AsyncGenerator[Any, None]:
    db_file = tmp_path / "test_bank.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    app = create_bank_app(db_url=db_url, service_key=TEST_SERVICE_KEY)
    async with app.router.lifespan_context(app):
        yield app


@pytest.mark.asyncio
async def test_bank_bypass_unauthenticated_fails(bank_app: Any) -> None:
    """Direct unauthenticated calls to /bank/* must be rejected with 401 or 403."""
    transport = ASGITransport(app=bank_app)
    async with AsyncClient(transport=transport, base_url="http://bank") as client:
        # 1. Missing credential header -> 401
        res_missing = await client.get("/bank/accounts/ACC-001/balance?customer_id=cust_001")
        assert res_missing.status_code == 401
        assert res_missing.json()["error"]["code"] == ReasonCode.E_AUTH_MISSING.value

        # 2. Wrong credential header -> 403
        res_wrong = await client.get(
            "/bank/accounts/ACC-001/balance?customer_id=cust_001",
            headers={"X-Bank-Service-Key": "wrong-secret-token"},
        )
        assert res_wrong.status_code == 403
        assert res_wrong.json()["error"]["code"] == ReasonCode.E_AUTH_KEY_MISMATCH.value


@pytest.mark.asyncio
async def test_bank_account_ownership_validation(bank_app: Any) -> None:
    """Requests attempting to read or debit an account with wrong customer ID must be rejected."""
    transport = ASGITransport(app=bank_app)
    headers = {"X-Bank-Service-Key": TEST_SERVICE_KEY}

    async with AsyncClient(transport=transport, base_url="http://bank", headers=headers) as client:
        # Valid customer -> 200
        res_valid = await client.get("/bank/accounts/ACC-001/balance?customer_id=cust_001")
        assert res_valid.status_code == 200
        assert res_valid.json()["balance_paise"] == 12500000

        # Wrong customer -> 404
        res_wrong_cust = await client.get("/bank/accounts/ACC-001/balance?customer_id=cust_002")
        assert res_wrong_cust.status_code == 404

        # Non-existent account -> 404
        res_no_acc = await client.get("/bank/accounts/ACC-999/balance?customer_id=cust_001")
        assert res_no_acc.status_code == 404


@pytest.mark.asyncio
async def test_bank_payment_same_key_debits_once(bank_app: Any) -> None:
    """Same idempotency key + identical payload must debit once and return the same result."""
    transport = ASGITransport(app=bank_app)
    headers = {"X-Bank-Service-Key": TEST_SERVICE_KEY}

    payload = {
        "customer_id": "cust_001",
        "from_account": "ACC-001",
        "payee_id": "BILL-ELEC",
        "amount_paise": 185000,
        "currency": "INR",
        "reference": "NOV-2026",
        "bank_idempotency_key": "bank-idem-001",
    }

    async with AsyncClient(transport=transport, base_url="http://bank", headers=headers) as client:
        # First call: executes debit (12500000 - 185000 = 12315000)
        res1 = await client.post("/bank/payments", json=payload)
        assert res1.status_code == 200
        data1 = res1.json()
        assert data1["status"] == "EXECUTED"
        assert data1["balance_paise"] == 12315000
        op_id_1 = data1["bank_operation_id"]
        assert op_id_1 is not None

        # Second call with identical key & payload: returns original result without second debit
        res2 = await client.post("/bank/payments", json=payload)
        assert res2.status_code == 200
        data2 = res2.json()
        assert data2["status"] == "EXECUTED"
        assert data2["balance_paise"] == 12315000
        assert data2["bank_operation_id"] == op_id_1

        # Check balance remains 12315000
        res_bal = await client.get("/bank/accounts/ACC-001/balance?customer_id=cust_001")
        assert res_bal.json()["balance_paise"] == 12315000

        # Check ledger has exactly 1 entry
        res_tx = await client.get("/bank/accounts/ACC-001/transactions?customer_id=cust_001")
        items = res_tx.json()["items"]
        assert len(items) == 1
        assert items[0]["amount_paise"] == 185000
        assert items[0]["balance_after_paise"] == 12315000


@pytest.mark.asyncio
async def test_bank_payment_same_key_changed_payload_conflicts(bank_app: Any) -> None:
    """Same idempotency key with altered payload must return 409 conflict and not debit."""
    transport = ASGITransport(app=bank_app)
    headers = {"X-Bank-Service-Key": TEST_SERVICE_KEY}

    payload = {
        "customer_id": "cust_001",
        "from_account": "ACC-001",
        "payee_id": "BILL-ELEC",
        "amount_paise": 185000,
        "currency": "INR",
        "reference": "NOV-2026",
        "bank_idempotency_key": "bank-idem-conflict-1",
    }

    async with AsyncClient(transport=transport, base_url="http://bank", headers=headers) as client:
        # First call succeeds
        res1 = await client.post("/bank/payments", json=payload)
        assert res1.status_code == 200

        # Second call with changed amount -> 409
        changed_payload = dict(payload, amount_paise=200000)
        res2 = await client.post("/bank/payments", json=changed_payload)
        assert res2.status_code == 409
        assert res2.json()["error"]["code"] == ReasonCode.E_REQ_IDEMPOTENCY_CONFLICT.value

        # Check balance was only debited once for 185000
        res_bal = await client.get("/bank/accounts/ACC-001/balance?customer_id=cust_001")
        assert res_bal.json()["balance_paise"] == 12315000


@pytest.mark.asyncio
async def test_bank_insufficient_funds_rejected(bank_app: Any) -> None:
    """Payments exceeding account balance must fail with 422 E_BANK_INSUFFICIENT_FUNDS."""
    transport = ASGITransport(app=bank_app)
    headers = {"X-Bank-Service-Key": TEST_SERVICE_KEY}

    huge_payload = {
        "customer_id": "cust_001",
        "from_account": "ACC-001",
        "payee_id": "BILL-ELEC",
        "amount_paise": 50000000,  # 500,000 INR > 125,000 INR
        "currency": "INR",
        "reference": "OVERSPEND",
        "bank_idempotency_key": "bank-huge-001",
    }

    async with AsyncClient(transport=transport, base_url="http://bank", headers=headers) as client:
        res = await client.post("/bank/payments", json=huge_payload)
        assert res.status_code == 422
        assert res.json()["error"]["code"] == ReasonCode.E_BANK_INSUFFICIENT_FUNDS.value

        # Balance remains pristine
        res_bal = await client.get("/bank/accounts/ACC-001/balance?customer_id=cust_001")
        assert res_bal.json()["balance_paise"] == 12500000


@pytest.mark.asyncio
async def test_bank_operation_lookup(bank_app: Any) -> None:
    """GET /bank/operations/{key} must return the recorded executed operation."""
    transport = ASGITransport(app=bank_app)
    headers = {"X-Bank-Service-Key": TEST_SERVICE_KEY}
    key = "bank-lookup-test-001"

    payload = {
        "customer_id": "cust_001",
        "from_account": "ACC-001",
        "payee_id": "BILL-ELEC",
        "amount_paise": 100000,
        "currency": "INR",
        "reference": "LOOKUP-TEST",
        "bank_idempotency_key": key,
    }

    async with AsyncClient(transport=transport, base_url="http://bank", headers=headers) as client:
        # Execute
        res_post = await client.post("/bank/payments", json=payload)
        assert res_post.status_code == 200
        op_id = res_post.json()["bank_operation_id"]

        # Lookup
        res_lookup = await client.get(f"/bank/operations/{key}")
        assert res_lookup.status_code == 200
        data = res_lookup.json()
        assert data["status"] == "EXECUTED"
        assert data["bank_operation_id"] == op_id
        assert data["bank_idempotency_key"] == key

        # Non-existent key -> 404
        res_none = await client.get("/bank/operations/non-existent-key")
        assert res_none.status_code == 404


@pytest.mark.asyncio
async def test_http_bank_client_with_bank_mock(bank_app: Any) -> None:
    """HttpBankClient wired to bank_mock via ASGITransport satisfies BankClient protocol."""
    transport = ASGITransport(app=bank_app)
    client = HttpBankClient(
        base_url="http://bank",
        service_key=TEST_SERVICE_KEY,
        transport=transport,
    )

    action = make_canonical_action(amount_paise=250000)

    # 1. Execute pay_bill
    res = await client.pay_bill(action, bank_idempotency_key="client-test-key-001")
    assert res.status == BankStatus.EXECUTED
    assert res.balance_paise == 12250000
    assert res.bank_operation_id is not None

    # 2. Lookup existing
    res_lookup = await client.lookup("client-test-key-001")
    assert res_lookup.status == BankStatus.EXECUTED
    assert res_lookup.bank_operation_id == res.bank_operation_id

    # 3. Insufficient funds through client
    action_huge = make_canonical_action(amount_paise=99999999)
    res_huge = await client.pay_bill(action_huge, bank_idempotency_key="client-huge-001")
    assert res_huge.status == BankStatus.REJECTED
    assert res_huge.reason_code == ReasonCode.E_BANK_INSUFFICIENT_FUNDS.value

    # 4. Idempotency conflict raises AgentGateError 409
    action_altered = make_canonical_action(amount_paise=300000)
    with pytest.raises(AgentGateError) as exc_info:
        await client.pay_bill(action_altered, bank_idempotency_key="client-test-key-001")
    assert exc_info.value.code == ReasonCode.E_REQ_IDEMPOTENCY_CONFLICT.value
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_fake_bank_double_satisfies_contract() -> None:
    """FakeBank satisfies the BankClient protocol across both success and fault injection modes."""
    fake = FakeBank()
    action = make_canonical_action()

    # Success
    res = await fake.pay_bill(action, "fake-key-1")
    assert res.status == BankStatus.EXECUTED
    assert "req_test_1" in fake.calls

    # Lookup
    res_lookup = await fake.lookup("fake-key-1")
    assert res_lookup.status == BankStatus.EXECUTED

    # Fault injection: insufficient funds
    fake_funds = FakeBank(fail_insufficient_funds=True)
    res_funds = await fake_funds.pay_bill(action, "fake-key-2")
    assert res_funds.status == BankStatus.REJECTED
    assert res_funds.reason_code == ReasonCode.E_BANK_INSUFFICIENT_FUNDS.value

    # Fault injection: timeout / unknown
    fake_timeout = FakeBank(timeout=True)
    res_timeout = await fake_timeout.pay_bill(action, "fake-key-3")
    assert res_timeout.status == BankStatus.UNKNOWN

    # Fault injection: outage
    fake_outage = FakeBank(fail_unavailable=True)
    with pytest.raises(AgentGateError) as exc_outage:
        await fake_outage.pay_bill(action, "fake-key-4")
    assert exc_outage.value.code == ReasonCode.E_BANK_UNAVAILABLE.value

    # Fault injection: conflict
    fake_conflict = FakeBank(fail_idempotency_conflict=True)
    with pytest.raises(AgentGateError) as exc_conflict:
        await fake_conflict.pay_bill(action, "fake-key-5")
    assert exc_conflict.value.code == ReasonCode.E_REQ_IDEMPOTENCY_CONFLICT.value
