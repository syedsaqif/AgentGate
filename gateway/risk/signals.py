"""Member C deterministic signal catalog implementing the 10 Appendix D signals."""

from __future__ import annotations

from contracts.core_models import ActionType
from contracts.risk_models import RiskInput, RiskSignal


def evaluate_new_payee(risk_input: RiskInput) -> RiskSignal | None:
    """Detect first payment by this agent to this payee."""
    payee_id = risk_input.action.params.payee_id
    if not payee_id:
        return None

    if payee_id not in risk_input.history.previously_paid_payees:
        return RiskSignal(
            name="new_payee",
            severity=50,
            weight=0.40,
            reason_code="SIG_NEW_PAYEE",
            message="First payment by this agent to this payee",
        )
    return None


def evaluate_amount_outlier(risk_input: RiskInput) -> RiskSignal | None:
    """Detect statistical or absolute amount outlier compared to history or mandate."""
    amount = risk_input.action.params.amount_paise
    amounts = risk_input.history.successful_amounts_paise

    if len(amounts) >= 10:
        sorted_vals = sorted(amounts)
        n = len(sorted_vals)
        median = (
            sorted_vals[n // 2]
            if n % 2 == 1
            else (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) // 2
        )
        deviations = sorted([abs(v - median) for v in amounts])
        mad = (
            deviations[n // 2]
            if n % 2 == 1
            else (deviations[n // 2 - 1] + deviations[n // 2]) // 2
        )

        max_hist = max(amounts)
        if mad > 0:
            z_score = 0.6745 * abs(amount - median) / float(mad)
        else:
            effective_scale = max(int(0.10 * median), 100)
            z_score = abs(amount - median) / float(effective_scale)

        if z_score > 3.5 or amount > 2 * max_hist:
            return RiskSignal(
                name="amount_outlier",
                severity=80,
                weight=0.625,
                reason_code="SIG_AMOUNT_OUTLIER",
                message=(
                    f"Amount ₹{amount / 100:.2f} is an outlier compared to "
                    f"historical median ₹{median / 100:.2f}"
                ),
            )
    else:
        # Warm-up phase: check mandate step-up threshold or historical max
        step_up = risk_input.mandate.terms.step_up.amount_above_paise
        if step_up is not None and amount > step_up:
            return RiskSignal(
                name="amount_outlier",
                severity=80,
                weight=0.625,
                reason_code="SIG_AMOUNT_OUTLIER",
                message=f"Amount ₹{amount / 100:.2f} exceeds confirmation threshold during warm-up",
            )
        if amounts and amount > 2 * max(amounts):
            return RiskSignal(
                name="amount_outlier",
                severity=80,
                weight=0.625,
                reason_code="SIG_AMOUNT_OUTLIER",
                message=f"Amount ₹{amount / 100:.2f} exceeds 2x historical maximum",
            )
    return None


def evaluate_near_limit_structuring(risk_input: RiskInput) -> RiskSignal | None:
    """Detect payment amount near the per-transaction cap (90% to 100%)."""
    amount = risk_input.action.params.amount_paise
    for grant in risk_input.mandate.terms.grants:
        if grant.max_per_txn_paise and grant.max_per_txn_paise > 0:
            ratio = amount / grant.max_per_txn_paise
            if 0.90 <= ratio <= 1.0:
                limit_fmt = f"₹{grant.max_per_txn_paise / 100:.2f}"
                return RiskSignal(
                    name="near_limit_structuring",
                    severity=60,
                    weight=0.40,
                    reason_code="SIG_STRUCTURING",
                    message=f"Amount ₹{amount / 100:.2f} is near the limit {limit_fmt}",
                )
    return None


def evaluate_velocity(risk_input: RiskInput) -> RiskSignal | None:
    """Detect high frequency of value-moving actions (> 5 in 60s)."""
    cutoff = risk_input.now.timestamp() - 60
    count = sum(1 for t in risk_input.history.recent_attempt_times if t.timestamp() >= cutoff)
    if count > 5:
        return RiskSignal(
            name="velocity",
            severity=70,
            weight=0.50,
            reason_code="SIG_VELOCITY",
            message=f"High velocity: {count} actions in under 60 seconds",
        )
    return None


def evaluate_request_rate(risk_input: RiskInput) -> RiskSignal | None:
    """Detect extreme overall request rate (> 10 requests in 60s)."""
    cutoff = risk_input.now.timestamp() - 60
    count = sum(1 for t in risk_input.history.recent_attempt_times if t.timestamp() >= cutoff)
    if count > 10:
        return RiskSignal(
            name="request_rate",
            severity=50,
            weight=0.40,
            reason_code="SIG_REQUEST_RATE",
            message=f"High request rate: {count} attempts in 60 seconds",
        )
    return None


def evaluate_off_pattern_time(risk_input: RiskInput) -> RiskSignal | None:
    """Detect actions outside granted operating hours."""
    for grant in risk_input.mandate.terms.grants:
        if grant.time_window is not None:
            now_dt = risk_input.now
            # Weekdays: 1=Monday .. 7=Sunday
            weekday = now_dt.isoweekday()
            if weekday not in grant.time_window.weekdays:
                return RiskSignal(
                    name="off_pattern_time",
                    severity=40,
                    weight=0.25,
                    reason_code="SIG_OFF_HOURS",
                    message=f"Action attempted on non-operating weekday {weekday}",
                )
            current_time_str = now_dt.strftime("%H:%M")
            start = grant.time_window.start_local
            end = grant.time_window.end_local
            if not (start <= current_time_str <= end):
                return RiskSignal(
                    name="off_pattern_time",
                    severity=40,
                    weight=0.25,
                    reason_code="SIG_OFF_HOURS",
                    message=f"Action attempted at {current_time_str} outside window {start}-{end}",
                )
    return None


def evaluate_sequence_anomaly(risk_input: RiskInput) -> RiskSignal | None:
    """Detect rapid repeated attempts immediately following policy denials."""
    cutoff = risk_input.now.timestamp() - 30
    recent_denials = sum(
        1 for t in risk_input.history.recent_denial_times if t.timestamp() >= cutoff
    )
    recent_attempts = sum(
        1 for t in risk_input.history.recent_attempt_times if t.timestamp() >= cutoff
    )
    if recent_denials >= 1 and recent_attempts >= 3:
        return RiskSignal(
            name="sequence_anomaly",
            severity=50,
            weight=0.30,
            reason_code="SIG_SEQUENCE_ANOMALY",
            message="Rapid repeated attempts immediately following policy denial",
        )
    return None


def evaluate_probing(risk_input: RiskInput) -> RiskSignal | None:
    """Detect probing via repeated policy denials (>= 3 in 5m or >= 5 in 10m)."""
    now_ts = risk_input.now.timestamp()
    denials_5m = sum(
        1 for t in risk_input.history.recent_denial_times if t.timestamp() >= now_ts - 300
    )
    denials_10m = sum(
        1 for t in risk_input.history.recent_denial_times if t.timestamp() >= now_ts - 600
    )
    if denials_5m >= 3 or denials_10m >= 5:
        return RiskSignal(
            name="probing",
            severity=95,
            weight=0.90,
            reason_code="SIG_PROBING",
            message=f"Probing detected: {denials_5m} policy denials in past 5 minutes",
        )
    return None


def evaluate_intent_mismatch(risk_input: RiskInput) -> RiskSignal | None:
    """Detect structured mismatch between declared category and mandate purpose."""
    declared = risk_input.action.intent.category
    for grant in risk_input.mandate.terms.grants:
        if grant.category and grant.category.lower() != declared.lower():
            return RiskSignal(
                name="intent_mismatch",
                severity=75,
                weight=0.60,
                reason_code="SIG_INTENT_MISMATCH",
                message=f"Category '{declared}' mismatches grant category '{grant.category}'",
            )
    return None


def evaluate_read_enumeration(risk_input: RiskInput) -> RiskSignal | None:
    """Detect recon-then-drain: multiple recent reads followed by large action."""
    action_type = risk_input.action.action_type
    if action_type in (ActionType.BILL_PAY, ActionType.TRANSFER):
        amount = risk_input.action.params.amount_paise
        for grant in risk_input.mandate.terms.grants:
            if grant.max_per_txn_paise and amount >= int(0.80 * grant.max_per_txn_paise):
                cutoff = risk_input.now.timestamp() - 120
                recent_attempts = sum(
                    1 for t in risk_input.history.recent_attempt_times if t.timestamp() >= cutoff
                )
                if recent_attempts >= 3:
                    return RiskSignal(
                        name="read_enumeration",
                        severity=60,
                        weight=0.35,
                        reason_code="SIG_RECON_DRAIN",
                        message="Recon then drain: rapid queries followed by high-value action",
                    )
    return None


ALL_SIGNAL_EVALUATORS = (
    evaluate_new_payee,
    evaluate_amount_outlier,
    evaluate_near_limit_structuring,
    evaluate_velocity,
    evaluate_request_rate,
    evaluate_off_pattern_time,
    evaluate_sequence_anomaly,
    evaluate_probing,
    evaluate_intent_mismatch,
    evaluate_read_enumeration,
)
