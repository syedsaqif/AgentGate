"""Integration tests for demo_server infrastructure.

Covers:
- Session management, account binding, and PIN exclusion
- Customer login and demo session establishment
- Bank overview, balance, and ledger retrieval
- Mandate creation and revocation proxying
- Approval decision proxying
- Gate ops event stream relay with sanitization
- Customer notifications stream relay
- Soft reset execution and reporting
- Startup and lifecycle verification
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from demo_server.config import DemoServerConfig
from demo_server.main import create_demo_app
from demo_server.session import SessionStore, global_session_store
from tools.dev import _wait_for_service

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "owned", "unanchored", "foreign", "other_customer", "other_account",
    "blocked", "pending", "missing", "invalid_url",
])
async def test_demo_receipt_qr_ownership_and_projection(case, monkeypatch):
    request_id = "req_" + "a" * 16
    url = "https://example.com/dispute-verifier-v1.html"
    verifier_url = url if case != "invalid_url" else url + "?id=1"
    monkeypatch.setenv("AGENTGATE_DISPUTE_VERIFIER_URL", verifier_url)
    session = global_session_store.create_session("cust_001", "ACC-001", "customer-token")
    gateway = AsyncMock()
    gateway.get_ops_token.return_value = "ops-token"
    receipt_hash = "b" * 64
    action = {
        "request_id": request_id,
        "customer_id": "cust_002" if case == "other_customer" else "cust_001",
        "from_account": "ACC-002" if case == "other_account" else "ACC-001",
        "action_type": "bill_pay",
        "status": {"blocked": "REJECTED", "pending": "PENDING_APPROVAL"}.get(case, "EXECUTED"),
        "audit_ref": {"seq": 4, "entry_hash": receipt_hash},
    }
    proof = {
        "format": "agentgate-dispute-v1", "record": {"request_id": request_id},
        "evidence": {"receipt_seq": 4, "receipt_hash": receipt_hash},
        "witness_checkpoint": None if case == "unanchored" else {"seq": 9},
        "links": ["private-proof-data"],
    }
    async def read(path, token):
        if path.startswith("/v1/actions/"):
            assert token == "customer-token"
            if case == "foreign":
                raise HTTPException(404, detail="Not found")
            return action
        assert token == "ops-token"
        if case == "missing":
            raise HTTPException(409, detail={
                "code": "E_REQ_INVALID", "message": "No signed disclosure exists for this payment.",
            })
        return proof

    gateway.read_payment_evidence.side_effect = read
    app = create_demo_app(gateway_client=gateway)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        path = f"/demo/bank/receipts/{request_id}/qr"
        assert (await client.get(path)).status_code == 401
        response = await client.get(path, headers={"Authorization": f"Bearer {session.session_id}"})
    if case in {"owned", "unanchored"}:
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json() == {
            "request_id": request_id, "receipt_seq": 4, "receipt_hash": receipt_hash,
            "witness_coverage": None if case == "unanchored" else 9, "verifier_url": url,
        }
    else:
        assert response.status_code == (
            404 if case in {"foreign", "other_customer", "other_account"} else 409
        )
        if case != "missing":
            gateway.get_ops_token.assert_not_awaited()
        if case == "invalid_url":
            assert response.json()["error"]["code"] == "UI_INVALID_CONFIG"


@pytest.fixture(autouse=True)
def clean_session_store():
    """Ensure session store is cleared before and after each test."""
    global_session_store.clear()
    yield
    global_session_store.clear()


def test_session_lifecycle_and_pin_exclusion():
    """Verify customer session stores account binding and token but never accepts or stores PIN."""
    store = SessionStore()
    sess = store.create_session(
        customer_id="cust_001",
        account_id="ACC-001",
        gateway_token="gw_token_abc",
        ttl_seconds=2,
    )

    assert sess.session_id.startswith("demo_sess_")
    assert sess.customer_id == "cust_001"
    assert sess.account_id == "ACC-001"
    assert sess.gateway_token == "gw_token_abc"
    # Ensure no pin attribute exists
    assert not hasattr(sess, "pin")
    assert sess.session_id in store._sessions

    # Retrieval
    retrieved = store.get_session(sess.session_id)
    assert retrieved is not None
    assert retrieved.customer_id == "cust_001"

    # Revocation
    store.revoke_session(sess.session_id)
    assert store.get_session(sess.session_id) is None


def test_session_expiration():
    """Verify expired sessions return None and are purged."""
    store = SessionStore()
    sess = store.create_session(
        customer_id="cust_001",
        account_id="ACC-001",
        gateway_token="gw_token_abc",
        ttl_seconds=-1,  # Expired immediately
    )
    assert store.get_session(sess.session_id) is None


@pytest.mark.asyncio
async def test_demo_server_health_check():
    """Verify /health returns 200 with service info."""
    app = create_demo_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["service"] == "demo_server"
        assert data["version"] == "1.0.0"


@pytest.mark.asyncio
async def test_unauthenticated_requests_rejected():
    """Verify endpoints requiring session reject requests with 401 and uniform envelope."""
    app = create_demo_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Overview without auth
        resp = await client.get("/demo/bank/overview")
        assert resp.status_code == 401
        err = resp.json()["error"]
        assert err["code"] == "E_AUTH_MISSING"
        assert "request_id" in err

        # Overview with invalid token
        resp = await client.get(
            "/demo/bank/overview",
            headers={"Authorization": "Bearer invalid_session"},
        )
        assert resp.status_code == 401
        err = resp.json()["error"]
        assert err["code"] == "E_AUTH_EXPIRED"

        # Stream without auth
        resp = await client.get("/demo/bank/stream")
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_customer_login_flow():
    """Verify POST /demo/bank/login contacts Gateway and creates session without echoing PIN."""
    mock_gw = AsyncMock()
    mock_gw.customer_login.return_value = {
        "token": "gw_tok_999",
        "customer_id": "cust_001",
        "role": "customer",
    }
    mock_gw.get_customer_bootstrap.return_value = {
        "accounts": [{"account_id": "ACC-001", "customer_id": "cust_001"}],
        "payees": [],
        "agents": [],
    }

    cfg = DemoServerConfig(customer_id="cust_001", account_id="ACC-001")
    app = create_demo_app(cfg)
    app.state.gateway_client = mock_gw

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login_payload = {"customer_id": "cust_001", "pin": "4821"}
        resp = await client.post("/demo/bank/login", json=login_payload)

        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data
        assert data["customer_id"] == "cust_001"
        assert data["account_id"] == "ACC-001"
        # Verify PIN is never returned in response payload
        assert "pin" not in data
        assert "4821" not in str(data)

        # Verify gateway client was called with the PIN for verification
        mock_gw.customer_login.assert_awaited_once_with(
            customer_id="cust_001",
            pin="4821",
        )

        # Verify session was created in store
        sess = global_session_store.get_session(data["session_id"])
        assert sess is not None
        assert sess.gateway_token == "gw_tok_999"


@pytest.mark.asyncio
async def test_bank_overview_proxy():
    """Verify GET /demo/bank/overview aggregates bank and gateway state correctly."""
    session = global_session_store.create_session(
        customer_id="cust_001",
        account_id="ACC-001",
        gateway_token="gw_cust_token",
    )

    mock_gw = AsyncMock()
    mock_gw.get_customer_bootstrap.return_value = {
        "accounts": [{"account_id": "ACC-001"}],
        "payees": [{"payee_id": "BILL-ELEC", "display_name": "Electricity Board"}],
        "agents": [{"agent_id": "agt_billpal", "name": "BillPal"}],
    }
    mock_gw.list_customer_mandates.return_value = {
        "items": [{"mandate_id": "man_1", "status": "ACTIVE"}]
    }
    mock_gw.list_customer_escalations.return_value = {
        "items": [{"escalation_id": "esc_1", "state": "PENDING"}]
    }
    mock_gw.read_payment_evidence.return_value = {"items": []}

    mock_bank = AsyncMock()
    mock_bank.get_balance.return_value = {
        "account_id": "ACC-001",
        "customer_id": "cust_001",
        "balance_paise": 12500000,
        "currency": "INR",
    }
    mock_bank.get_transactions.return_value = {
        "items": [
            {
                "entry_id": "tx_1",
                "account_id": "ACC-001",
                "amount_paise": 185000,
                "entry_type": "DEBIT",
            }
        ]
    }

    app = create_demo_app()
    app.state.gateway_client = mock_gw
    app.state.bank_client = mock_bank

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/demo/bank/overview",
            headers={"Authorization": f"Bearer {session.session_id}"},
        )
        assert resp.status_code == 200
        data = resp.json()

        assert data["customer_id"] == "cust_001"
        assert data["account_id"] == "ACC-001"
        assert data["balance_paise"] == 12500000
        assert data["currency"] == "INR"
        assert len(data["transactions"]) == 1
        assert len(data["mandates"]) == 1
        assert len(data["escalations"]) == 1
        assert len(data["payees"]) == 1
        assert len(data["agents"]) == 1

        mock_bank.get_balance.assert_awaited_once_with(
            account_id="ACC-001", customer_id="cust_001"
        )


@pytest.mark.asyncio
async def test_mandate_create_and_revoke_proxy():
    """Verify customer mandate creation and revocation proxying."""
    session = global_session_store.create_session(
        customer_id="cust_001",
        account_id="ACC-001",
        gateway_token="gw_cust_token",
    )

    mock_gw = AsyncMock()
    mock_gw.create_customer_mandate.return_value = {
        "mandate": {"terms": {"mandate_id": "man_new_123"}},
        "audit_ref": "audit_abc",
    }
    mock_gw.revoke_customer_mandate.return_value = {
        "mandate_id": "man_new_123",
        "status": "REVOKED",
        "audit_ref": "audit_xyz",
    }

    app = create_demo_app()
    app.state.gateway_client = mock_gw

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Create mandate
        create_payload = {
            "agent_id": "agt_billpal",
            "valid_from": "2026-11-21T00:00:00Z",
            "valid_until": "2026-11-28T00:00:00Z",
            "grants": [],
            "pin": "4821",
        }
        create_resp = await client.post(
            "/demo/bank/mandates",
            json=create_payload,
            headers={"Authorization": f"Bearer {session.session_id}"},
        )
        assert create_resp.status_code == 201
        assert create_resp.json()["mandate"]["terms"]["mandate_id"] == "man_new_123"
        mock_gw.create_customer_mandate.assert_awaited_once_with(
            gateway_token="gw_cust_token",
            payload=create_payload,
        )

        # Revoke mandate
        revoke_resp = await client.post(
            "/demo/bank/mandates/man_new_123/revoke",
            headers={"Authorization": f"Bearer {session.session_id}"},
        )
        assert revoke_resp.status_code == 200
        assert revoke_resp.json()["status"] == "REVOKED"
        mock_gw.revoke_customer_mandate.assert_awaited_once_with(
            gateway_token="gw_cust_token",
            mandate_id="man_new_123",
        )


@pytest.mark.asyncio
async def test_approval_decision_proxy():
    """Verify approval decision submission proxies correctly with PIN."""
    session = global_session_store.create_session(
        customer_id="cust_001",
        account_id="ACC-001",
        gateway_token="gw_cust_token",
    )

    mock_gw = AsyncMock()
    mock_gw.submit_approval_decision.return_value = {
        "escalation_id": "esc_123",
        "state": "APPROVED",
        "result": {"status": "EXECUTED"},
    }

    app = create_demo_app()
    app.state.gateway_client = mock_gw

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/demo/bank/approvals/esc_123",
            json={"decision": "APPROVED", "pin": "4821", "suspend_agent": False},
            headers={"Authorization": f"Bearer {session.session_id}"},
        )
        assert resp.status_code == 200
        assert resp.json()["state"] == "APPROVED"

        mock_gw.submit_approval_decision.assert_awaited_once_with(
            gateway_token="gw_cust_token",
            escalation_id="esc_123",
            decision="APPROVED",
            pin="4821",
            suspend_agent=False,
        )


@pytest.mark.asyncio
async def test_demo_soft_reset():
    """Verify POST /demo/reset reinstates agent, revokes mandates, and prepares fresh mandate."""
    mock_gw = AsyncMock()
    mock_gw.reinstate_agent.return_value = {"agent_id": "agt_billpal", "status": "ACTIVE"}
    mock_gw.customer_login.return_value = {"token": "gw_reset_tok"}
    mock_gw.list_customer_mandates.return_value = {
        "items": [
            {"status": "ACTIVE", "terms": {"mandate_id": "man_old_1"}},
            {"status": "REVOKED", "terms": {"mandate_id": "man_old_2"}},
        ]
    }
    mock_gw.revoke_customer_mandate.return_value = {"status": "REVOKED"}
    mock_gw.create_customer_mandate.return_value = {
        "mandate": {"terms": {"mandate_id": "man_fresh_demo"}}
    }

    cfg = DemoServerConfig(
        customer_id="cust_001",
        account_id="ACC-001",
        customer_pin="4821",
    )
    app = create_demo_app(cfg)
    app.state.gateway_client = mock_gw

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/demo/reset")
        assert resp.status_code == 200
        data = resp.json()

        assert data["status"] == "ok"
        assert data["reinstated_agent"] == "agt_billpal"
        assert data["revoked_mandates"] == ["man_old_1"]
        assert data["created_mandate_id"] == "man_fresh_demo"
        assert data["reset_scope"]["agent_reinstated"] is True
        assert data["reset_scope"]["mandates_reset"] is True
        assert data["reset_scope"]["session_traces_cleared"] is True
        assert data["reset_scope"]["balance_reset"] is False
        assert data["reset_scope"]["risk_history_reset"] is False
        assert (
            "A fresh balance and clean risk history require a separately started"
            in data["message"]
        )

        mock_gw.reinstate_agent.assert_awaited_once_with("agt_billpal")
        mock_gw.revoke_customer_mandate.assert_awaited_once_with(
            "gw_reset_tok", "man_old_1"
        )
        assert mock_gw.create_customer_mandate.await_count == 1


def test_wait_for_service_startup_error():
    """Verify _wait_for_service raises SystemExit when child process terminates during startup."""
    mock_proc = MagicMock()
    mock_proc.poll.return_value = 1  # Exited with code 1
    mock_proc.returncode = 1

    mock_client = MagicMock()

    with pytest.raises(SystemExit) as exc_info:
        _wait_for_service(
            client=mock_client,
            url="http://127.0.0.1:8000/health/ready",
            proc=mock_proc,
            name="test_service",
            attempts=5,
        )

    assert "test_service process exited during startup with code 1" in str(exc_info.value)
