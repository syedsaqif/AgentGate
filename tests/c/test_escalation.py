"""Unit tests for Member C escalation service and HTTP routes."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from contracts.common import ActionType, FinalDecision, RiskLevel
from contracts.core_models import ActionParams, CanonicalAction, Intent
from contracts.escalation_models import EscalationDecision, EscalationState
from contracts.identity_models import AuthContext
from contracts.reason_codes import ReasonCode
from contracts.risk_models import RiskResult, RiskSignal
from gateway.errors import register_error_handlers
from gateway.escalation.routes import authorize_escalation_agent, create_escalation_router
from gateway.escalation.service import EscalationConfig, PersistentEscalationService
from gateway.events.bus import EventBus
from tests.support.c.providers import (
    DummyTx,
    FixtureHumanAuth,
    FixtureLifecycle,
    RecordingAuditLog,
    RecordingExecutionPort,
    TrustedRegistry,
)
from tests.support.platform import FrozenClock


def _make_action(
    request_id: str = "req_esc_001",
    customer_id: str = "cust_001",
    agent_id: str = "agt_billpal",
    mandate_id: str = "man_esc_001",
    amount_paise: int = 480000,
    payee_id: str = "BILL-ELEC",
    note: str = "Monthly power bill",
) -> CanonicalAction:
    return CanonicalAction(
        request_id=request_id,
        customer_id=customer_id,
        agent_id=agent_id,
        mandate_id=mandate_id,
        mandate_hash="0" * 64,
        action_type=ActionType.BILL_PAY,
        params=ActionParams(
            from_account="ACC-001",
            payee_id=payee_id,
            amount_paise=amount_paise,
            currency="INR",
            reference="NOV-2026",
        ),
        intent=Intent(
            task="pay_electricity_bill",
            category="utilities.electricity",
            note=note,
        ),
        idempotency_key=f"idem_{request_id}",
        received_at=datetime(2026, 11, 19, 11, 0, 0, tzinfo=UTC),
    )


def _make_assessment(score: int = 50) -> RiskResult:
    return RiskResult(
        score=score,
        level=RiskLevel.MEDIUM,
        recommended=FinalDecision.ESCALATE,
        signals=(
            RiskSignal(
                name="amount_outlier",
                severity=80,
                weight=0.625,
                reason_code="E_RISK_AMOUNT_OUTLIER",
                message="Amount is significantly higher than historical median",
            ),
        ),
        model_version="1.0.0",
    )


@pytest.fixture
def base_clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 11, 19, 11, 0, 0, tzinfo=UTC))


@pytest.fixture
def trusted_registry() -> TrustedRegistry:
    return TrustedRegistry.default()


@pytest.mark.asyncio
async def test_create_and_trusted_summary(
    base_clock: FrozenClock, trusted_registry: TrustedRegistry
) -> None:
    service = PersistentEscalationService(
        clock=base_clock,
        catalog=trusted_registry,
        config=EscalationConfig(default_ttl_seconds=300),
    )
    tx = DummyTx()
    action = _make_action(note="Payment for power bill <script>alert(1)</script>")
    assessment = _make_assessment()

    esc = await service.create(action, assessment, tx)
    assert esc.escalation_id == f"esc_{action.request_id}"
    assert esc.state == EscalationState.PENDING
    assert esc.expires_at == datetime(2026, 11, 19, 11, 5, 0, tzinfo=UTC)

    # Check trusted summary segregation
    summary = esc.trusted_summary
    assert summary["from_account"]["masked_label"] == "**1234"
    assert summary["payee"]["display_name"] == "Electricity Board"
    assert summary["payee"]["verified"] is True
    assert summary["amount"]["amount_paise"] == 480000

    # Agent note must be segregated and explicitly marked unverified
    agent_note = summary["agent_note"]
    assert agent_note["unverified"] is True
    assert "Payment for power bill" in agent_note["text"]
    assert agent_note["label"] == "Unverified Agent Note"


@pytest.mark.asyncio
async def test_max_pending_per_mandate_limit(base_clock: FrozenClock) -> None:
    service = PersistentEscalationService(
        clock=base_clock,
        config=EscalationConfig(max_pending_per_mandate=3),
    )
    tx = DummyTx()
    assessment = _make_assessment()

    # 3 pending creations succeed
    for i in range(1, 4):
        act = _make_action(request_id=f"req_limit_{i}", mandate_id="man_shared")
        await service.create(act, assessment, tx)

    # 4th pending creation for same mandate fails with E_ESC_LIMIT_REACHED
    act_4 = _make_action(request_id="req_limit_4", mandate_id="man_shared")
    with pytest.raises(ValueError, match=ReasonCode.E_ESC_LIMIT_REACHED.value):
        await service.create(act_4, assessment, tx)

    # Different mandate succeeds
    act_diff = _make_action(request_id="req_limit_diff", mandate_id="man_other")
    esc_diff = await service.create(act_diff, assessment, tx)
    assert esc_diff.state == EscalationState.PENDING


@pytest.mark.asyncio
async def test_get_owned_wrong_owner_does_not_reveal(base_clock: FrozenClock) -> None:
    service = PersistentEscalationService(clock=base_clock)
    tx = DummyTx()
    act = _make_action(customer_id="cust_001", agent_id="agt_billpal")
    esc = await service.create(act, _make_assessment(), tx)

    # Customer and Agent can retrieve
    c_esc = await service.get_owned(esc.escalation_id, "cust_001")
    assert c_esc.escalation_id == esc.escalation_id

    a_esc = await service.get_owned(esc.escalation_id, "agt_billpal")
    assert a_esc.escalation_id == esc.escalation_id

    # Wrong owner raises KeyError (same as non-existent)
    with pytest.raises(KeyError, match=f"Escalation {esc.escalation_id} not found"):
        await service.get_owned(esc.escalation_id, "cust_intruder")

    with pytest.raises(KeyError, match="Escalation esc_unknown not found"):
        await service.get_owned("esc_unknown", "cust_001")


@pytest.mark.asyncio
async def test_decide_approve_and_duplicate(base_clock: FrozenClock) -> None:
    audit_log = RecordingAuditLog()
    service = PersistentEscalationService(clock=base_clock, audit_log=audit_log)
    tx = DummyTx()
    act = _make_action()
    esc = await service.create(act, _make_assessment(), tx)

    # Approve
    approved = await service.decide(esc.escalation_id, EscalationDecision.APPROVE, "cust_001", tx)
    assert approved.state == EscalationState.APPROVED
    assert approved.decided_by == "cust_001"
    assert approved.decided_at is not None

    # Audit event emitted
    assert len(audit_log.events) == 1
    assert audit_log.events[0].event_type == "escalation.approved"

    # Duplicate approve raises E_ESC_ALREADY_CONSUMED
    with pytest.raises(ValueError, match=ReasonCode.E_ESC_ALREADY_CONSUMED.value):
        await service.decide(esc.escalation_id, EscalationDecision.APPROVE, "cust_001", tx)


@pytest.mark.asyncio
async def test_decide_reject(base_clock: FrozenClock) -> None:
    audit_log = RecordingAuditLog()
    service = PersistentEscalationService(clock=base_clock, audit_log=audit_log)
    tx = DummyTx()
    act = _make_action()
    esc = await service.create(act, _make_assessment(), tx)

    # Reject
    rejected = await service.decide(esc.escalation_id, EscalationDecision.REJECT, "cust_001", tx)
    assert rejected.state == EscalationState.REJECTED

    # Re-decision raises E_ESC_REJECTED
    with pytest.raises(ValueError, match=ReasonCode.E_ESC_REJECTED.value):
        await service.decide(esc.escalation_id, EscalationDecision.APPROVE, "cust_001", tx)


@pytest.mark.asyncio
async def test_claim_execution_and_duplicate_claim(base_clock: FrozenClock) -> None:
    service = PersistentEscalationService(clock=base_clock)
    tx = DummyTx()
    act = _make_action()
    esc = await service.create(act, _make_assessment(), tx)

    # Cannot claim execution while still PENDING
    with pytest.raises(ValueError, match=ReasonCode.E_REQ_INVALID.value):
        await service.claim_execution(esc.escalation_id, str(esc.action_hash), tx)

    # Approve first
    await service.decide(esc.escalation_id, EscalationDecision.APPROVE, "cust_001", tx)

    # Claim with wrong hash raises E_ESC_HASH_MISMATCH
    with pytest.raises(ValueError, match=ReasonCode.E_ESC_HASH_MISMATCH.value):
        await service.claim_execution(esc.escalation_id, "sha256:wronghash", tx)

    # Valid claim transitions to EXECUTED
    claimed = await service.claim_execution(esc.escalation_id, str(esc.action_hash), tx)
    assert claimed.state == EscalationState.EXECUTED

    # Duplicate claim raises E_ESC_ALREADY_CONSUMED
    with pytest.raises(ValueError, match=ReasonCode.E_ESC_ALREADY_CONSUMED.value):
        await service.claim_execution(esc.escalation_id, str(esc.action_hash), tx)


@pytest.mark.asyncio
async def test_expire_due_and_expired_decision(base_clock: FrozenClock) -> None:
    service = PersistentEscalationService(
        clock=base_clock,
        config=EscalationConfig(default_ttl_seconds=60),
    )
    tx = DummyTx()
    act = _make_action()
    esc = await service.create(act, _make_assessment(), tx)

    # Advance clock past expiry
    base_clock.advance(seconds=61)

    # Expire due sweeper marks it EXPIRED
    expired_count = await service.expire_due(base_clock.now())
    assert expired_count == 1

    # Decide on expired raises E_ESC_EXPIRED
    with pytest.raises(ValueError, match=ReasonCode.E_ESC_EXPIRED.value):
        await service.decide(esc.escalation_id, EscalationDecision.APPROVE, "cust_001", tx)


@pytest.mark.asyncio
async def test_cancel_for_mandate_and_agent(base_clock: FrozenClock) -> None:
    service = PersistentEscalationService(clock=base_clock)
    tx = DummyTx()
    esc1 = await service.create(
        _make_action(request_id="req_c1", mandate_id="m1"), _make_assessment(), tx
    )
    esc2 = await service.create(
        _make_action(request_id="req_c2", agent_id="a1"), _make_assessment(), tx
    )

    # Cancel mandate m1
    await service.cancel_for_mandate("m1", tx)
    with pytest.raises(ValueError, match=ReasonCode.E_ESC_CANCELLED.value):
        await service.decide(esc1.escalation_id, EscalationDecision.APPROVE, "cust_001", tx)

    # Cancel agent a1
    await service.cancel_for_agent("a1", tx)
    with pytest.raises(ValueError, match=ReasonCode.E_ESC_CANCELLED.value):
        await service.decide(esc2.escalation_id, EscalationDecision.APPROVE, "cust_001", tx)


# ---------------------------------------------------------------------------
# Route Tests
# ---------------------------------------------------------------------------


def _setup_test_app(
    clock: FrozenClock,
    event_bus: EventBus | None = None,
) -> tuple[
    TestClient,
    PersistentEscalationService,
    FixtureHumanAuth,
    RecordingExecutionPort,
    FixtureLifecycle,
    RecordingAuditLog,
]:
    audit_log = RecordingAuditLog()
    lifecycle = FixtureLifecycle()
    human_auth = FixtureHumanAuth()
    exec_port = RecordingExecutionPort()
    service = PersistentEscalationService(clock=clock, audit_log=audit_log)

    app = FastAPI()
    register_error_handlers(app)

    def fixture_agent_auth(request: Request) -> AuthContext:
        return AuthContext(
            customer_id="cust_001",
            agent_id="agt_billpal",
            mandate_id=request.headers.get("X-Fixture-Mandate", "man_esc_001"),
            mandate_hash="0" * 64,
            jkt="fixture-thumbprint",
            token_jti="fixture-token",
            proof_jti="fixture-proof",
        )

    app.dependency_overrides[authorize_escalation_agent] = fixture_agent_auth
    router = create_escalation_router(
        event_bus=event_bus,
        service=service,
        human_auth=human_auth,
        lifecycle=lifecycle,
        execution_port=exec_port,
        audit_log=audit_log,
    )
    app.include_router(router)
    client = TestClient(app)
    return client, service, human_auth, exec_port, lifecycle, audit_log


@pytest.mark.asyncio
async def test_route_get_escalations(base_clock: FrozenClock) -> None:
    client, service, _, _, _, _ = _setup_test_app(base_clock)
    tx = DummyTx()
    esc = await service.create(_make_action(), _make_assessment(), tx)

    # Authorized agent query
    resp = client.get(f"/v1/escalations/{esc.escalation_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["escalation_id"] == str(esc.escalation_id)
    assert data["state"] == "PENDING"
    assert "result" in data

    # A verified token for this agent under another mandate returns 404.
    resp_wrong = client.get(
        f"/v1/escalations/{esc.escalation_id}",
        headers={"X-Fixture-Mandate": "another_mandate"},
    )
    assert resp_wrong.status_code == 404
    assert resp_wrong.json()["error"]["code"] == ReasonCode.E_REQ_INVALID.value


@pytest.mark.asyncio
async def test_route_decision_approve_with_pin(base_clock: FrozenClock) -> None:
    bus = EventBus()
    client, service, _, exec_port, _, _ = _setup_test_app(base_clock, bus)
    tx = DummyTx()
    esc = await service.create(_make_action(), _make_assessment(), tx)

    # Approve with correct customer PIN 4821
    resp = client.post(
        f"/v1/customer/escalations/{esc.escalation_id}/decision",
        json={"decision": "APPROVE", "pin": "4821"},
        headers={"Authorization": "Bearer tok_customer_001"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == EscalationState.EXECUTED.value
    assert len(exec_port.request_ids) == 1
    assert exec_port.request_ids[0] == str(esc.request_id)
    rows = [e.data for e in bus.get_since(0) if e.event_type == "gate.step"]
    assert [r["stage"] for r in rows] == ["approval", "execution"]
    assert rows[0]["detail"] == {"decision": "APPROVE"}
    assert rows[1]["detail"] == {"outcome": "EXECUTED"}
    assert rows[0]["seq"] < rows[1]["seq"]
    assert all(r["request_id"] == str(esc.request_id) for r in rows)


@pytest.mark.asyncio
async def test_route_decision_wrong_pin(base_clock: FrozenClock) -> None:
    client, service, _, exec_port, _, audit_log = _setup_test_app(base_clock)
    tx = DummyTx()
    esc = await service.create(_make_action(), _make_assessment(), tx)

    # Approve with wrong PIN 9999
    resp = client.post(
        f"/v1/customer/escalations/{esc.escalation_id}/decision",
        json={"decision": "APPROVE", "pin": "9999"},
        headers={"Authorization": "Bearer tok_customer_001"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == ReasonCode.E_ESC_BAD_PIN.value

    # Escalation must still be PENDING and execution port NOT called
    owned = await service.get_owned(esc.escalation_id, "cust_001")
    assert owned.state == EscalationState.PENDING
    assert len(exec_port.request_ids) == 0

    # PIN failure must be audited
    assert any(e.event_type == "auth.pin_failed" for e in audit_log.events)


@pytest.mark.asyncio
async def test_route_decision_reject_and_suspend(base_clock: FrozenClock) -> None:
    client, service, _, _, lifecycle, _ = _setup_test_app(base_clock)
    tx = DummyTx()
    esc = await service.create(_make_action(), _make_assessment(), tx)

    # Reject and suspend
    resp = client.post(
        f"/v1/customer/escalations/{esc.escalation_id}/decision",
        json={"decision": "REJECT", "suspend_agent": True},
        headers={"Authorization": "Bearer tok_customer_001"},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == EscalationState.REJECTED.value

    # Lifecycle customer delegation suspended
    assert len(lifecycle.suspended_delegations) == 1
    assert lifecycle.suspended_delegations[0][0] == "cust_001"
    assert lifecycle.suspended_delegations[0][1] == "agt_billpal"


@pytest.mark.asyncio
async def test_route_replacement_parameters_forbidden(base_clock: FrozenClock) -> None:
    client, service, _, _, _, _ = _setup_test_app(base_clock)
    tx = DummyTx()
    esc = await service.create(_make_action(), _make_assessment(), tx)

    # Attempting to supply replacement action parameters
    resp = client.post(
        f"/v1/customer/escalations/{esc.escalation_id}/decision",
        json={"decision": "APPROVE", "pin": "4821", "amount_paise": 100},
        headers={"Authorization": "Bearer tok_customer_001"},
    )
    assert resp.status_code == 400  # Normalized to 400 E_REQ_INVALID
    assert resp.json()["error"]["code"] == ReasonCode.E_REQ_INVALID.value
