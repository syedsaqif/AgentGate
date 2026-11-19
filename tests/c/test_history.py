"""Unit tests for Member C history tracking, baselines, and feature inputs."""

from __future__ import annotations

from datetime import UTC, datetime

from contracts.common import ActionType
from contracts.core_models import ActionParams, CanonicalAction, Intent
from gateway.risk.features import (
    count_recent_attempts,
    count_recent_denials,
    is_new_payee,
)
from gateway.risk.history import (
    RiskHistoryStore,
    calculate_mad,
    calculate_median,
    calculate_robust_z_score,
)
from gateway.risk.schema import register_risk_tables
from simulator.history_seed import seed_synthetic_history
from simulator.personas import CUSTOMER_ID_PRIMARY


def test_schema_table_registration() -> None:
    tables = register_risk_tables()
    assert "risk_attempts" in tables
    assert "risk_baselines" in tables


def test_seed_reproducibility_with_fixed_seed() -> None:
    store1 = RiskHistoryStore()
    store2 = RiskHistoryStore()

    seed_synthetic_history(store1, seed=42)
    seed_synthetic_history(store2, seed=42)

    base1 = store1.get_or_create_baseline(CUSTOMER_ID_PRIMARY, "agt_billpal")
    base2 = store2.get_or_create_baseline(CUSTOMER_ID_PRIMARY, "agt_billpal")

    assert base1.sample_count == 16
    assert base1.sample_count == base2.sample_count
    assert base1.median_amount_paise == base2.median_amount_paise
    assert base1.mad_amount_paise == base2.mad_amount_paise
    assert base1.amounts_paise == base2.amounts_paise
    assert base1.known_payees == base2.known_payees

    # ₹1,850 is close to median
    assert 180_000 <= base1.median_amount_paise <= 190_000


def test_current_attempt_counted_exactly_once() -> None:
    store = RiskHistoryStore()
    now = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)

    # First attempt
    store.record_attempt(
        attempt_id="att_idemp_001",
        request_id="req_001",
        customer_id=CUSTOMER_ID_PRIMARY,
        agent_id="agt_billpal",
        mandate_id="man_001",
        amount_paise=185000,
        payee_id="BILL-ELEC",
        verdict="ALLOW",
        decision="ALLOW",
        attempted_at=now,
    )

    # Duplicate call with same attempt_id (idempotency check)
    store.record_attempt(
        attempt_id="att_idemp_001",
        request_id="req_001",
        customer_id=CUSTOMER_ID_PRIMARY,
        agent_id="agt_billpal",
        mandate_id="man_001",
        amount_paise=185000,
        payee_id="BILL-ELEC",
        verdict="ALLOW",
        decision="ALLOW",
        attempted_at=now,
    )

    assert len(store.attempts) == 1
    snap = store.get_history_snapshot(
        customer_id=CUSTOMER_ID_PRIMARY, agent_id="agt_billpal", now=now
    )
    assert len(snap.recent_attempt_times) == 1


def test_denials_and_uncertain_outcomes_excluded_from_baseline() -> None:
    store = RiskHistoryStore()
    now = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)

    # Record a policy denial attempt
    store.record_attempt(
        attempt_id="att_denial_001",
        request_id="req_deny_001",
        customer_id=CUSTOMER_ID_PRIMARY,
        agent_id="agt_billpal",
        mandate_id="man_001",
        amount_paise=700000,
        payee_id="BILL-ELEC",
        verdict="DENY",
        decision="DENY",
        attempted_at=now,
    )

    # Record failed / uncertain bank outcomes
    store.record_outcome(
        request_id="req_deny_001",
        customer_id=CUSTOMER_ID_PRIMARY,
        agent_id="agt_billpal",
        mandate_id="man_001",
        payee_id="BILL-ELEC",
        amount_paise=700000,
        status="REJECTED",
        executed_at=now,
    )
    store.record_outcome(
        request_id="req_unknown_001",
        customer_id=CUSTOMER_ID_PRIMARY,
        agent_id="agt_billpal",
        mandate_id="man_001",
        payee_id="BILL-ELEC",
        amount_paise=500000,
        status="EXECUTION_UNKNOWN",
        executed_at=now,
    )

    baseline = store.get_or_create_baseline(CUSTOMER_ID_PRIMARY, "agt_billpal")
    # Baseline must remain empty of these failed/denied amounts
    assert baseline.sample_count == 0
    assert baseline.amounts_paise == []

    # But recent attempt counters must reflect the denial
    snap = store.get_history_snapshot(
        customer_id=CUSTOMER_ID_PRIMARY, agent_id="agt_billpal", now=now
    )
    assert len(snap.recent_attempt_times) == 1
    assert len(snap.recent_denial_times) == 1
    assert snap.successful_amounts_paise == ()


def test_auth_failed_traffic_excluded_from_verified_history() -> None:
    store = RiskHistoryStore()
    now = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)

    # Record an unauthenticated / auth-failed attempt
    store.record_attempt(
        attempt_id="att_auth_fail_001",
        request_id="req_auth_fail_001",
        customer_id=CUSTOMER_ID_PRIMARY,
        agent_id="agt_billpal",
        mandate_id=None,
        amount_paise=999999,
        payee_id="PAYEE-CRYPTO",
        verdict="AUTH_FAILED",
        decision="DENY",
        attempted_at=now,
        is_auth_failure=True,
    )

    snap = store.get_history_snapshot(
        customer_id=CUSTOMER_ID_PRIMARY, agent_id="agt_billpal", now=now
    )
    # Must NOT appear in verified customer/agent attempt history
    assert len(snap.recent_attempt_times) == 0
    assert len(snap.recent_denial_times) == 0

    baseline = store.get_or_create_baseline(CUSTOMER_ID_PRIMARY, "agt_billpal")
    assert baseline.sample_count == 0


def test_zero_history_and_mad_zero_defined_behavior() -> None:
    # 1. Zero history (< 10 samples warm-up)
    amounts = [185000, 184000, 186000]
    med = calculate_median(amounts)
    mad = calculate_mad(amounts, med)
    assert calculate_robust_z_score(185000, med, mad, sample_count=3) == 0.0

    # 2. MAD == 0 (all historical observations identical)
    identical_amounts = [185000] * 12
    med_identical = calculate_median(identical_amounts)
    mad_identical = calculate_mad(identical_amounts, med_identical)
    assert mad_identical == 0

    # Identical amount should have 0 z-score
    z_same = calculate_robust_z_score(185000, med_identical, mad_identical, sample_count=12)
    assert z_same == 0.0

    # Outlier amount with MAD=0 must not raise ZeroDivisionError and should return a high deviation
    z_diff = calculate_robust_z_score(480000, med_identical, mad_identical, sample_count=12)
    assert z_diff > 3.5


def test_features_and_risk_input_builder() -> None:
    now = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)
    history = RiskHistoryStore()
    seed_synthetic_history(history, now=now)

    snap = history.get_history_snapshot(
        customer_id=CUSTOMER_ID_PRIMARY, agent_id="agt_billpal", now=now
    )

    known_action = CanonicalAction(
        request_id="req_k1",
        customer_id=CUSTOMER_ID_PRIMARY,
        agent_id="agt_billpal",
        mandate_id="man_01",
        mandate_hash="0" * 64,
        action_type=ActionType.BILL_PAY,
        params=ActionParams(from_account="ACC-001", payee_id="BILL-ELEC", amount_paise=185000),
        intent=Intent(task="bill", category="utilities"),
        idempotency_key="id_k1",
        received_at=now,
    )
    assert is_new_payee(known_action, snap) is False

    unknown_action = known_action.model_copy(
        update={"params": known_action.params.model_copy(update={"payee_id": "PAYEE-X"})}
    )
    assert is_new_payee(unknown_action, snap) is True

    # Test count_recent_attempts and count_recent_denials
    assert count_recent_attempts(snap, window_seconds=600, now=now) >= 0
    assert count_recent_denials(snap, window_seconds=600, now=now) == 0
