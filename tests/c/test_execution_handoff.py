"""Unit tests for Member C approval to execution handoff (Task C06)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from contracts.common import ActionType, FinalDecision, RiskLevel
from contracts.core_models import (
    ActionParams,
    ActionStatus,
    BankStatus,
    CanonicalAction,
    Grant,
    Intent,
    MandateSnapshot,
    MandateStatus,
    MandateTerms,
    UsageSnapshot,
)
from contracts.escalation_models import EscalationDecision, EscalationState
from contracts.identity_models import AgentPublicIdentity, AgentStatus
from contracts.reason_codes import ReasonCode
from contracts.risk_models import RiskResult, RiskSignal
from gateway.escalation.service import EscalationConfig, PersistentEscalationService
from tests.support.c.providers import (
    DummyTx,
    FixtureAgentRegistry,
    FixtureMandateReader,
    FixtureUsageReader,
    RecordingExecutionPort,
)
from tests.support.platform import FrozenClock


def _make_action(
    request_id: str = "req_handoff_001",
    customer_id: str = "cust_001",
    agent_id: str = "agt_billpal",
    mandate_id: str = "man_handoff_001",
    amount_paise: int = 480000,
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
            payee_id="BILL-ELEC",
            amount_paise=amount_paise,
            currency="INR",
            reference="NOV-2026",
        ),
        intent=Intent(
            task="pay_electricity_bill",
            category="utilities.electricity",
            note="Handoff test bill",
        ),
        idempotency_key=f"idem_{request_id}",
        received_at=datetime(2026, 11, 19, 11, 0, 0, tzinfo=UTC),
    )


def _make_assessment() -> RiskResult:
    return RiskResult(
        score=50,
        level=RiskLevel.MEDIUM,
        recommended=FinalDecision.ESCALATE,
        signals=(
            RiskSignal(
                name="amount_outlier",
                severity=80,
                weight=0.625,
                reason_code="E_RISK_AMOUNT_OUTLIER",
                message="Outlier requiring approval",
            ),
        ),
        model_version="1.0.0",
    )


def _make_mandate_snapshot(
    mandate_id: str = "man_handoff_001",
    status: MandateStatus = MandateStatus.ACTIVE,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
) -> MandateSnapshot:
    terms = MandateTerms(
        mandate_id=mandate_id,
        version=1,
        customer_id="cust_001",
        agent_id="agt_billpal",
        valid_from=valid_from or datetime(2026, 11, 1, 0, 0, tzinfo=UTC),
        valid_until=valid_until or datetime(2026, 12, 1, 0, 0, tzinfo=UTC),
        grants=(
            Grant(
                grant_id="grant_001",
                action=ActionType.BILL_PAY,
                account_ids=("ACC-001",),
                payee_ids=("BILL-ELEC",),
                max_per_txn_paise=500000,
                max_total_paise=1000000,
            ),
        ),
    )
    return MandateSnapshot(
        terms=terms,
        status=status,
        mandate_hash="0" * 64,
        sig="sig_test_mandate",
        key_id="key_test_mandate",
    )


def _make_agent_identity(
    agent_id: str = "agt_billpal",
    status: AgentStatus = AgentStatus.ACTIVE,
) -> AgentPublicIdentity:
    return AgentPublicIdentity(
        agent_id=agent_id,
        name="BillPal Agent",
        vendor="BillPal Inc",
        public_jwk={"kty": "OKP", "crv": "Ed25519", "x": "test_key"},
        jkt="jkt_billpal",
        max_scopes=("bill_pay",),
        status=status,
    )


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 11, 19, 11, 0, 0, tzinfo=UTC))


@pytest.mark.asyncio
async def test_handoff_stored_ids_only(clock: FrozenClock) -> None:
    mandate_reader = FixtureMandateReader(
        mandates={"man_handoff_001": _make_mandate_snapshot()}
    )
    agent_registry = FixtureAgentRegistry(
        agents={"agt_billpal": _make_agent_identity()}
    )
    exec_port = RecordingExecutionPort()
    service = PersistentEscalationService(
        clock=clock,
        mandate_reader=mandate_reader,
        agent_registry=agent_registry,
    )
    tx = DummyTx()

    act = _make_action(request_id="req_h_001", mandate_id="man_handoff_001")
    esc = await service.create(act, _make_assessment(), tx)
    await service.decide(esc.escalation_id, EscalationDecision.APPROVE, "cust_001", tx)

    # Execute handoff
    exec_res = await service.execute_approved(esc.escalation_id, tx, exec_port)

    # Proves ONLY stored IDs are sent
    assert len(exec_port.request_ids) == 1
    assert exec_port.request_ids[0] == "req_h_001"
    assert exec_port.escalation_ids[0] == esc.escalation_id
    assert exec_res.status == ActionStatus.EXECUTED

    # State in store is now EXECUTED
    final_esc = await service.get_owned(esc.escalation_id, "cust_001")
    assert final_esc.state == EscalationState.EXECUTED


@pytest.mark.asyncio
async def test_no_bank_call_on_reject(clock: FrozenClock) -> None:
    exec_port = RecordingExecutionPort()
    service = PersistentEscalationService(clock=clock)
    tx = DummyTx()

    act = _make_action(request_id="req_rej_001")
    esc = await service.create(act, _make_assessment(), tx)
    await service.decide(esc.escalation_id, EscalationDecision.REJECT, "cust_001", tx)

    # Attempting handoff on rejected action raises E_ESC_REJECTED
    with pytest.raises(ValueError, match=ReasonCode.E_ESC_REJECTED.value):
        await service.execute_approved(esc.escalation_id, tx, exec_port)

    # Proves no bank call occurred
    assert len(exec_port.request_ids) == 0


@pytest.mark.asyncio
async def test_no_bank_call_on_expiry(clock: FrozenClock) -> None:
    exec_port = RecordingExecutionPort()
    service = PersistentEscalationService(
        clock=clock,
        config=EscalationConfig(default_ttl_seconds=60),
    )
    tx = DummyTx()

    act = _make_action(request_id="req_exp_001")
    esc = await service.create(act, _make_assessment(), tx)
    await service.decide(esc.escalation_id, EscalationDecision.APPROVE, "cust_001", tx)

    # Advance clock past expiry deadline
    clock.advance(seconds=61)

    # Attempting handoff on expired action raises E_ESC_EXPIRED
    with pytest.raises(ValueError, match=ReasonCode.E_ESC_EXPIRED.value):
        await service.execute_approved(esc.escalation_id, tx, exec_port)

    # Proves no bank call occurred
    assert len(exec_port.request_ids) == 0


@pytest.mark.asyncio
async def test_no_bank_call_on_mandate_revocation(clock: FrozenClock) -> None:
    mandate_reader = FixtureMandateReader(
        mandates={
            "man_handoff_001": _make_mandate_snapshot(status=MandateStatus.REVOKED)
        }
    )
    exec_port = RecordingExecutionPort()
    service = PersistentEscalationService(
        clock=clock,
        mandate_reader=mandate_reader,
    )
    tx = DummyTx()

    act = _make_action(request_id="req_rev_001", mandate_id="man_handoff_001")
    esc = await service.create(act, _make_assessment(), tx)
    await service.decide(esc.escalation_id, EscalationDecision.APPROVE, "cust_001", tx)

    # Mandate is revoked: handoff must fail with E_MANDATE_REVOKED
    with pytest.raises(ValueError, match=ReasonCode.E_MANDATE_REVOKED.value):
        await service.execute_approved(esc.escalation_id, tx, exec_port)

    # Proves no bank call occurred
    assert len(exec_port.request_ids) == 0


@pytest.mark.asyncio
async def test_no_bank_call_on_agent_suspension(clock: FrozenClock) -> None:
    mandate_reader = FixtureMandateReader(
        mandates={"man_handoff_001": _make_mandate_snapshot()}
    )
    agent_registry = FixtureAgentRegistry(
        agents={
            "agt_billpal": _make_agent_identity(status=AgentStatus.SUSPENDED)
        }
    )
    exec_port = RecordingExecutionPort()
    service = PersistentEscalationService(
        clock=clock,
        mandate_reader=mandate_reader,
        agent_registry=agent_registry,
    )
    tx = DummyTx()

    act = _make_action(request_id="req_susp_001", mandate_id="man_handoff_001")
    esc = await service.create(act, _make_assessment(), tx)
    await service.decide(esc.escalation_id, EscalationDecision.APPROVE, "cust_001", tx)

    # Agent is suspended: handoff must fail with E_AUTH_AGENT_SUSPENDED
    with pytest.raises(ValueError, match=ReasonCode.E_AUTH_AGENT_SUSPENDED.value):
        await service.execute_approved(esc.escalation_id, tx, exec_port)

    # Proves no bank call occurred
    assert len(exec_port.request_ids) == 0


@pytest.mark.asyncio
async def test_no_bank_call_on_budget_exhaustion(clock: FrozenClock) -> None:
    mandate_reader = FixtureMandateReader(
        mandates={"man_handoff_001": _make_mandate_snapshot()}
    )
    # Remaining paise is only 1,000, but action requires 480,000
    usage_reader = FixtureUsageReader(
        usages={
            "man_handoff_001": (
                UsageSnapshot(
                    mandate_id="man_handoff_001",
                    grant_id="grant_001",
                    window_key="mandate",
                    committed_paise=999000,
                    reserved_paise=0,
                    remaining_paise=1000,
                    committed_count=5,
                    reserved_count=0,
                    remaining_count=5,
                ),
            )
        }
    )
    exec_port = RecordingExecutionPort()
    service = PersistentEscalationService(
        clock=clock,
        mandate_reader=mandate_reader,
        usage_reader=usage_reader,
    )
    tx = DummyTx()

    act = _make_action(request_id="req_budg_001", amount_paise=480000)
    esc = await service.create(act, _make_assessment(), tx)
    await service.decide(esc.escalation_id, EscalationDecision.APPROVE, "cust_001", tx)

    # Budget exhausted: handoff must fail
    with pytest.raises(ValueError, match=ReasonCode.E_BANK_INSUFFICIENT_FUNDS.value):
        await service.execute_approved(esc.escalation_id, tx, exec_port)

    # Proves no bank call occurred
    assert len(exec_port.request_ids) == 0


@pytest.mark.asyncio
async def test_duplicate_execution_returns_stored_outcome(clock: FrozenClock) -> None:
    exec_port = RecordingExecutionPort()
    service = PersistentEscalationService(clock=clock)
    tx = DummyTx()

    act = _make_action(request_id="req_dup_001")
    esc = await service.create(act, _make_assessment(), tx)
    await service.decide(esc.escalation_id, EscalationDecision.APPROVE, "cust_001", tx)

    # First execution succeeds
    res1 = await service.execute_approved(esc.escalation_id, tx, exec_port)
    assert len(exec_port.request_ids) == 1
    assert res1.status == ActionStatus.EXECUTED

    # Second execution returns stored result without calling executor again
    res2 = await service.execute_approved(esc.escalation_id, tx, exec_port)
    assert len(exec_port.request_ids) == 1  # Still 1
    assert res2.status == ActionStatus.EXECUTED
    assert res2.request_id == res1.request_id


@pytest.mark.asyncio
async def test_crash_recovery_resumes_once(clock: FrozenClock) -> None:
    exec_port = RecordingExecutionPort()
    service = PersistentEscalationService(clock=clock)
    tx = DummyTx()

    act = _make_action(request_id="req_crash_001")
    esc = await service.create(act, _make_assessment(), tx)
    await service.decide(esc.escalation_id, EscalationDecision.APPROVE, "cust_001", tx)

    # State is currently APPROVED but undispatched (simulating crash before dispatch)
    owned = await service.get_owned(esc.escalation_id, "cust_001")
    assert owned.state == EscalationState.APPROVED

    # Startup recovery sweeps and resumes undispatched records
    recovered = await service.recover_undispatched(exec_port)
    assert len(recovered) == 1
    assert recovered[0].request_id == "req_crash_001"
    assert len(exec_port.request_ids) == 1

    # Second startup recovery run finds nothing to execute
    recovered_again = await service.recover_undispatched(exec_port)
    assert len(recovered_again) == 0
    assert len(exec_port.request_ids) == 1  # Unchanged


@pytest.mark.asyncio
async def test_bank_outcome_mapping(clock: FrozenClock) -> None:
    # 1. Bank EXECUTED -> state EXECUTED
    exec_port_ok = RecordingExecutionPort(default_status=ActionStatus.EXECUTED)
    service1 = PersistentEscalationService(clock=clock)
    tx = DummyTx()
    esc1 = await service1.create(_make_action("req_m1"), _make_assessment(), tx)
    await service1.decide(esc1.escalation_id, EscalationDecision.APPROVE, "cust_001", tx)
    await service1.execute_approved(esc1.escalation_id, tx, exec_port_ok)
    res_esc1 = await service1.get_owned(esc1.escalation_id, "cust_001")
    assert res_esc1.state == EscalationState.EXECUTED

    # 2. Bank REJECTED -> state FAILED
    exec_port_rej = RecordingExecutionPort(
        default_status=ActionStatus.REJECTED,
        scripted_bank_statuses={"req_m2": BankStatus.REJECTED},
    )
    service2 = PersistentEscalationService(clock=clock)
    esc2 = await service2.create(_make_action("req_m2"), _make_assessment(), tx)
    await service2.decide(esc2.escalation_id, EscalationDecision.APPROVE, "cust_001", tx)
    await service2.execute_approved(esc2.escalation_id, tx, exec_port_rej)
    res_esc2 = await service2.get_owned(esc2.escalation_id, "cust_001")
    assert res_esc2.state == EscalationState.FAILED

    # 3. Bank EXECUTION_UNKNOWN -> state retains unknown outcome, not marked rejected
    exec_port_unk = RecordingExecutionPort(
        default_status=ActionStatus.EXECUTION_UNKNOWN,
        scripted_bank_statuses={"req_m3": BankStatus.UNKNOWN},
    )
    service3 = PersistentEscalationService(clock=clock)
    esc3 = await service3.create(_make_action("req_m3"), _make_assessment(), tx)
    await service3.decide(esc3.escalation_id, EscalationDecision.APPROVE, "cust_001", tx)
    await service3.execute_approved(esc3.escalation_id, tx, exec_port_unk)
    rec3 = await service3.get_owned(esc3.escalation_id, "cust_001")
    assert rec3.state != EscalationState.REJECTED
    assert rec3.result is not None
    assert rec3.result.status == ActionStatus.EXECUTION_UNKNOWN


def test_real_stack_handoff_skipped_with_reason() -> None:
    pytest.skip(
        "Real-stack handoff integration test skipped: Member A real execution "
        "and bank client modules have not landed yet."
    )
