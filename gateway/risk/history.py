"""Member C history tracking, baseline computation, and snapshot builder."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from contracts.risk_models import HistorySnapshot


def calculate_median(values: list[int]) -> int:
    """Calculate integer median of integer paise amounts."""
    if not values:
        return 0
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    mid = n // 2
    if n % 2 == 1:
        return sorted_vals[mid]
    return (sorted_vals[mid - 1] + sorted_vals[mid]) // 2


def calculate_mad(values: list[int], median: int) -> int:
    """Calculate Median Absolute Deviation (MAD) in integer paise."""
    if not values:
        return 0
    deviations = [abs(v - median) for v in values]
    return calculate_median(deviations)


def calculate_robust_z_score(amount: int, median: int, mad: int, sample_count: int) -> float:
    """Calculate robust z-score: 0.6745 * |amount - median| / MAD.

    Defined behaviors for edge cases:
    - Zero history (sample_count < 10): returns 0.0 (warm-up priors apply).
    - MAD == 0 (all samples identical):
      - If amount == median, z-score is 0.0.
      - If amount != median, fallback to relative deviation using 10% of median (min 100 paise).
    """
    if sample_count < 10:
        return 0.0
    if mad == 0:
        if amount == median:
            return 0.0
        effective_scale = max(int(0.10 * median), 100)
        return abs(amount - median) / float(effective_scale)
    return 0.6745 * abs(amount - median) / float(mad)


@dataclass
class BaselineProfile:
    """In-memory profile containing confirmed successful observations for an agent/customer."""

    sample_count: int = 0
    median_amount_paise: int = 0
    mad_amount_paise: int = 0
    min_amount_paise: int = 0
    max_amount_paise: int = 0
    amounts_paise: list[int] = field(default_factory=list)
    known_payees: set[str] = field(default_factory=set)
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def update_from_successful_amount(
        self, amount_paise: int, payee_id: str | None, now: datetime
    ) -> None:
        self.amounts_paise.append(amount_paise)
        if len(self.amounts_paise) > 100:
            self.amounts_paise.pop(0)  # Bounded window of 100 observations to limit drift
        self.sample_count = len(self.amounts_paise)
        self.median_amount_paise = calculate_median(self.amounts_paise)
        self.mad_amount_paise = calculate_mad(self.amounts_paise, self.median_amount_paise)
        self.min_amount_paise = min(self.amounts_paise)
        self.max_amount_paise = max(self.amounts_paise)
        if payee_id:
            self.known_payees.add(payee_id)
        self.updated_at = now


@dataclass
class AttemptRecord:
    attempt_id: str
    request_id: str
    customer_id: str
    agent_id: str
    mandate_id: str | None
    amount_paise: int
    payee_id: str | None
    verdict: str
    decision: str
    attempted_at: datetime
    is_auth_failure: bool = False
    is_synthetic: bool = False


class RiskHistoryStore:
    """Manages risk attempt logs, verified baselines, and snapshot extraction."""

    def __init__(self) -> None:
        # Key: (customer_id, agent_id)
        self.baselines: dict[tuple[str, str], BaselineProfile] = {}
        # List of all recorded attempts
        self.attempts: list[AttemptRecord] = []
        # Index for idempotency: attempt_id -> AttemptRecord
        self.attempt_by_id: dict[str, AttemptRecord] = {}

    def get_or_create_baseline(self, customer_id: str, agent_id: str) -> BaselineProfile:
        key = (customer_id, agent_id)
        if key not in self.baselines:
            self.baselines[key] = BaselineProfile()
        return self.baselines[key]

    def record_attempt(
        self,
        *,
        attempt_id: str,
        request_id: str,
        customer_id: str,
        agent_id: str,
        mandate_id: str | None,
        amount_paise: int,
        payee_id: str | None,
        verdict: str,
        decision: str,
        attempted_at: datetime,
        is_auth_failure: bool = False,
        is_synthetic: bool = False,
    ) -> AttemptRecord:
        """Record an evaluated action attempt idempotently.

        Current attempt is counted exactly once using attempt_id.
        Auth-failed traffic is flagged and excluded from verified customer/agent metrics.
        """
        if attempt_id in self.attempt_by_id:
            return self.attempt_by_id[attempt_id]

        record = AttemptRecord(
            attempt_id=attempt_id,
            request_id=request_id,
            customer_id=customer_id if not is_auth_failure else "unauthenticated",
            agent_id=agent_id if not is_auth_failure else "unauthenticated",
            mandate_id=mandate_id if not is_auth_failure else None,
            amount_paise=amount_paise,
            payee_id=payee_id,
            verdict=verdict,
            decision=decision,
            attempted_at=attempted_at,
            is_auth_failure=is_auth_failure,
            is_synthetic=is_synthetic,
        )
        self.attempts.append(record)
        self.attempt_by_id[attempt_id] = record
        return record

    def record_outcome(
        self,
        *,
        request_id: str,
        customer_id: str,
        agent_id: str,
        mandate_id: str | None,
        payee_id: str | None,
        amount_paise: int,
        status: str,
        executed_at: datetime,
        is_synthetic: bool = False,
    ) -> None:
        """Update normal-success baseline only if execution confirmed successful.

        Failed, rejected, or uncertain bank outcomes NEVER update the baseline.
        """
        if status != "EXECUTED":
            return

        baseline = self.get_or_create_baseline(customer_id, agent_id)
        baseline.update_from_successful_amount(amount_paise, payee_id, executed_at)

    def get_history_snapshot(
        self,
        *,
        customer_id: str,
        agent_id: str,
        current_attempt_id: str | None = None,
        now: datetime | None = None,
        recent_window_seconds: int = 600,
    ) -> HistorySnapshot:
        """Build HistorySnapshot for risk feature evaluation.

        - successful_amounts_paise comes from confirmed baseline profile.
        - recent_attempt_times includes all authenticated attempts within the window (deduped).
        - recent_denial_times includes all policy denials within the window.
        - previously_paid_payees comes from confirmed baseline payees.
        """
        cutoff = (now or datetime.now(UTC)) - timedelta(seconds=recent_window_seconds)
        baseline = self.get_or_create_baseline(customer_id, agent_id)

        attempt_times: list[datetime] = []
        denial_times: list[datetime] = []
        seen_attempt_ids: set[str] = set()

        for att in reversed(self.attempts):
            if att.attempted_at < cutoff:
                break
            # Do not mix unauthenticated/auth-failed traffic into verified history
            if att.is_auth_failure or att.customer_id != customer_id or att.agent_id != agent_id:
                continue

            # Idempotency: count current attempt exactly once
            if att.attempt_id in seen_attempt_ids:
                continue
            seen_attempt_ids.add(att.attempt_id)

            attempt_times.append(att.attempted_at)
            if att.verdict in ("DENY", "REJECTED") or att.decision == "DENY":
                denial_times.append(att.attempted_at)

        return HistorySnapshot(
            successful_amounts_paise=tuple(baseline.amounts_paise),
            recent_attempt_times=tuple(reversed(attempt_times)),
            recent_denial_times=tuple(reversed(denial_times)),
            previously_paid_payees=tuple(sorted(baseline.known_payees)),
        )


__all__ = [
    "calculate_median",
    "calculate_mad",
    "calculate_robust_z_score",
    "BaselineProfile",
    "AttemptRecord",
    "RiskHistoryStore",
]
