"""Unit tests for Member C circuit breaker recommendation and lifecycle execution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from contracts.common import ActionType, FinalDecision, PolicyVerdict, RiskLevel
from contracts.core_models import (
    ActionParams,
    CanonicalAction,
    Grant,
    Intent,
    MandateSnapshot,
    MandateStatus,
    MandateTerms,
    PayeeCard,
    PolicyResult,
)
from contracts.identity_models import AuthContext
from contracts.reason_codes import ReasonCode
from contracts.risk_models import HistorySnapshot, RiskInput, RiskResult, RiskSignal
from gateway.risk.breaker import (
    apply_breaker_recommendation,
    evaluate_circuit_breaker,
    execute_breaker_trip,
)
from tests.support.c.providers import (
    DummyTx,
    FixtureLifecycle,
    MemoryEscalationStore,
    RecordingAuditLog,
    TrustedRegistry,
)


def _make_breaker_input(
    *,
    amount_paise: int = 185000,
    payee_id: str = "BILL-ELEC",
    intent_category: str = "utilities",
    grant_category: str = "utilities",
    denial_times: tuple[datetime, ...] = (),
    now: datetime | None = None,
) -> RiskInput:
    clock_now = now or datetime(2026, 11, 19, 11, 0, 0, tzinfo=UTC)

    action = CanonicalAction(
        request_id="req_brk_001",
        customer_id="cust_001",
        agent_id="agt_billpal",
        mandate_id="man_brk_001",
        mandate_hash="0" * 64,
        action_type=ActionType.BILL_PAY,
        params=ActionParams(
            from_account="ACC-001",
            payee_id=payee_id,
            amount_paise=amount_paise,
            currency="INR",
        ),
        intent=Intent(task="payment", category=intent_category),
        idempotency_key="idem_brk_001",
        received_at=clock_now,
    )

    grant = Grant(
        grant_id="grant_01",
        action=ActionType.BILL_PAY,
        account_ids=("ACC-001",),
        payee_ids=(payee_id,),
        max_per_txn_paise=500000,
        category=grant_category,
    )

    terms = MandateTerms(
        mandate_id="man_brk_001",
        version=1,
        customer_id="cust_001",
        agent_id="agt_billpal",
        valid_from=clock_now - timedelta(days=10),
        valid_until=clock_now + timedelta(days=30),
        grants=(grant,),
        risk_profile="balanced",
    )

    mandate = MandateSnapshot(
        terms=terms,
        status=MandateStatus.ACTIVE,
        mandate_hash="0" * 64,
        sig="sig_test",
        key_id="key_test",
    )

    auth = AuthContext(
        customer_id="cust_001",
        agent_id="agt_billpal",
        mandate_id="man_brk_001",
        mandate_hash="0" * 64,
        jkt="jkt_billpal",
        token_jti="tok_jti",
        proof_jti="prf_jti",
    )

    policy = PolicyResult(verdict=PolicyVerdict.ALLOW, reason_codes=())

    hist = HistorySnapshot(
        successful_amounts_paise=(185000,) * 12,
        recent_attempt_times=(),
        recent_denial_times=denial_times,
        previously_paid_payees=("BILL-ELEC", "BILL-MOBILE"),
    )

    return RiskInput(
        auth=auth,
        action=action,
        policy=policy,
        mandate=mandate,
        history=hist,
        now=clock_now,
        attempt_id="att_brk_001",
    )


def test_trigger_1_score_in_high_band() -> None:
    risk_in = _make_breaker_input()
    res = RiskResult(
        score=85,
        level=RiskLevel.HIGH,
        recommended=FinalDecision.DENY,
        signals=(),
        breaker_recommended=False,
        breaker_reasons=(),
        model_version="1.0.0",
    )

    rec, reasons = evaluate_circuit_breaker(risk_in, res)
    assert rec is True
    assert any("HIGH deny band" in r for r in reasons)

    enriched = apply_breaker_recommendation(risk_in, res)
    assert enriched.breaker_recommended is True
    assert len(enriched.breaker_reasons) > 0


def test_trigger_2_five_denials_in_ten_minutes() -> None:
    now = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)
    denials = tuple(now - timedelta(seconds=i * 60) for i in range(5))
    risk_in = _make_breaker_input(denial_times=denials, now=now)

    res = RiskResult(
        score=20,
        level=RiskLevel.LOW,
        recommended=FinalDecision.ALLOW,
        signals=(),
        breaker_recommended=False,
        breaker_reasons=(),
        model_version="1.0.0",
    )

    rec, reasons = evaluate_circuit_breaker(risk_in, res)
    assert rec is True
    assert any("Probing detected: 5 policy denials" in r for r in reasons)


def test_trigger_3_intent_mismatch_plus_unverified_payee() -> None:
    reg = TrustedRegistry()
    reg.payees["PAYEE-X"] = PayeeCard(
        payee_id="PAYEE-X",
        display_name="Unknown Payee X",
        category="personal",
        verified=False,
    )

    risk_in = _make_breaker_input(payee_id="PAYEE-X")
    res = RiskResult(
        score=50,
        level=RiskLevel.MEDIUM,
        recommended=FinalDecision.ESCALATE,
        signals=(
            RiskSignal(
                name="intent_mismatch",
                severity=75,
                weight=0.60,
                reason_code="SIG_INTENT_MISMATCH",
                message="Category mismatch",
            ),
        ),
        breaker_recommended=False,
        breaker_reasons=(),
        model_version="1.0.0",
    )

    rec, reasons = evaluate_circuit_breaker(risk_in, res, catalog=reg)
    assert rec is True
    assert any("Intent mismatch detected on payment to unverified payee" in r for r in reasons)


def test_breaker_remains_closed_for_normal_request() -> None:
    risk_in = _make_breaker_input(payee_id="BILL-ELEC")
    res = RiskResult(
        score=10,
        level=RiskLevel.LOW,
        recommended=FinalDecision.ALLOW,
        signals=(),
        breaker_recommended=False,
        breaker_reasons=(),
        model_version="1.0.0",
    )

    rec, reasons = evaluate_circuit_breaker(risk_in, res)
    assert rec is False
    assert len(reasons) == 0


@pytest.mark.asyncio
async def test_execute_breaker_trip_agent_scope() -> None:
    lifecycle = FixtureLifecycle()
    audit_log = RecordingAuditLog()
    store = MemoryEscalationStore()
    tx = DummyTx()

    risk_in = _make_breaker_input()
    res = RiskResult(
        score=85,
        level=RiskLevel.HIGH,
        recommended=FinalDecision.DENY,
        signals=(),
        breaker_recommended=True,
        breaker_reasons=("Risk score in HIGH band",),
        model_version="1.0.0",
    )

    # Pre-populate a pending escalation to verify cancellation
    await store.create(risk_in.action, res, tx)

    await execute_breaker_trip(
        risk_in,
        res,
        lifecycle=lifecycle,
        audit_log=audit_log,
        escalation_service=store,
        tx=tx,
        scope="agent",
    )

    # Assert lifecycle called with correct subject and reason
    assert len(lifecycle.suspended_agents) == 1
    suspended_agent, reasons = lifecycle.suspended_agents[0]
    assert suspended_agent == "agt_billpal"
    assert reasons == (ReasonCode.E_RISK_BREAKER_OPEN.value,)

    # Assert pending escalation was cancelled
    assert len(store.escalations) == 1
    esc = list(store.escalations.values())[0]
    assert esc.state.value == "CANCELLED"

    # Assert audit log received exactly one event
    assert len(audit_log.events) == 1
    event = audit_log.events[0]
    assert event.event_type == "risk.breaker_tripped"
    assert event.agent_id == "agt_billpal"
    assert event.customer_id == "cust_001"
    assert ReasonCode.E_RISK_BREAKER_OPEN.value in event.reason_codes


@pytest.mark.asyncio
async def test_execute_breaker_trip_customer_scope() -> None:
    lifecycle = FixtureLifecycle()
    audit_log = RecordingAuditLog()

    risk_in = _make_breaker_input()
    res = RiskResult(
        score=85,
        level=RiskLevel.HIGH,
        recommended=FinalDecision.DENY,
        signals=(),
        breaker_recommended=True,
        breaker_reasons=("Customer safety action",),
        model_version="1.0.0",
    )

    await execute_breaker_trip(
        risk_in,
        res,
        lifecycle=lifecycle,
        audit_log=audit_log,
        scope="customer",
    )

    # Assert customer-scoped delegation suspension called
    assert len(lifecycle.suspended_delegations) == 1
    cust, agt, reasons = lifecycle.suspended_delegations[0]
    assert cust == "cust_001"
    assert agt == "agt_billpal"
    assert reasons == (ReasonCode.E_RISK_BREAKER_OPEN.value,)
    assert len(lifecycle.suspended_agents) == 0

    # Audit event emitted exactly once
    assert len(audit_log.events) == 1
    assert audit_log.events[0].safe_payload["scope"] == "customer"
