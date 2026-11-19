"""Member C circuit-breaker recommendation and execution coordination."""

from __future__ import annotations

from typing import Any

from contracts.audit_models import AuditEvent
from contracts.audit_ports import AuditLog
from contracts.common import RiskLevel, Tx
from contracts.core_models import PayeeCard
from contracts.core_ports import Lifecycle
from contracts.escalation_ports import EscalationService
from contracts.reason_codes import ReasonCode
from contracts.risk_models import RiskInput, RiskResult


def evaluate_circuit_breaker(
    risk_input: RiskInput,
    risk_result: RiskResult,
    catalog: Any = None,
) -> tuple[bool, tuple[str, ...]]:
    """Determine whether a circuit breaker trip is recommended.

    Triggers on any of:
    1. Score in the HIGH band (level == RiskLevel.HIGH).
    2. Five policy denials within ten minutes (HistorySnapshot.recent_denial_times).
    3. intent_mismatch signal plus an unverified payee (PayeeCard.verified == False).
    """
    reasons: list[str] = []

    # Trigger 1: Score in HIGH band
    if risk_result.level == RiskLevel.HIGH:
        reasons.append(
            f"Risk score {risk_result.score} is in the HIGH deny band (threshold exceeded)"
        )

    # Trigger 2: Five policy denials in ten minutes
    now_ts = risk_input.now.timestamp()
    denials_10m = sum(
        1 for t in risk_input.history.recent_denial_times if t.timestamp() >= now_ts - 600
    )
    if denials_10m >= 5:
        reasons.append(f"Probing detected: {denials_10m} policy denials within the last 10 minutes")

    # Trigger 3: intent_mismatch plus unverified payee
    has_intent_mismatch = any(s.name == "intent_mismatch" for s in risk_result.signals)
    payee_id = risk_input.action.params.payee_id
    is_unverified_payee = False

    if payee_id:
        if catalog is not None and hasattr(catalog, "get_payee"):
            card = catalog.get_payee(payee_id)
            if card is None or (isinstance(card, PayeeCard) and not card.verified):
                is_unverified_payee = True
        else:
            # If not in known previously paid payees, treat as unverified
            if payee_id not in risk_input.history.previously_paid_payees:
                is_unverified_payee = True

    if has_intent_mismatch and is_unverified_payee:
        reasons.append(
            f"Intent mismatch detected on payment to unverified payee '{payee_id}'"
        )

    recommended = len(reasons) > 0
    return recommended, tuple(reasons)


def apply_breaker_recommendation(
    risk_input: RiskInput,
    risk_result: RiskResult,
    catalog: Any = None,
) -> RiskResult:
    """Enrich RiskResult with breaker recommendation if any trigger condition matches."""
    rec, reasons = evaluate_circuit_breaker(risk_input, risk_result, catalog)
    if not rec:
        return risk_result

    combined_reasons = tuple(dict.fromkeys(risk_result.breaker_reasons + reasons))
    return risk_result.model_copy(
        update={
            "breaker_recommended": True,
            "breaker_reasons": combined_reasons,
        }
    )


async def execute_breaker_trip(
    risk_input: RiskInput,
    risk_result: RiskResult,
    lifecycle: Lifecycle,
    audit_log: AuditLog,
    escalation_service: EscalationService | None = None,
    tx: Tx | None = None,
    *,
    scope: str = "agent",
) -> None:
    """Perform side effects when the circuit breaker fires.

    - Scope 'agent': Globally suspends the registered agent and cancels pending approvals.
    - Scope 'customer': Suspends customer's delegation to this agent.
    - Emits exactly one audit event through the AuditLog port.
    """
    agent_id = risk_input.action.agent_id
    customer_id = risk_input.action.customer_id
    reasons = (ReasonCode.E_RISK_BREAKER_OPEN.value,)

    if scope == "customer":
        await lifecycle.suspend_customer_delegation(customer_id, agent_id, reasons)
    else:
        await lifecycle.suspend_agent(agent_id, reasons)
        if escalation_service is not None and tx is not None:
            await escalation_service.cancel_for_agent(agent_id, tx)

    # Emit audit event through AuditLog port exactly once
    event = AuditEvent(
        event_type="risk.breaker_tripped",
        request_id=risk_input.action.request_id,
        customer_id=customer_id,
        agent_id=agent_id,
        mandate_id=risk_input.action.mandate_id,
        mandate_hash=risk_input.action.mandate_hash,
        decision="DENY",
        reason_codes=reasons,
        risk={
            "score": risk_result.score,
            "level": str(risk_result.level),
            "breaker_reasons": list(risk_result.breaker_reasons),
        },
        safe_payload={
            "scope": scope,
            "action_type": str(risk_input.action.action_type),
            "amount_paise": risk_input.action.params.amount_paise,
        },
        occurred_at=risk_input.now,
    )
    await audit_log.append(event, tx)


__all__ = [
    "evaluate_circuit_breaker",
    "apply_breaker_recommendation",
    "execute_breaker_trip",
]
