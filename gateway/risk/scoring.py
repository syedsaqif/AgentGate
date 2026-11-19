"""Member C pure synchronous noisy-OR risk scoring engine."""

from __future__ import annotations

from contracts.common import FinalDecision, RiskLevel, Tx
from contracts.risk_models import RiskInput, RiskResult, RiskSignal
from gateway.risk.signals import ALL_SIGNAL_EVALUATORS

MODEL_VERSION = "1.0.0-noisy_or"

PROFILE_THRESHOLDS: dict[str, tuple[int, int]] = {
    "balanced": (35, 70),
    "conservative": (25, 60),
    "permissive": (45, 80),
}


def combine_signals_noisy_or(signals: tuple[RiskSignal, ...]) -> int:
    """Combine individual risk signals using noisy-OR probability aggregation.

    P = 1 - PROD(1 - w_i * (s_i / 100))
    Score = round(100 * P), bounded to [0, 100]
    """
    if not signals:
        return 0

    prob_complement = 1.0
    for s in signals:
        p_i = s.weight * (s.severity / 100.0)
        p_i = max(0.0, min(1.0, p_i))
        prob_complement *= (1.0 - p_i)

    combined_prob = 1.0 - prob_complement
    score = int(round(100.0 * combined_prob))
    return max(0, min(100, score))


def determine_risk_level_and_decision(
    score: int, risk_profile: str
) -> tuple[RiskLevel, FinalDecision]:
    """Map integer score to RiskLevel and FinalDecision according to mandate profile."""
    low_thresh, high_thresh = PROFILE_THRESHOLDS.get(risk_profile, (35, 70))

    if score < low_thresh:
        return RiskLevel.LOW, FinalDecision.ALLOW
    if score < high_thresh:
        return RiskLevel.MEDIUM, FinalDecision.ESCALATE
    return RiskLevel.HIGH, FinalDecision.DENY


def score_risk(risk_input: RiskInput) -> RiskResult:
    """Pure, synchronous evaluation of all 10 signals, noisy-OR score, and decisions.

    Uses strictly risk_input.now for time. No I/O, no sleep, no scenario_id checks.
    """
    fired_signals: list[RiskSignal] = []
    for evaluator in ALL_SIGNAL_EVALUATORS:
        sig = evaluator(risk_input)
        if sig is not None:
            fired_signals.append(sig)

    # Sort signals by contribution (weight * severity) descending
    fired_signals.sort(key=lambda s: s.weight * s.severity, reverse=True)
    signals_tuple = tuple(fired_signals)

    score = combine_signals_noisy_or(signals_tuple)
    profile = risk_input.mandate.terms.risk_profile or "balanced"
    level, recommended = determine_risk_level_and_decision(score, profile)

    # Circuit breaker recommendation: deny band or hard-trip (probing or severity >= 90)
    has_hard_trip = any(s.name == "probing" or s.severity >= 90 for s in signals_tuple)
    breaker_recommended = (level == RiskLevel.HIGH) or has_hard_trip
    breaker_reasons: tuple[str, ...] = ()
    if breaker_recommended:
        # At most three reasons
        breaker_reasons = tuple(s.message for s in signals_tuple[:3])

    return RiskResult(
        score=score,
        level=level,
        recommended=recommended,
        signals=signals_tuple,
        breaker_recommended=breaker_recommended,
        breaker_reasons=breaker_reasons,
        model_version=MODEL_VERSION,
    )


class DeterministicRiskEngine:
    """Production implementation of contracts.risk_ports.RiskEngine protocol."""

    def __init__(self, history_store: object | None = None) -> None:
        self.history_store = history_store

    def score(self, risk_input: RiskInput) -> RiskResult:
        return score_risk(risk_input)

    async def record_attempt(self, risk_input: RiskInput, tx: Tx) -> None:
        if self.history_store is not None and hasattr(self.history_store, "record_attempt"):
            self.history_store.record_attempt(
                attempt_id=risk_input.attempt_id,
                request_id=risk_input.action.request_id,
                customer_id=risk_input.action.customer_id,
                agent_id=risk_input.action.agent_id,
                mandate_id=risk_input.action.mandate_id,
                amount_paise=risk_input.action.params.amount_paise,
                payee_id=risk_input.action.params.payee_id,
                verdict=risk_input.policy.verdict,
                decision="PENDING",
                attempted_at=risk_input.now,
            )

    async def record_outcome(self, outcome: object, tx: Tx) -> None:
        pass


__all__ = [
    "MODEL_VERSION",
    "PROFILE_THRESHOLDS",
    "combine_signals_noisy_or",
    "determine_risk_level_and_decision",
    "score_risk",
    "DeterministicRiskEngine",
]
