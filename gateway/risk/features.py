"""Member C trusted feature construction and RiskInput builder."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from contracts.common import OpaqueId
from contracts.core_models import CanonicalAction, MandateSnapshot, PayeeCard, PolicyResult
from contracts.identity_models import AuthContext
from contracts.risk_models import HistorySnapshot, RiskInput


def build_risk_input(
    *,
    auth: AuthContext,
    action: CanonicalAction,
    policy: PolicyResult,
    mandate: MandateSnapshot,
    history: HistorySnapshot,
    now: datetime,
    attempt_id: OpaqueId | None = None,
) -> RiskInput:
    """Build a strongly typed RiskInput DTO for the risk scoring engine.

    Uses only verified server-side inputs. Agent text (note) is not evaluated here.
    """
    resolved_attempt_id = attempt_id or f"att_{action.request_id}"
    return RiskInput(
        auth=auth,
        action=action,
        policy=policy,
        mandate=mandate,
        history=history,
        now=now,
        attempt_id=resolved_attempt_id,
    )


def extract_action_payee_category(action: CanonicalAction, catalog: Any = None) -> str | None:
    """Resolve the registered category for the action's payee via catalog if available."""
    if catalog is not None and action.params.payee_id:
        card = catalog.get_payee(action.params.payee_id)
        if isinstance(card, PayeeCard):
            return card.category
    return None


def is_new_payee(action: CanonicalAction, history: HistorySnapshot) -> bool:
    """Check if the action targets a payee never previously paid in verified history."""
    if not action.params.payee_id:
        return False
    return action.params.payee_id not in history.previously_paid_payees


def count_recent_attempts(
    history: HistorySnapshot, *, window_seconds: int, now: datetime
) -> int:
    """Count verified attempts within the past window_seconds."""
    cutoff = now.timestamp() - window_seconds
    return sum(1 for t in history.recent_attempt_times if t.timestamp() >= cutoff)


def count_recent_denials(
    history: HistorySnapshot, *, window_seconds: int, now: datetime
) -> int:
    """Count policy denials within the past window_seconds."""
    cutoff = now.timestamp() - window_seconds
    return sum(1 for t in history.recent_denial_times if t.timestamp() >= cutoff)


__all__ = [
    "build_risk_input",
    "extract_action_payee_category",
    "is_new_payee",
    "count_recent_attempts",
    "count_recent_denials",
]
