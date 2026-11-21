"""Display-safe explanations of the mandate evaluator's ordered decisions."""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from contracts.common import PolicyVerdict
from contracts.core_models import CanonicalAction, MandateSnapshot, UsageSnapshot
from contracts.identity_models import AuthContext
from gateway.mandates.policy import CheckObserver, evaluate_policy


@dataclass(frozen=True)
class PolicyCheck:
    check: str
    status: Literal["pass", "fail", "step_up"]
    decisive: bool
    detail: dict[str, int]
    grant_index: int | None


@dataclass(frozen=True)
class PolicyExplanation:
    verdict: PolicyVerdict
    reason_code: str
    checks: tuple[PolicyCheck, ...]


def observe_checks(emit: Callable[[PolicyCheck], None]) -> CheckObserver:
    """Adapt policy comparisons to the same display-safe rows for live or offline use."""

    def record(
        check: str,
        passed: bool,
        soft: bool,
        decisive: bool,
        grant_index: int | None,
        detail: dict[str, int],
    ) -> None:
        status: Literal["pass", "fail", "step_up"] = "pass"
        if not passed:
            status = "step_up" if soft else "fail"
        emit(PolicyCheck(check, status, decisive, detail, grant_index))

    return record


def explain_policy(
    action: CanonicalAction,
    mandate: MandateSnapshot,
    usages: Sequence[UsageSnapshot],
    now: datetime,
    known_paid_payees: Collection[str] = (),
    *,
    auth: AuthContext | None = None,
) -> PolicyExplanation:
    """Return reached checks in evaluation order. Grant indices are zero-based."""
    checks: list[PolicyCheck] = []
    result = evaluate_policy(
        action, mandate, tuple(usages), now, known_paid_payees, auth, observe_checks(checks.append)
    )
    return PolicyExplanation(result.verdict, result.reason_codes[0], tuple(checks))
