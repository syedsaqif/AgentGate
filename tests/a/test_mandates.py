"""Member A mandate domain test suite: creation, validation, canonical hash, and isolation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from contracts.canonical import canonical_json, sha256_prefixed
from contracts.common import ActionType
from contracts.core_models import Grant, MandateStatus, StepUpRules, TimeWindow
from contracts.reason_codes import ReasonCode
from gateway.main import create_app
from tests.support.b.providers import MemoryAuditLog


@pytest.fixture
def mandate_test_env(tmp_path: Path):
    db_file = tmp_path / "test_gateway_mandates.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    app = create_app(
        profile="test",
        overrides={"database": {"gateway_url": db_url, "wal": True}},
    )
    return app, db_url


def make_valid_mandate_payload(
    agent_id: str = "agt_billpal",
    from_account: str = "ACC-001",
    payee_id: str = "BILL-ELEC",
    max_per_txn_paise: int = 200000,
    max_total_paise: int = 1000000,
    max_count: int = 5,
    pin: str = "4821",
) -> dict:
    now = datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC)
    return {
        "agent_id": agent_id,
        "valid_from": now.isoformat(),
        "valid_until": (now + timedelta(days=30)).isoformat(),
        "grants": [
            {
                "grant_id": "grant_bill_1",
                "action": "bill_pay",
                "account_ids": [from_account],
                "payee_ids": [payee_id],
                "currency": "INR",
                "max_per_txn_paise": max_per_txn_paise,
                "max_total_paise": max_total_paise,
                "max_count": max_count,
                "window": "mandate",
                "time_window": {
                    "timezone": "Asia/Kolkata",
                    "weekdays": [1, 2, 3, 4, 5],
                    "start_local": "09:00",
                    "end_local": "18:00",
                },
            }
        ],
        "step_up": {
            "amount_above_paise": 500000,
            "new_payee_requires_approval": True,
            "always_ask_actions": [],
        },
        "risk_profile": "balanced",
        "pin": pin,
    }


@pytest.mark.asyncio
async def test_mandate_creation_happy_path(mandate_test_env) -> None:
    """Happy path mandate creation: signed terms, usage rows initialized to zero, audit recorded."""
    app, _ = mandate_test_env
    transport = ASGITransport(app=app)
    headers = {"Authorization": "Bearer cust_session_001"}

    payload = make_valid_mandate_payload()

    async with AsyncClient(transport=transport, base_url="http://gateway", headers=headers) as client:
        res = await client.post("/v1/customer/mandates", json=payload)
        assert res.status_code == 201
        data = res.json()
        assert "mandate" in data
        assert "audit_ref" in data

        mandate = data["mandate"]
        assert mandate["status"] == "ACTIVE"
        assert mandate["terms"]["customer_id"] == "cust_001"
        assert mandate["terms"]["agent_id"] == "agt_billpal"
        assert mandate["mandate_hash"].startswith("sha256:")
        assert len(mandate["sig"]) > 0

        # Verify audit event in MemoryAuditLog
        audit_log: MemoryAuditLog = app.state.deps.audit_log
        assert len(audit_log.events) == 1
        ev = audit_log.events[0]
        assert ev.event_type == "mandate.created"
        assert ev.customer_id == "cust_001"
        assert ev.mandate_id == mandate["terms"]["mandate_id"]
        # PIN must never leak into audit safe_payload or ordinary logs
        assert "pin" not in str(ev.safe_payload)
        assert "4821" not in str(ev.safe_payload)

        # Verify usage query
        usage_res = await client.get("/v1/customer/mandates")
        assert usage_res.status_code == 200
        items = usage_res.json()["items"]
        assert len(items) == 1
        usage = items[0]["usage"][0]
        assert usage["committed_paise"] == 0
        assert usage["reserved_paise"] == 0
        assert usage["remaining_paise"] == 1000000
        assert usage["remaining_count"] == 5


@pytest.mark.asyncio
async def test_mandate_creation_pin_failure_rejected(mandate_test_env) -> None:
    """Invalid PIN must fail with HTTP 403 E_ESC_BAD_PIN without creating mandate."""
    app, _ = mandate_test_env
    transport = ASGITransport(app=app)
    headers = {"Authorization": "Bearer cust_session_001"}

    payload = make_valid_mandate_payload(pin="9999")  # Correct PIN is 4821

    async with AsyncClient(transport=transport, base_url="http://gateway", headers=headers) as client:
        res = await client.post("/v1/customer/mandates", json=payload)
        assert res.status_code == 403
        assert res.json()["error"]["code"] == ReasonCode.E_ESC_BAD_PIN.value

        # Zero mandates created
        list_res = await client.get("/v1/customer/mandates")
        assert len(list_res.json()["items"]) == 0


@pytest.mark.asyncio
async def test_mandate_creation_auth_and_role_checks(mandate_test_env) -> None:
    """Missing session token or ops session attempting customer route must be rejected."""
    app, _ = mandate_test_env
    transport = ASGITransport(app=app)
    payload = make_valid_mandate_payload()

    # 1. Missing Authorization header -> 401
    async with AsyncClient(transport=transport, base_url="http://gateway") as client:
        res_no_auth = await client.post("/v1/customer/mandates", json=payload)
        assert res_no_auth.status_code == 401
        assert res_no_auth.json()["error"]["code"] == ReasonCode.E_AUTH_MISSING.value

    # 2. Ops role attempting customer route -> 401 (invalid customer session)
    async with AsyncClient(
        transport=transport,
        base_url="http://gateway",
        headers={"Authorization": "Bearer ops_session_001"},
    ) as client:
        res_ops = await client.post("/v1/customer/mandates", json=payload)
        assert res_ops.status_code == 401


@pytest.mark.asyncio
async def test_mandate_creation_constraint_validations(mandate_test_env) -> None:
    """Verify constraint rejections: unowned account, unknown payee, scope ceiling, etc."""
    app, _ = mandate_test_env
    transport = ASGITransport(app=app)
    headers = {"Authorization": "Bearer cust_session_001"}

    async with AsyncClient(transport=transport, base_url="http://gateway", headers=headers) as client:
        # 1. Account belongs to cust_002, not cust_001
        p_wrong_acc = make_valid_mandate_payload(from_account="ACC-002")
        res1 = await client.post("/v1/customer/mandates", json=p_wrong_acc)
        assert res1.status_code == 400
        assert res1.json()["error"]["code"] == ReasonCode.E_REQ_INVALID.value

        # 2. Payee does not exist in trusted catalog
        p_wrong_payee = make_valid_mandate_payload(payee_id="PAYEE-NONEXISTENT")
        res2 = await client.post("/v1/customer/mandates", json=p_wrong_payee)
        assert res2.status_code == 400

        # 3. Agent is suspended
        p_susp = make_valid_mandate_payload(agent_id="agt_suspended")
        res3 = await client.post("/v1/customer/mandates", json=p_susp)
        assert res3.status_code == 403
        assert res3.json()["error"]["code"] == ReasonCode.E_AUTH_AGENT_SUSPENDED.value

        # 4. Scope ceiling exceeded (agt_billpal has max_scopes ("bill_pay", ...), not "transfer")
        p_transfer = make_valid_mandate_payload()
        p_transfer["grants"][0]["action"] = "transfer"
        res4 = await client.post("/v1/customer/mandates", json=p_transfer)
        assert res4.status_code == 400

        # 5. max_per_txn_paise > max_total_paise
        p_bad_caps = make_valid_mandate_payload(max_per_txn_paise=2000000, max_total_paise=1000000)
        res5 = await client.post("/v1/customer/mandates", json=p_bad_caps)
        assert res5.status_code == 400

        # 6. Overnight / invalid time window (start 22:00 >= end 06:00)
        p_overnight = make_valid_mandate_payload()
        p_overnight["grants"][0]["time_window"]["start_local"] = "22:00"
        p_overnight["grants"][0]["time_window"]["end_local"] = "06:00"
        res6 = await client.post("/v1/customer/mandates", json=p_overnight)
        assert res6.status_code == 400

        # 7. Unsupported budget window
        p_bad_window = make_valid_mandate_payload()
        p_bad_window["grants"][0]["window"] = "monthly"
        res7 = await client.post("/v1/customer/mandates", json=p_bad_window)
        assert res7.status_code == 400


@pytest.mark.asyncio
async def test_mandate_cross_customer_isolation(mandate_test_env) -> None:
    """Customer B's GET /v1/customer/mandates must never see Customer A's mandates."""
    app, _ = mandate_test_env
    transport = ASGITransport(app=app)

    # 1. Create mandate for cust_001
    async with AsyncClient(
        transport=transport,
        base_url="http://gateway",
        headers={"Authorization": "Bearer cust_session_001"},
    ) as client_a:
        payload = make_valid_mandate_payload()
        res_a = await client_a.post("/v1/customer/mandates", json=payload)
        assert res_a.status_code == 201

        list_a = await client_a.get("/v1/customer/mandates")
        assert len(list_a.json()["items"]) == 1

    # 2. Query as cust_002
    async with AsyncClient(
        transport=transport,
        base_url="http://gateway",
        headers={"Authorization": "Bearer cust_session_002"},
    ) as client_b:
        list_b = await client_b.get("/v1/customer/mandates")
        assert list_b.status_code == 200
        # Isolation: zero mandates returned for customer 2
        assert len(list_b.json()["items"]) == 0


@pytest.mark.asyncio
async def test_mandate_creation_audit_failure_rolls_back(mandate_test_env) -> None:
    """If audit log append fails, the unit of work transaction must rollback cleanly."""
    app, _ = mandate_test_env
    transport = ASGITransport(app=app)
    headers = {"Authorization": "Bearer cust_session_001"}

    # Inject failure into audit log
    app.state.deps.audit_log.fail_append = True
    payload = make_valid_mandate_payload()

    async with AsyncClient(transport=transport, base_url="http://gateway", headers=headers) as client:
        res = await client.post("/v1/customer/mandates", json=payload)
        # Audit failure results in 500 / error
        assert res.status_code in (500, 503)

    # Verify atomic rollback: verify zero rows in mandates table
    engine = app.state.engine
    async with engine.connect() as conn:
        res_count = await conn.execute(text("SELECT count(*) FROM mandates;"))
        assert res_count.scalar() == 0
        res_usage = await conn.execute(text("SELECT count(*) FROM mandate_usage;"))
        assert res_usage.scalar() == 0
