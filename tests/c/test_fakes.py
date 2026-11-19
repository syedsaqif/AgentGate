"""Unit tests verifying test doubles and their negative branches for Member C."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from contracts.audit_models import AuditEvent
from contracts.common import ActionType, FinalDecision, HumanRole, RiskLevel
from contracts.core_models import (
    ActionParams,
    ActionStatus,
    BankStatus,
    CanonicalAction,
    Intent,
)
from contracts.escalation_models import EscalationDecision, EscalationState
from contracts.reason_codes import ReasonCode
from contracts.risk_models import RiskInput, RiskResult
from simulator.client_port import FixtureScenarioClient
from tests.support.c.providers import (
    DummyTx,
    FixtureActionReader,
    FixtureHumanAuth,
    FixtureRiskEngine,
    MemoryEscalationStore,
    RecordingAuditLog,
    RecordingExecutionPort,
    TrustedRegistry,
)
from tests.support.platform import FrozenClock


def _make_action(request_id: str = "req_test_001") -> CanonicalAction:
    return CanonicalAction(
        request_id=request_id,
        customer_id="cust_001",
        agent_id="agt_billpal",
        mandate_id="man_001",
        mandate_hash="0" * 64,
        action_type=ActionType.BILL_PAY,
        params=ActionParams(
            from_account="ACC-001",
            payee_id="BILL-ELEC",
            amount_paise=185000,
            currency="INR",
        ),
        intent=Intent(task="electricity", category="utilities"),
        idempotency_key=f"idem_{request_id}",
        received_at=datetime.now(UTC),
    )


def _make_risk_result() -> RiskResult:
    return RiskResult(
        score=10,
        level=RiskLevel.LOW,
        recommended=FinalDecision.ALLOW,
        signals=(),
        breaker_recommended=False,
        breaker_reasons=(),
        model_version="1.0.0",
    )


@pytest.mark.asyncio
async def test_risk_engine_fake_and_error_mode() -> None:
    engine = FixtureRiskEngine(result=_make_risk_result())
    dummy_input = RiskInput.model_construct()

    # Normal mode
    res = engine.score(dummy_input)
    assert res.score == 10
    assert len(engine.scores_called) == 1

    # Error mode raises exception
    engine.error_mode = True
    with pytest.raises(RuntimeError, match="error mode"):
        engine.score(dummy_input)


@pytest.mark.asyncio
async def test_execution_port_scripted_failures_and_recording() -> None:
    port = RecordingExecutionPort(
        scripted_results={
            "req_exec": ActionStatus.EXECUTED,
            "req_rej": ActionStatus.REJECTED,
            "req_unknown": ActionStatus.EXECUTION_UNKNOWN,
            "req_fail": ActionStatus.FAILED,
        }
    )

    res_exec = await port.execute_stored("req_exec", "esc_01")
    assert res_exec.status == ActionStatus.EXECUTED
    assert res_exec.bank_result is not None
    assert res_exec.bank_result.status == BankStatus.EXECUTED

    res_rej = await port.execute_stored("req_rej", "esc_02")
    assert res_rej.status == ActionStatus.REJECTED
    assert res_rej.bank_result is not None
    assert res_rej.bank_result.status == BankStatus.REJECTED

    res_unk = await port.execute_stored("req_unknown", "esc_03")
    assert res_unk.status == ActionStatus.EXECUTION_UNKNOWN
    assert res_unk.bank_result is not None
    assert res_unk.bank_result.status == BankStatus.UNKNOWN

    res_fail = await port.execute_stored("req_fail", "esc_04")
    assert res_fail.status == ActionStatus.FAILED

    assert port.request_ids == ["req_exec", "req_rej", "req_unknown", "req_fail"]
    assert port.escalation_ids == ["esc_01", "esc_02", "esc_03", "esc_04"]


@pytest.mark.asyncio
async def test_human_auth_fake_identities_and_pin() -> None:
    auth = FixtureHumanAuth()

    # Valid customer authentication
    cust_ctx = await auth.authenticate("tok_customer_001", HumanRole.CUSTOMER)
    assert cust_ctx.subject_id == "cust_001"
    assert cust_ctx.role == HumanRole.CUSTOMER

    # Valid ops authentication
    ops_ctx = await auth.authenticate("tok_ops_001", HumanRole.OPS)
    assert ops_ctx.subject_id == "ops_admin"
    assert ops_ctx.role == HumanRole.OPS

    # Expired token negative case
    with pytest.raises(ValueError, match=ReasonCode.E_AUTH_EXPIRED):
        await auth.authenticate("tok_expired_001", HumanRole.CUSTOMER)

    # Invalid token negative case
    with pytest.raises(ValueError, match=ReasonCode.E_AUTH_INVALID_TOKEN):
        await auth.authenticate("tok_unknown", HumanRole.CUSTOMER)

    # Role mismatch negative case
    with pytest.raises(ValueError, match=ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH):
        await auth.authenticate("tok_customer_001", HumanRole.OPS)

    # PIN success and failure
    await auth.verify_pin("cust_001", "4821")
    with pytest.raises(ValueError, match=ReasonCode.E_ESC_BAD_PIN):
        await auth.verify_pin("cust_001", "9999")
    with pytest.raises(ValueError, match=ReasonCode.E_ESC_BAD_PIN):
        await auth.verify_pin("unknown_customer", "4821")


@pytest.mark.asyncio
async def test_audit_log_fake_and_unavailable_failure() -> None:
    audit = RecordingAuditLog()
    dummy_event = AuditEvent.model_construct(event_type="test")

    # Normal append
    ref = await audit.append(dummy_event)
    assert ref.seq == 1
    assert len(audit.events) == 1

    # Injected failure switch
    audit.fail_on_append = True
    with pytest.raises(ValueError, match=ReasonCode.E_AUDIT_WRITE_FAILED):
        await audit.append(dummy_event)


@pytest.mark.asyncio
async def test_escalation_store_lifecycle_and_conflict() -> None:
    store = MemoryEscalationStore()
    tx = DummyTx()
    action = _make_action("req_esc_001")
    risk_res = _make_risk_result()

    esc = await store.create(action, risk_res, tx)
    assert esc.state == EscalationState.PENDING

    # Negative case: Cannot claim unapproved escalation
    with pytest.raises(ValueError, match="Cannot claim execution"):
        await store.claim_execution(esc.escalation_id, esc.action_hash, tx)

    # Approve
    approved = await store.decide(esc.escalation_id, EscalationDecision.APPROVE, "cust_001", tx)
    assert approved.state == EscalationState.APPROVED

    # Negative case: Double decide (conflict)
    with pytest.raises(ValueError, match=ReasonCode.E_ESC_ALREADY_CONSUMED):
        await store.decide(esc.escalation_id, EscalationDecision.APPROVE, "cust_001", tx)

    # Negative case: Claim with wrong hash
    with pytest.raises(ValueError, match=ReasonCode.E_ESC_HASH_MISMATCH):
        await store.claim_execution(esc.escalation_id, "wrong_hash", tx)

    # Consume
    consumed = await store.claim_execution(esc.escalation_id, esc.action_hash, tx)
    assert consumed.state == EscalationState.EXECUTED

    # Negative case: Duplicate consume (conflict)
    with pytest.raises(ValueError, match=ReasonCode.E_ESC_ALREADY_CONSUMED):
        await store.claim_execution(esc.escalation_id, esc.action_hash, tx)


@pytest.mark.asyncio
async def test_escalation_store_expiry_with_frozen_clock() -> None:
    clock = FrozenClock(datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC))
    store = MemoryEscalationStore()
    tx = DummyTx()
    action = _make_action("req_exp_001")
    risk_res = _make_risk_result()

    # Create escalation with 60s TTL
    esc = await store.create(action, risk_res, tx, ttl_seconds=60, now=clock.now())
    assert esc.state == EscalationState.PENDING

    # Advance clock past TTL
    clock.advance(seconds=61)
    expired_count = await store.expire_due(clock.now())
    assert expired_count == 1

    # Verify state is EXPIRED and cannot be approved or claimed
    esc_expired = await store.get_owned(esc.escalation_id, "cust_001")
    assert esc_expired.state == EscalationState.EXPIRED

    with pytest.raises(ValueError, match=ReasonCode.E_ESC_EXPIRED):
        await store.decide(esc.escalation_id, EscalationDecision.APPROVE, "cust_001", tx)

    with pytest.raises(ValueError, match=ReasonCode.E_ESC_EXPIRED):
        await store.claim_execution(esc.escalation_id, esc.action_hash, tx)


@pytest.mark.asyncio
async def test_escalation_store_revocation_cancellation() -> None:
    store = MemoryEscalationStore()
    tx = DummyTx()
    action = _make_action("req_can_001")
    risk_res = _make_risk_result()

    esc = await store.create(action, risk_res, tx)
    assert esc.state == EscalationState.PENDING

    # Revoke mandate
    await store.cancel_for_mandate(action.mandate_id, tx)
    esc_cancelled = await store.get_owned(esc.escalation_id, "cust_001")
    assert esc_cancelled.state == EscalationState.CANCELLED

    # Negative case: Cannot decide cancelled escalation
    with pytest.raises(ValueError, match=ReasonCode.E_ESC_CANCELLED):
        await store.decide(esc.escalation_id, EscalationDecision.APPROVE, "cust_001", tx)

    # Negative case: Cannot claim cancelled escalation
    with pytest.raises(ValueError, match=ReasonCode.E_ESC_CANCELLED):
        await store.claim_execution(esc.escalation_id, esc.action_hash, tx)


@pytest.mark.asyncio
async def test_action_reader_and_trusted_registry() -> None:
    reg = TrustedRegistry.default()
    b_elec = reg.get_payee("BILL-ELEC")
    assert b_elec is not None
    assert b_elec.verified is True

    p_x = reg.get_payee("PAYEE-X")
    assert p_x is not None
    assert p_x.verified is False

    assert reg.get_payee("UNKNOWN") is None

    acc = reg.get_account("ACC-001")
    assert acc is not None
    assert acc.masked_label == "**1234"
    assert reg.get_account("UNKNOWN") is None

    reader = FixtureActionReader()
    action = _make_action("req_read_001")
    reader.actions[action.request_id] = action

    fetched = await reader.get(action.request_id)
    assert fetched.request_id == action.request_id

    with pytest.raises(KeyError):
        await reader.get("req_nonexistent")


@pytest.mark.asyncio
async def test_scenario_client_fake() -> None:
    client = FixtureScenarioClient()

    # Request token
    tok = await client.request_token("agt_billpal", "man_001")
    assert "access_token" in tok

    # Send action
    action = _make_action("req_sim_001")
    act_res = await client.send_action(action)
    assert act_res["status"] == "EXECUTED"

    # Customer login
    login_res = await client.customer_login("cust_001", "4821")
    assert "session_token" in login_res

    # Customer login with bad PIN
    bad_login = await client.customer_login("cust_001", "0000")
    assert "error" in bad_login
    assert bad_login["error"]["code"] == "E_ESC_BAD_PIN"

    # Decide escalation
    dec_res = await client.decide_escalation(
        login_res["session_token"], "esc_001", "APPROVE", pin="4821"
    )
    assert dec_res["state"] == "APPROVED"

    # Decide escalation with bad PIN
    bad_dec = await client.decide_escalation(
        login_res["session_token"], "esc_001", "APPROVE", pin="0000"
    )
    assert "error" in bad_dec
