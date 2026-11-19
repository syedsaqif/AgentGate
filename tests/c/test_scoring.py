"""Unit tests for Member C noisy-OR explainable risk scoring."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from contracts.common import ActionType, FinalDecision, PolicyVerdict, RiskLevel
from contracts.core_models import (
    ActionParams,
    CanonicalAction,
    Grant,
    Intent,
    MandateSnapshot,
    MandateStatus,
    MandateTerms,
    PolicyResult,
    StepUpRules,
    TimeWindow,
)
from contracts.identity_models import AuthContext
from contracts.risk_models import HistorySnapshot, RiskInput, RiskSignal
from gateway.risk.history import RiskHistoryStore
from gateway.risk.scoring import (
    MODEL_VERSION,
    combine_signals_noisy_or,
    score_risk,
)
from simulator.history_seed import seed_synthetic_history
from simulator.personas import (
    CUSTOMER_ID_PRIMARY,
    PAYEE_ELECTRICITY,
    PAYEE_MOBILE,
    PAYEE_UNVERIFIED_X,
)


def _make_risk_input(
    *,
    amount_paise: int = 185000,
    payee_id: str = "BILL-ELEC",
    intent_category: str = "utilities",
    grant_category: str = "utilities",
    max_per_txn_paise: int = 500000,
    risk_profile: str = "balanced",
    step_up_above_paise: int | None = None,
    history: HistorySnapshot | None = None,
    now: datetime | None = None,
    time_window: TimeWindow | None = None,
) -> RiskInput:
    clock_now = now or datetime(2026, 11, 19, 11, 0, 0, tzinfo=UTC)

    action = CanonicalAction(
        request_id="req_test_score",
        customer_id=CUSTOMER_ID_PRIMARY,
        agent_id="agt_billpal",
        mandate_id="man_score_001",
        mandate_hash="0" * 64,
        action_type=ActionType.BILL_PAY,
        params=ActionParams(
            from_account="ACC-001",
            payee_id=payee_id,
            amount_paise=amount_paise,
            currency="INR",
        ),
        intent=Intent(task="payment", category=intent_category),
        idempotency_key="idem_score_001",
        received_at=clock_now,
    )

    grant = Grant(
        grant_id="grant_01",
        action=ActionType.BILL_PAY,
        account_ids=("ACC-001",),
        payee_ids=(payee_id,),
        max_per_txn_paise=max_per_txn_paise,
        category=grant_category,
        time_window=time_window,
    )

    terms = MandateTerms(
        mandate_id="man_score_001",
        version=1,
        customer_id=CUSTOMER_ID_PRIMARY,
        agent_id="agt_billpal",
        valid_from=clock_now - timedelta(days=10),
        valid_until=clock_now + timedelta(days=30),
        grants=(grant,),
        step_up=StepUpRules(amount_above_paise=step_up_above_paise),
        risk_profile=risk_profile,  # type: ignore[arg-type]
    )

    mandate = MandateSnapshot(
        terms=terms,
        status=MandateStatus.ACTIVE,
        mandate_hash="0" * 64,
        sig="sig_test",
        key_id="key_test",
    )

    auth = AuthContext(
        customer_id=CUSTOMER_ID_PRIMARY,
        agent_id="agt_billpal",
        mandate_id="man_score_001",
        mandate_hash="0" * 64,
        jkt="jkt_billpal",
        token_jti="tok_jti",
        proof_jti="prf_jti",
    )

    policy = PolicyResult(verdict=PolicyVerdict.ALLOW, reason_codes=())

    hist = history or HistorySnapshot(
        successful_amounts_paise=(185000,) * 15,
        recent_attempt_times=(),
        recent_denial_times=(),
        previously_paid_payees=(PAYEE_ELECTRICITY.payee_id, PAYEE_MOBILE.payee_id),
    )

    return RiskInput(
        auth=auth,
        action=action,
        policy=policy,
        mandate=mandate,
        history=hist,
        now=clock_now,
        attempt_id="att_test_score",
    )


def test_ordinary_bill_with_seeded_history_is_low() -> None:
    now = datetime(2026, 11, 19, 11, 0, 0, tzinfo=UTC)
    store = RiskHistoryStore()
    seed_synthetic_history(store, now=now)
    snap = store.get_history_snapshot(
        customer_id=CUSTOMER_ID_PRIMARY, agent_id="agt_billpal", now=now
    )

    risk_in = _make_risk_input(amount_paise=185000, payee_id="BILL-ELEC", history=snap, now=now)
    res = score_risk(risk_in)

    assert res.level == RiskLevel.LOW
    assert res.recommended == FinalDecision.ALLOW
    assert res.score < 35
    assert res.breaker_recommended is False
    assert res.model_version == MODEL_VERSION


def test_4800_example_is_medium_step_up_not_high() -> None:
    now = datetime(2026, 11, 19, 11, 0, 0, tzinfo=UTC)
    store = RiskHistoryStore()
    seed_synthetic_history(store, now=now)
    snap = store.get_history_snapshot(
        customer_id=CUSTOMER_ID_PRIMARY, agent_id="agt_billpal", now=now
    )

    # ₹4,800 is 480,000 paise (outlier against median ₹1,850 and max ~₹1,920)
    risk_in = _make_risk_input(amount_paise=480000, payee_id="BILL-ELEC", history=snap, now=now)
    res = score_risk(risk_in)

    assert res.level == RiskLevel.MEDIUM
    assert res.recommended == FinalDecision.ESCALATE
    assert 35 <= res.score < 70
    assert any(s.name == "amount_outlier" for s in res.signals)
    assert res.breaker_recommended is False


def test_monotonicity_property() -> None:
    # Adding risk signals never lowers the combined noisy-OR score
    s1 = RiskSignal(name="s1", severity=50, weight=0.4, reason_code="R1", message="m1")
    s2 = RiskSignal(name="s2", severity=60, weight=0.5, reason_code="R2", message="m2")
    s3 = RiskSignal(name="s3", severity=70, weight=0.6, reason_code="R3", message="m3")

    score0 = combine_signals_noisy_or(())
    score1 = combine_signals_noisy_or((s1,))
    score2 = combine_signals_noisy_or((s1, s2))
    score3 = combine_signals_noisy_or((s1, s2, s3))

    assert 0 <= score0 <= score1 <= score2 <= score3 <= 100


def test_deterministic_output_for_same_input() -> None:
    risk_in = _make_risk_input(amount_paise=480000)
    res1 = score_risk(risk_in)
    res2 = score_risk(risk_in)

    assert res1.score == res2.score
    assert res1.level == res2.level
    assert res1.recommended == res2.recommended
    assert [s.name for s in res1.signals] == [s.name for s in res2.signals]


def test_signal_new_payee() -> None:
    risk_in = _make_risk_input(payee_id=PAYEE_UNVERIFIED_X.payee_id)
    res = score_risk(risk_in)
    assert any(s.name == "new_payee" for s in res.signals)


def test_signal_amount_outlier() -> None:
    risk_in = _make_risk_input(amount_paise=480000)
    res = score_risk(risk_in)
    assert any(s.name == "amount_outlier" for s in res.signals)


def test_signal_near_limit_structuring() -> None:
    # 95% of 500,000 max_per_txn is 475,000 paise
    risk_in = _make_risk_input(amount_paise=475000, max_per_txn_paise=500000)
    res = score_risk(risk_in)
    assert any(s.name == "near_limit_structuring" for s in res.signals)


def test_signal_velocity() -> None:
    now = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)
    # 6 attempts in past 30 seconds
    recent = tuple(now - timedelta(seconds=i * 5) for i in range(6))
    hist = HistorySnapshot(
        successful_amounts_paise=(185000,) * 15,
        recent_attempt_times=recent,
        recent_denial_times=(),
        previously_paid_payees=("BILL-ELEC",),
    )
    risk_in = _make_risk_input(history=hist, now=now)
    res = score_risk(risk_in)
    assert any(s.name == "velocity" for s in res.signals)


def test_signal_request_rate() -> None:
    now = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)
    # 12 attempts in past 50 seconds
    recent = tuple(now - timedelta(seconds=i * 4) for i in range(12))
    hist = HistorySnapshot(
        successful_amounts_paise=(185000,) * 15,
        recent_attempt_times=recent,
        recent_denial_times=(),
        previously_paid_payees=("BILL-ELEC",),
    )
    risk_in = _make_risk_input(history=hist, now=now)
    res = score_risk(risk_in)
    assert any(s.name == "request_rate" for s in res.signals)


def test_signal_off_pattern_time() -> None:
    now = datetime(2026, 11, 19, 23, 0, 0, tzinfo=UTC)  # 23:00 UTC
    window = TimeWindow(
        timezone="Asia/Kolkata",
        weekdays=(1, 2, 3, 4, 5),
        start_local="09:00",
        end_local="18:00",
    )
    risk_in = _make_risk_input(now=now, time_window=window)
    res = score_risk(risk_in)
    assert any(s.name == "off_pattern_time" for s in res.signals)


def test_signal_sequence_anomaly() -> None:
    now = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)
    denials = (now - timedelta(seconds=10),)
    attempts = tuple(now - timedelta(seconds=i * 5) for i in range(4))
    hist = HistorySnapshot(
        successful_amounts_paise=(185000,) * 15,
        recent_attempt_times=attempts,
        recent_denial_times=denials,
        previously_paid_payees=("BILL-ELEC",),
    )
    risk_in = _make_risk_input(history=hist, now=now)
    res = score_risk(risk_in)
    assert any(s.name == "sequence_anomaly" for s in res.signals)


def test_signal_probing() -> None:
    now = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)
    denials = tuple(now - timedelta(seconds=i * 60) for i in range(4))
    hist = HistorySnapshot(
        successful_amounts_paise=(185000,) * 15,
        recent_attempt_times=denials,
        recent_denial_times=denials,
        previously_paid_payees=("BILL-ELEC",),
    )
    risk_in = _make_risk_input(history=hist, now=now)
    res = score_risk(risk_in)
    assert any(s.name == "probing" for s in res.signals)
    assert res.level == RiskLevel.HIGH
    assert res.recommended == FinalDecision.DENY
    assert res.breaker_recommended is True


def test_signal_intent_mismatch() -> None:
    risk_in = _make_risk_input(intent_category="gambling", grant_category="utilities")
    res = score_risk(risk_in)
    assert any(s.name == "intent_mismatch" for s in res.signals)


def test_signal_read_enumeration() -> None:
    now = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)
    # 4 attempts in past 60s
    attempts = tuple(now - timedelta(seconds=i * 15) for i in range(4))
    hist = HistorySnapshot(
        successful_amounts_paise=(185000,) * 15,
        recent_attempt_times=attempts,
        recent_denial_times=(),
        previously_paid_payees=("BILL-ELEC",),
    )
    # Amount 450,000 is 90% of 500,000 max_per_txn
    risk_in = _make_risk_input(
        amount_paise=450000, max_per_txn_paise=500000, history=hist, now=now
    )
    res = score_risk(risk_in)
    assert any(s.name == "read_enumeration" for s in res.signals)


def test_profile_bands_conservative_balanced_permissive() -> None:
    from gateway.risk.scoring import determine_risk_level_and_decision

    # Score 30 in Conservative (25/60): MEDIUM / ESCALATE
    lvl_c, dec_c = determine_risk_level_and_decision(30, "conservative")
    assert lvl_c == RiskLevel.MEDIUM
    assert dec_c == FinalDecision.ESCALATE

    # Score 30 in Balanced (35/70): LOW / ALLOW
    lvl_b, dec_b = determine_risk_level_and_decision(30, "balanced")
    assert lvl_b == RiskLevel.LOW
    assert dec_b == FinalDecision.ALLOW

    # Score 40 in Permissive (45/80): LOW / ALLOW
    lvl_p, dec_p = determine_risk_level_and_decision(40, "permissive")
    assert lvl_p == RiskLevel.LOW
    assert dec_p == FinalDecision.ALLOW
