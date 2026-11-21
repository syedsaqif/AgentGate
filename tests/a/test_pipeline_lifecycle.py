"""Member A request pipeline, decision combiner, and lifecycle coordination test suite."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from contracts.common import ActionType, FinalDecision, RiskLevel
from contracts.core_models import (
    Grant,
    MandateSnapshot,
    MandateStatus,
    MandateTerms,
    StepUpRules,
)
from contracts.identity_models import AgentStatus, AuthContext
from contracts.reason_codes import ReasonCode
from contracts.risk_models import RiskResult
from gateway.events.bus import EventBus
from gateway.executor.service import ExecutionService
from gateway.main import create_app
from gateway.mandates.schema import MandateTable
from gateway.platform.db import Base
from gateway.platform.lifecycle import LifecycleCoordinator
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


def make_pipeline_mandate(
    mandate_id: str = "mnd_pipe_1",
    customer_id: str = "cust_001",
    agent_id: str = "agt_billpal",
    allow_transfers: bool = False,
    max_per_txn_paise: int = 500000,
    max_total_paise: int = 2000000,
    payee_id: str = "BILL-ELEC",
) -> MandateSnapshot:
    """Create a MandateSnapshot for pipeline testing."""
    now = datetime(2026, 11, 1, 0, 0, 0, tzinfo=UTC)
    grants = [
        Grant(
            grant_id="grant_bill_1",
            action=ActionType.BILL_PAY,
            account_ids=("ACC-001",),
            payee_ids=(payee_id,),
            currency="INR",
            max_per_txn_paise=max_per_txn_paise,
            max_total_paise=max_total_paise,
            max_count=10,
            window="mandate",
        )
    ]
    if allow_transfers:
        grants.append(
            Grant(
                grant_id="grant_transfer_1",
                action=ActionType.TRANSFER,
                account_ids=("ACC-001",),
                payee_ids=("ACC-002",),
                currency="INR",
                max_per_txn_paise=max_per_txn_paise,
                max_total_paise=max_total_paise,
                max_count=10,
                window="mandate",
            )
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
async def pipeline_test_env(tmp_path: Path):
    """Spins up a pipeline test harness with isolated SQLite DB and injectable providers."""
    db_file = tmp_path / "test_pipeline.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"

    mandate = make_pipeline_mandate()
    default_auth = AuthContext(
        customer_id="cust_001",
        agent_id="agt_billpal",
        mandate_id="mnd_pipe_1",
        mandate_hash=mandate.mandate_hash,
        jkt="jkt_billpal_test",
        token_jti="tok_jti_001",
        proof_jti="prf_jti_001",
    )
    default_risk = RiskResult(
        score=15,
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
        overrides={"database": {"gateway_url": db_url, "wal": True}},
    )

    # Wire test dependencies
    deps = app.state.deps
    deps.clock = clock
    deps.human_auth = human_auth
    deps.agent_registry = agent_registry
    deps.token_service = token_service
    deps.audit_log = audit_log
    deps.catalog = catalog
    deps.event_bus = event_bus
    deps.escalation_service = escalation_service
    deps.bank_client = fake_bank
    deps.identity_verifier = identity_verifier
    deps.risk_engine = risk_engine

    # Initialize DB schema
    await init_database(app.state.engine, Base.metadata)

    # Seed mandate in DB and mandate service
    async with deps.unit_of_work as tx:
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
            updated_at=clock.now(),
        )
        tx.session.add(row)

    # Ensure lifecycle coordinator is configured
    deps.lifecycle = LifecycleCoordinator(
        unit_of_work=deps.unit_of_work,
        audit_log=deps.audit_log,
        clock=deps.clock,
        event_bus=deps.event_bus,
        token_service=deps.token_service,
        escalation_service=deps.escalation_service,
        agent_registry=deps.agent_registry,
    )

    # Wire execution service with fake_bank
    exec_srv = ExecutionService(
        unit_of_work=deps.unit_of_work,
        action_store=deps.action_reader,
        reservation_manager=deps.reservation_port,
        bank_client=fake_bank,
        audit_log=deps.audit_log,
        mandate_reader=deps.mandate_reader,
        clock=deps.clock,
        session_factory=app.state.session_factory,
    )
    deps.execution_service = exec_srv
    deps.execution_port = exec_srv

    return {
        "app": app,
        "deps": deps,
        "mandate": mandate,
        "bank": fake_bank,
        "audit": audit_log,
        "risk": risk_engine,
        "identity": identity_verifier,
        "event_bus": event_bus,
        "escalations": escalation_service,
        "tokens": token_service,
        "clock": clock,
    }


def make_payment_body(
    from_account: str = "ACC-001",
    payee_id: str = "BILL-ELEC",
    amount_paise: int = 150000,
    currency: str = "INR",
    task: str = "pay_bill",
    category: str = "utilities.electricity",
    note: str = "November power bill",
) -> dict:
    return {
        "from_account": from_account,
        "payee_id": payee_id,
        "amount_paise": amount_paise,
        "currency": currency,
        "reference": "ELEC-2026-11",
        "intent": {
            "task": task,
            "category": category,
            "note": note,
        },
    }


@pytest.mark.asyncio
async def test_happy_path_bill_payment_allow(pipeline_test_env) -> None:
    """Happy path payment: pure policy ALLOW + risk ALLOW -> executes on bank -> 200 OK."""
    env = pipeline_test_env
    app = env["app"]
    bank: FakeBank = env["bank"]
    event_bus: EventBus = env["event_bus"]

    # Subscribe to event bus
    queue = event_bus.subscribe()

    transport = ASGITransport(app=app)
    headers = {"Idempotency-Key": "idemp_pipe_001"}
    body = make_payment_body()

    async with AsyncClient(
        transport=transport, base_url="http://gateway", headers=headers
    ) as client:
        res = await client.post("/v1/payments/bills", json=body)
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "EXECUTED"
        assert data["bank_result"]["status"] == "EXECUTED"
        assert len(bank.calls) == 1

        decision = queue.get_nowait()
        assert decision.event_type == "policy.decision"
        assert decision.data["decision"] == "ALLOW"
        event = queue.get_nowait()
        assert event.event_type == "bank.executed"
        assert event.data["status"] == "EXECUTED"
        assert event.data["customer_id"] == "cust_001"


@pytest.mark.asyncio
async def test_hard_policy_deny_still_scores_risk(pipeline_test_env) -> None:
    """Hard policy DENY (unlisted payee) returns 403; risk scoring must still run."""
    env = pipeline_test_env
    app = env["app"]
    risk: FixtureRiskEngine = env["risk"]
    bank: FakeBank = env["bank"]

    transport = ASGITransport(app=app)
    headers = {"Idempotency-Key": "idemp_pipe_deny_1"}
    # Payee PAYEE-X is outside the mandate's allowed payees
    body = make_payment_body(payee_id="PAYEE-X")

    async with AsyncClient(
        transport=transport, base_url="http://gateway", headers=headers
    ) as client:
        res = await client.post("/v1/payments/bills", json=body)
        assert res.status_code == 403
        assert res.json()["error"]["code"] == ReasonCode.E_POLICY_PAYEE_NOT_ALLOWED.value

        # Zero bank calls
        assert len(bank.calls) == 0

        # Risk engine MUST have been scored and attempt recorded!
        assert len(risk.scores_called) == 1
        assert len(risk.recorded_attempts) == 1


@pytest.mark.asyncio
async def test_risk_high_overrides_policy_allow(pipeline_test_env) -> None:
    """Risk engine returning HIGH risk must override pure policy ALLOW with 403 E_RISK_HIGH."""
    env = pipeline_test_env
    app = env["app"]
    bank: FakeBank = env["bank"]
    risk: FixtureRiskEngine = env["risk"]

    risk.result = RiskResult(
        score=85,
        level=RiskLevel.HIGH,
        recommended=FinalDecision.DENY,
        signals=(),
        breaker_recommended=False,
        breaker_reasons=(),
        model_version="rules_v1",
    )

    transport = ASGITransport(app=app)
    headers = {"Idempotency-Key": "idemp_pipe_high_1"}
    body = make_payment_body()

    async with AsyncClient(
        transport=transport, base_url="http://gateway", headers=headers
    ) as client:
        res = await client.post("/v1/payments/bills", json=body)
        assert res.status_code == 403
        assert res.json()["error"]["code"] == ReasonCode.E_RISK_HIGH.value
        assert len(bank.calls) == 0


@pytest.mark.asyncio
async def test_circuit_breaker_suspends_agent_atomically(pipeline_test_env) -> None:
    """When breaker triggers, agent must be suspended and 403 returned."""
    env = pipeline_test_env
    app = env["app"]
    bank: FakeBank = env["bank"]
    risk: FixtureRiskEngine = env["risk"]
    tokens: FakeTokenService = env["tokens"]
    registry: FakeAgentRegistry = env["deps"].agent_registry

    risk.result = RiskResult(
        score=95,
        level=RiskLevel.HIGH,
        recommended=FinalDecision.DENY,
        signals=(),
        breaker_recommended=True,
        breaker_reasons=("rapid_velocity_exceeded",),
        model_version="rules_v1",
    )

    transport = ASGITransport(app=app)
    headers = {"Idempotency-Key": "idemp_pipe_breaker_1"}
    body = make_payment_body()

    async with AsyncClient(
        transport=transport, base_url="http://gateway", headers=headers
    ) as client:
        res = await client.post("/v1/payments/bills", json=body)
        assert res.status_code == 403
        assert res.json()["error"]["code"] == ReasonCode.E_RISK_BREAKER_OPEN.value
        assert len(bank.calls) == 0

        # Agent must be suspended in registry
        agent = await registry.get("agt_billpal")
        assert agent.status == AgentStatus.SUSPENDED

        # Tokens revoked
        assert "agt_billpal" in tokens.revoked_agents


@pytest.mark.asyncio
async def test_risk_engine_error_escalates_and_never_downgrades_deny(pipeline_test_env) -> None:
    """Risk engine error falls back to score 50 / ESCALATE (202), never downgrading DENY."""
    env = pipeline_test_env
    app = env["app"]
    risk: FixtureRiskEngine = env["risk"]
    risk.error_mode = True

    transport = ASGITransport(app=app)

    # 1. On pure policy ALLOW: risk error -> 202 ESCALATE with Retry-After: 2
    headers = {"Idempotency-Key": "idemp_pipe_err_1"}
    body = make_payment_body()

    async with AsyncClient(
        transport=transport, base_url="http://gateway", headers=headers
    ) as client:
        res = await client.post("/v1/payments/bills", json=body)
        assert res.status_code == 202
        assert res.headers.get("Retry-After") == "2"
        assert res.json()["decision"] == "ESCALATE"
        assert res.json()["status"] == "PENDING_APPROVAL"

    # 2. On pure policy DENY: risk error must NOT downgrade hard DENY -> stays 403
    headers_deny = {"Idempotency-Key": "idemp_pipe_err_deny"}
    body_deny = make_payment_body(payee_id="PAYEE-X")

    async with AsyncClient(
        transport=transport, base_url="http://gateway", headers=headers_deny
    ) as client:
        res_deny = await client.post("/v1/payments/bills", json=body_deny)
        assert res_deny.status_code == 403
        assert res_deny.json()["error"]["code"] == ReasonCode.E_POLICY_PAYEE_NOT_ALLOWED.value


@pytest.mark.asyncio
async def test_soft_step_up_creates_escalation_zero_budget_spend(pipeline_test_env) -> None:
    """Soft step-up rule creates escalation: 202, 0 bank calls, 0 budget spend."""
    env = pipeline_test_env
    app = env["app"]
    bank: FakeBank = env["bank"]
    escalations: MemoryEscalationStore = env["escalations"]

    transport = ASGITransport(app=app)
    headers = {"Idempotency-Key": "idemp_pipe_stepup_1"}
    # Amount 350,000 paise > step_up threshold 300,000 paise
    body = make_payment_body(amount_paise=350000)

    async with AsyncClient(
        transport=transport, base_url="http://gateway", headers=headers
    ) as client:
        res = await client.post("/v1/payments/bills", json=body)
        assert res.status_code == 202
        assert res.headers.get("Retry-After") == "2"
        data = res.json()
        assert data["decision"] == "ESCALATE"
        assert data["status"] == "PENDING_APPROVAL"
        assert "escalation_id" in data

        # Zero bank calls
        assert len(bank.calls) == 0

        # Escalation stored
        assert data["escalation_id"] in escalations.escalations


@pytest.mark.asyncio
async def test_pre_dispatch_audit_failure_prevents_bank_call(pipeline_test_env) -> None:
    """Pre-dispatch audit failure returns 503 with zero bank calls."""
    env = pipeline_test_env
    app = env["app"]
    bank: FakeBank = env["bank"]
    audit: MemoryAuditLog = env["audit"]
    audit.fail_append = True

    transport = ASGITransport(app=app)
    headers = {"Idempotency-Key": "idemp_pipe_audit_fail"}
    body = make_payment_body()

    async with AsyncClient(
        transport=transport, base_url="http://gateway", headers=headers
    ) as client:
        res = await client.post("/v1/payments/bills", json=body)
        assert res.status_code == 503
        assert res.json()["error"]["code"] == ReasonCode.E_AUDIT_WRITE_FAILED.value
        assert len(bank.calls) == 0


@pytest.mark.asyncio
async def test_customer_revoke_mandate_lifecycle(pipeline_test_env) -> None:
    """Revoke mandate revokes tokens, cancels escalations, and updates DB atomically."""
    env = pipeline_test_env
    app = env["app"]
    mandate: MandateSnapshot = env["mandate"]
    tokens: FakeTokenService = env["tokens"]
    event_bus: EventBus = env["event_bus"]
    q = event_bus.subscribe()

    transport = ASGITransport(app=app)
    headers = {"Authorization": "Bearer cust_session_001"}

    async with AsyncClient(
        transport=transport, base_url="http://gateway", headers=headers
    ) as client:
        # Revoke mandate
        res = await client.post(f"/v1/customer/mandates/{mandate.terms.mandate_id}/revoke")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "REVOKED"
        assert data["mandate_id"] == mandate.terms.mandate_id

        # Tokens revoked for mandate
        assert mandate.terms.mandate_id in tokens.revoked_mandates

        # Event published
        ev = await q.get()
        assert ev.event_type == "mandate.revoked"
        assert ev.data["mandate_id"] == mandate.terms.mandate_id

        # Subsequent payments under revoked mandate must be rejected
        pay_headers = {"Idempotency-Key": "idemp_after_revoke"}
        pay_res = await client.post(
            "/v1/payments/bills", json=make_payment_body(), headers=pay_headers
        )
        assert pay_res.status_code == 403
        assert pay_res.json()["error"]["code"] == ReasonCode.E_MANDATE_REVOKED.value


@pytest.mark.asyncio
async def test_customer_revoke_unowned_or_missing_mandate_returns_404(pipeline_test_env) -> None:
    """Revoking a non-existent or unowned mandate returns 404."""
    env = pipeline_test_env
    app = env["app"]
    transport = ASGITransport(app=app)

    # 1. Non-existent mandate
    headers1 = {"Authorization": "Bearer cust_session_001"}
    async with AsyncClient(
        transport=transport, base_url="http://gateway", headers=headers1
    ) as client:
        res1 = await client.post("/v1/customer/mandates/mnd_nonexistent/revoke")
        assert res1.status_code == 404

    # 2. Existing mandate owned by cust_001, called by cust_002
    headers2 = {"Authorization": "Bearer cust_session_002"}
    async with AsyncClient(
        transport=transport, base_url="http://gateway", headers=headers2
    ) as client:
        res2 = await client.post("/v1/customer/mandates/mnd_pipe_1/revoke")
        assert res2.status_code == 404


@pytest.mark.asyncio
async def test_suspend_customer_delegation_isolation(pipeline_test_env) -> None:
    """suspend_customer_delegation revokes ONLY target customer's mandates."""
    env = pipeline_test_env
    coordinator: LifecycleCoordinator = env["deps"].lifecycle
    uow = env["deps"].unit_of_work

    # Seed a second customer's mandate for the same agent
    async with uow as tx:
        m2 = MandateTable(
            mandate_id="mnd_cust_002_other",
            version=1,
            customer_id="cust_002",
            agent_id="agt_billpal",
            status="ACTIVE",
            terms_json="{}",
            mandate_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
            sig="sig_dummy",
            key_id="key_1",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        tx.session.add(m2)

    # Suspend delegation for cust_001
    await coordinator.suspend_customer_delegation("cust_001", "agt_billpal")

    # Verify cust_001 mandate is REVOKED, cust_002 mandate is still ACTIVE
    async with uow as tx:
        s1 = await tx.session.scalar(
            select(MandateTable.status).where(MandateTable.mandate_id == "mnd_pipe_1")
        )
        s2 = await tx.session.scalar(
            select(MandateTable.status).where(MandateTable.mandate_id == "mnd_cust_002_other")
        )
        assert s1 == "REVOKED"
        assert s2 == "ACTIVE"


@pytest.mark.asyncio
async def test_reinstate_agent_does_not_revive_tokens(pipeline_test_env) -> None:
    """reinstate_agent reactivates agent in registry without reviving old tokens or approvals."""
    env = pipeline_test_env
    coordinator: LifecycleCoordinator = env["deps"].lifecycle
    registry: FakeAgentRegistry = env["deps"].agent_registry
    tokens: FakeTokenService = env["tokens"]

    # First suspend
    await coordinator.suspend_agent("agt_billpal")
    agent = await registry.get("agt_billpal")
    assert agent.status == AgentStatus.SUSPENDED
    assert "agt_billpal" in tokens.revoked_agents

    # Reinstate
    await coordinator.reinstate_agent("agt_billpal")
    agent_reinstated = await registry.get("agt_billpal")
    assert agent_reinstated.status == AgentStatus.ACTIVE

    # Revoked tokens list is NOT cleared (old tokens stay dead)
    assert "agt_billpal" in tokens.revoked_agents
