"""Member A scoped read routes, customer bootstrap, role-filtered SSE streams, and dev guards."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from contracts.common import ActionType, FinalDecision, RiskLevel
from contracts.core_models import (
    ActionParams,
    ActionStatus,
    CanonicalAction,
    Grant,
    Intent,
    MandateSnapshot,
    MandateStatus,
    MandateTerms,
    StepUpRules,
)
from contracts.identity_models import AuthContext
from contracts.reason_codes import ReasonCode
from contracts.risk_models import RiskResult
from gateway.events.bus import EventBus
from gateway.main import create_app
from gateway.mandates.schema import MandateTable
from gateway.platform.db import Base
from gateway.platform.startup import init_database
from tests.support.a.providers import (
    FakeAgentRegistry,
    FakeBank,
    FakeCatalog,
    FakeHumanAuth,
    FakeTokenService,
)
from tests.support.b.providers import FixtureIdentityVerifier, MemoryAuditLog
from tests.support.c.providers import (
    FixtureRiskEngine,
    MemoryEscalationStore,
)
from tests.support.platform import FrozenClock


def make_test_mandate(
    mandate_id: str = "mnd_read_1",
    customer_id: str = "cust_001",
    agent_id: str = "agt_billpal",
    include_reads: bool = True,
) -> MandateSnapshot:
    """Create a MandateSnapshot with read grants."""
    now = datetime(2026, 11, 1, 0, 0, 0, tzinfo=UTC)
    grants = [
        Grant(
            grant_id="grant_bill_1",
            action=ActionType.BILL_PAY,
            account_ids=("ACC-001",),
            payee_ids=("BILL-ELEC",),
            currency="INR",
            max_per_txn_paise=500000,
            max_total_paise=2000000,
            max_count=10,
            window="mandate",
        )
    ]
    if include_reads:
        grants.extend(
            [
                Grant(
                    grant_id="grant_bal_1",
                    action=ActionType.BALANCE_READ,
                    account_ids=("ACC-001",),
                ),
                Grant(
                    grant_id="grant_tx_1",
                    action=ActionType.TRANSACTIONS_READ,
                    account_ids=("ACC-001",),
                ),
            ]
        )

    terms = MandateTerms(
        mandate_id=mandate_id,
        version=1,
        customer_id=customer_id,
        agent_id=agent_id,
        valid_from=now,
        valid_until=now + timedelta(days=60),
        grants=tuple(grants),
        step_up=StepUpRules(amount_above_paise=300000, new_payee_requires_approval=False),
        risk_profile="balanced",
    )
    return MandateSnapshot(
        terms=terms,
        status=MandateStatus.ACTIVE,
        mandate_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
        sig="sig_test_dummy",
        key_id="key_1",
    )


@pytest.fixture
async def read_stream_env(tmp_path: Path):
    """Spins up a test harness with isolated SQLite DB and injectable providers."""
    db_file = tmp_path / "test_read_stream.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"

    mandate = make_test_mandate()
    default_auth = AuthContext(
        customer_id="cust_001",
        agent_id="agt_billpal",
        mandate_id="mnd_read_1",
        mandate_hash=mandate.mandate_hash,
        jkt="jkt_billpal_test",
        token_jti="tok_jti_001",
        proof_jti="prf_jti_001",
    )
    default_risk = RiskResult(
        score=10,
        level=RiskLevel.LOW,
        recommended=FinalDecision.ALLOW,
        signals=(),
        breaker_recommended=False,
        breaker_reasons=(),
        model_version="rules_v1",
    )

    clock = FrozenClock(datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC))
    human_auth = FakeHumanAuth()
    agent_registry = FakeAgentRegistry()
    token_service = FakeTokenService()
    audit_log = MemoryAuditLog()
    catalog = FakeCatalog()
    event_bus = EventBus()
    escalation_service = MemoryEscalationStore()
    fake_bank = FakeBank()
    identity_verifier = FixtureIdentityVerifier(result=default_auth)
    risk_engine = FixtureRiskEngine(result=default_risk)

    app = create_app(
        profile="test",
        overrides={
            "database": {"gateway_url": db_url},
            "clock": clock,
            "human_auth": human_auth,
            "agent_registry": agent_registry,
            "token_service": token_service,
            "audit_log": audit_log,
            "catalog": catalog,
            "event_bus": event_bus,
            "escalation_service": escalation_service,
            "bank_client": fake_bank,
            "identity_verifier": identity_verifier,
            "risk_engine": risk_engine,
        },
    )

    engine = app.state.engine
    await init_database(engine, Base.metadata)

    # Insert test mandate
    session_factory = app.state.session_factory
    async with session_factory() as session:
        row = MandateTable(
            mandate_id=mandate.terms.mandate_id,
            version=1,
            customer_id=mandate.terms.customer_id,
            agent_id=mandate.terms.agent_id,
            status=str(mandate.status),
            terms_json=mandate.terms.model_dump_json(exclude_none=True),
            mandate_hash=mandate.mandate_hash,
            sig=mandate.sig,
            key_id=mandate.key_id,
            created_at=clock.now(),
        )
        session.add(row)
        await session.commit()

    return {
        "app": app,
        "clock": clock,
        "human_auth": human_auth,
        "agent_registry": agent_registry,
        "audit_log": audit_log,
        "catalog": catalog,
        "event_bus": event_bus,
        "fake_bank": fake_bank,
        "identity_verifier": identity_verifier,
        "risk_engine": risk_engine,
        "mandate": mandate,
        "session_factory": session_factory,
    }


@pytest.mark.asyncio
async def test_balance_read_authorized(read_stream_env):
    """GET /v1/accounts/{id}/balance succeeds when granted in mandate."""
    app = read_stream_env["app"]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/v1/accounts/ACC-001/balance")
        assert res.status_code == 200
        data = res.json()
        assert data["account_id"] == "ACC-001"
        assert data["customer_id"] == "cust_001"
        assert data["balance_paise"] == 12315000
        assert data["currency"] == "INR"
        assert "audit_ref" in data
        assert data["audit_ref"]["seq"] >= 1


@pytest.mark.asyncio
async def test_balance_read_out_of_scope_account_denied(read_stream_env):
    """GET /v1/accounts/{id}/balance fails with 403 when account is outside mandate scope."""
    app = read_stream_env["app"]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/v1/accounts/ACC-999/balance")
        assert res.status_code == 403
        data = res.json()
        assert data["error"]["code"] == ReasonCode.E_REQ_INVALID.value


@pytest.mark.asyncio
async def test_balance_read_action_not_permitted(read_stream_env):
    """GET /v1/accounts/{id}/balance fails with 403 when mandate lacks balance_read grant."""
    app = read_stream_env["app"]
    # Reconfigure mandate without read grants
    no_read_mandate = make_test_mandate(mandate_id="mnd_no_reads", include_reads=False)
    session_factory = read_stream_env["session_factory"]
    clock = read_stream_env["clock"]
    async with session_factory() as session:
        row = MandateTable(
            mandate_id=no_read_mandate.terms.mandate_id,
            version=1,
            customer_id=no_read_mandate.terms.customer_id,
            agent_id=no_read_mandate.terms.agent_id,
            status=str(no_read_mandate.status),
            terms_json=no_read_mandate.terms.model_dump_json(exclude_none=True),
            mandate_hash=no_read_mandate.mandate_hash,
            sig=no_read_mandate.sig,
            key_id=no_read_mandate.key_id,
            created_at=clock.now(),
        )
        session.add(row)
        await session.commit()

    # Reconfigure identity verifier to return this mandate
    read_stream_env["identity_verifier"].result = AuthContext(
        customer_id="cust_001",
        agent_id="agt_billpal",
        mandate_id="mnd_no_reads",
        mandate_hash=no_read_mandate.mandate_hash,
        jkt="jkt_billpal_test",
        token_jti="tok_jti_002",
        proof_jti="prf_jti_002",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/v1/accounts/ACC-001/balance")
        assert res.status_code == 403
        data = res.json()
        assert data["error"]["code"] == ReasonCode.E_REQ_INVALID.value


@pytest.mark.asyncio
async def test_transactions_read_authorized_and_paginated(read_stream_env):
    """GET /v1/accounts/{id}/transactions returns scoped entries and cursor."""
    app = read_stream_env["app"]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/v1/accounts/ACC-001/transactions?limit=10")
        assert res.status_code == 200
        data = res.json()
        assert "items" in data
        assert isinstance(data["items"], list)
        assert len(data["items"]) >= 1
        assert data["items"][0]["account_id"] == "ACC-001"
        assert "audit_ref" in data


@pytest.mark.asyncio
async def test_transactions_read_out_of_scope_denied(read_stream_env):
    """GET /v1/accounts/{id}/transactions fails with 403 on ungranted account."""
    app = read_stream_env["app"]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/v1/accounts/ACC-002/transactions")
        assert res.status_code == 403
        assert res.json()["error"]["code"] == ReasonCode.E_REQ_INVALID.value


@pytest.mark.asyncio
async def test_get_action_status_and_ownership_guard(read_stream_env):
    """GET /v1/actions/{request_id} returns own action status and returns 404 for unowned."""
    app = read_stream_env["app"]
    session_factory = read_stream_env["session_factory"]
    action_store = app.state.deps.action_reader
    clock = read_stream_env["clock"]

    # Record action for customer cust_001 and agent agt_billpal
    canonical_action = CanonicalAction(
        request_id="req_status_test_1",
        customer_id="cust_001",
        agent_id="agt_billpal",
        mandate_id="mnd_read_1",
        mandate_hash="hash_1",
        action_type=ActionType.BILL_PAY,
        params=ActionParams(
            from_account="ACC-001",
            payee_id="BILL-ELEC",
            amount_paise=50000,
            currency="INR",
        ),
        intent=Intent(task="bill", category="utilities"),
        idempotency_key="idemp_status_1",
        received_at=clock.now(),
    )
    async with session_factory() as session:
        await action_store.record_action(
            session=session,
            action=canonical_action,
            bank_idempotency_key="bank_key_status_1",
        )
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Matching agent queries action status -> 200 OK
        res = await client.get(
            "/v1/actions/req_status_test_1",
            headers={"Authorization": "DPoP dummy_proof"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["request_id"] == "req_status_test_1"
        assert data["status"] == ActionStatus.RECEIVED.value
        assert data["customer_id"] == "cust_001"

        # 2. Matching customer queries action status -> 200 OK
        res = await client.get(
            "/v1/actions/req_status_test_1",
            headers={"Authorization": "Bearer cust_session_001"},
        )
        assert res.status_code == 200

        # 3. Different customer (cust_002) queries action status -> 404 (IDOR guard)
        res = await client.get(
            "/v1/actions/req_status_test_1",
            headers={"Authorization": "Bearer cust_session_002"},
        )
        assert res.status_code == 404

        # 4. Non-existent action -> 404
        res = await client.get(
            "/v1/actions/req_non_existent",
            headers={"Authorization": "Bearer cust_session_001"},
        )
        assert res.status_code == 404


@pytest.mark.asyncio
async def test_customer_bootstrap_isolation(read_stream_env):
    """GET /v1/customer/bootstrap returns strictly the authenticated customer's own accounts."""
    app = read_stream_env["app"]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Customer 001
        res1 = await client.get(
            "/v1/customer/bootstrap",
            headers={"Authorization": "Bearer cust_session_001"},
        )
        assert res1.status_code == 200
        data1 = res1.json()
        assert "accounts" in data1
        assert "payees" in data1
        assert "agents" in data1
        account_ids_1 = [a["account_id"] for a in data1["accounts"]]
        assert "ACC-001" in account_ids_1
        assert "ACC-002" not in account_ids_1  # Customer 002's account NEVER leaked

        # Customer 002
        res2 = await client.get(
            "/v1/customer/bootstrap",
            headers={"Authorization": "Bearer cust_session_002"},
        )
        assert res2.status_code == 200
        data2 = res2.json()
        account_ids_2 = [a["account_id"] for a in data2["accounts"]]
        assert "ACC-002" in account_ids_2
        assert "ACC-001" not in account_ids_2  # Customer 001's account NOT in Customer 2's list

        # Unauthenticated request -> 401
        res3 = await client.get("/v1/customer/bootstrap")
        assert res3.status_code == 401


@pytest.mark.asyncio
async def test_customer_stream_isolation_and_redaction(read_stream_env):
    """Customer SSE stream isolates events by customer_id and redacts ops fields."""
    event_bus: EventBus = read_stream_env["event_bus"]

    # Publish events to bus
    await event_bus.publish(
        "bank.executed",
        {
            "customer_id": "cust_001",
            "agent_id": "agt_billpal",
            "request_id": "req_ev_1",
            "status": "EXECUTED",
            "risk": {"score": 95, "circuit_breaker": True},
            "diagnostics": "internal_memory_dump",
        },
    )
    await event_bus.publish(
        "bank.executed",
        {
            "customer_id": "cust_002",
            "agent_id": "agt_other",
            "request_id": "req_ev_2",
            "status": "EXECUTED",
        },
    )
    await event_bus.publish(
        "mandate.revoked",
        {
            "customer_id": "cust_001",
            "agent_id": "agt_billpal",
            "mandate_id": "mnd_read_1",
            "status": "REVOKED",
        },
    )

    app = read_stream_env["app"]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Replay events starting from ID 0 for Customer 001
        async with client.stream(
            "GET",
            "/v1/customer/stream?max_events=2",
            headers={
                "Authorization": "Bearer cust_session_001",
                "Last-Event-ID": "0",
            },
        ) as response:
            assert response.status_code == 200
            assert "text/event-stream" in response.headers["content-type"]

            lines = []
            try:
                async with asyncio.timeout(2.0):
                    async for line in response.aiter_lines():
                        lines.append(line)
                        if "mnd_read_1" in line:
                            break
            except TimeoutError:
                pass

            body = "\n".join(lines)
            # Event 1 received
            assert "req_ev_1" in body
            # Event 2 (Customer 002) MUST NOT be present
            assert "req_ev_2" not in body
            # Ops diagnostics and risk fields MUST be redacted
            assert "diagnostics" not in body
            assert "internal_memory_dump" not in body
            assert '"risk"' not in body


@pytest.mark.asyncio
async def test_sse_evicted_resync_signal(read_stream_env):
    """When client provides an evicted Last-Event-ID, server emits explicit resync event."""
    # Create bus with small limit to force eviction
    bus = EventBus(history_limit=2)
    read_stream_env["app"].state.deps.event_bus = bus

    # Publish 5 events
    for i in range(1, 6):
        await bus.publish(
            "bank.executed",
            {
                "customer_id": "cust_001",
                "agent_id": "agt_billpal",
                "request_id": f"req_evict_{i}",
                "status": "EXECUTED",
            },
        )

    app = read_stream_env["app"]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Request with Last-Event-ID: 1 (evicted)
        async with client.stream(
            "GET",
            "/v1/customer/stream?max_events=1",
            headers={
                "Authorization": "Bearer cust_session_001",
                "Last-Event-ID": "1",
            },
        ) as response:
            assert response.status_code == 200
            lines = []
            try:
                async with asyncio.timeout(2.0):
                    async for line in response.aiter_lines():
                        lines.append(line)
                        if "resync" in line:
                            break
            except TimeoutError:
                pass

            body = "\n".join(lines)
            assert "event: resync" in body


@pytest.mark.asyncio
async def test_ops_stream_full_details_and_role_guard(read_stream_env):
    """Ops SSE stream delivers all events unredacted; rejects customer session."""
    event_bus: EventBus = read_stream_env["event_bus"]
    await event_bus.publish(
        "action.evaluated",
        {
            "customer_id": "cust_001",
            "agent_id": "agt_billpal",
            "request_id": "req_ops_1",
            "risk": {"score": 92, "breaker": True},
            "diagnostics": "ops_debug_info",
        },
    )

    app = read_stream_env["app"]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Customer cannot access ops stream -> 403
        res = await client.get(
            "/v1/ops/stream",
            headers={"Authorization": "Bearer cust_session_001"},
        )
        assert res.status_code == 403
        assert res.json()["error"]["code"] == ReasonCode.E_AUTH_KEY_MISMATCH.value

        # 2. Ops session accesses ops stream -> 200 with unredacted data
        async with client.stream(
            "GET",
            "/v1/ops/stream?max_events=1",
            headers={
                "Authorization": "Bearer ops_session_001",
                "Last-Event-ID": "0",
            },
        ) as response:
            assert response.status_code == 200
            lines = []
            try:
                async with asyncio.timeout(2.0):
                    async for line in response.aiter_lines():
                        lines.append(line)
                        if "req_ops_1" in line:
                            break
            except TimeoutError:
                pass

            body = "\n".join(lines)
            assert "req_ops_1" in body
            assert "ops_debug_info" in body
            assert "risk" in body


@pytest.mark.asyncio
async def test_dev_routes_test_profile_and_real_guard(read_stream_env):
    """Dev routes function in test profile with key, and are completely ABSENT in real profile."""
    app = read_stream_env["app"]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Dev clock advance with valid credential -> 200
        res = await client.post(
            "/v1/dev/clock",
            headers={"X-AgentGate-Dev-Credential": "dev-control-secret"},
            json={"advance_seconds": 3600},
        )
        assert res.status_code == 200
        assert "clock" in res.json()

        # 2. Dev clock set_time with valid credential -> 200
        res = await client.post(
            "/v1/dev/clock",
            headers={"X-Dev-Control-Key": "dev-control-secret"},
            json={"now": "2026-11-25T14:30:00Z"},
        )
        assert res.status_code == 200
        assert res.json()["clock"] == "2026-11-25T14:30:00Z"

        # 3. Missing/bad credential -> 401
        res = await client.post(
            "/v1/dev/clock",
            headers={"X-AgentGate-Dev-Credential": "wrong-secret"},
            json={"advance_seconds": 60},
        )
        assert res.status_code == 401

        # 4. Dev reset with valid credential -> 200
        res = await client.post(
            "/v1/dev/reset",
            headers={"X-AgentGate-Dev-Credential": "dev-control-secret"},
            json={"seed": True},
        )
        assert res.status_code == 200
        assert res.json()["status"] == "RESET"

    # 5. Guard check: In real profile, dev routes MUST NOT be registered (404)
    class RealProvider:
        pass

    real_double = RealProvider()
    real_app = create_app(
        profile="real",
        overrides={
            "database": {"gateway_url": "sqlite+aiosqlite:///:memory:"},
            "identity_verifier": real_double,
            "human_auth": real_double,
            "audit_log": real_double,
            "policy_evaluator": real_double,
            "risk_engine": real_double,
            "escalation_service": real_double,
            "bank_client": real_double,
        },
    )
    # Check that routes are completely unregistered
    route_paths = [getattr(r, "path", None) for r in real_app.routes]
    assert "/v1/dev/clock" not in route_paths
    assert "/v1/dev/reset" not in route_paths
    real_transport = ASGITransport(app=real_app)
    async with AsyncClient(transport=real_transport, base_url="http://test") as real_client:
        res_real = await real_client.post(
            "/v1/dev/clock",
            headers={"X-AgentGate-Dev-Credential": "dev-control-secret"},
            json={"advance_seconds": 60},
        )
        assert res_real.status_code == 404

        res_real_reset = await real_client.post(
            "/v1/dev/reset",
            headers={"X-AgentGate-Dev-Credential": "dev-control-secret"},
            json={"seed": True},
        )
        assert res_real_reset.status_code == 404
