"""Member C explicit risk/execution substitutes."""

from __future__ import annotations

from dataclasses import dataclass, field

from contracts.common import AuditRef
from contracts.core_models import ActionStatus, ExecutionResult
from contracts.risk_models import RiskInput, RiskResult


@dataclass
class FixtureRiskEngine:
    result: RiskResult

    def score(self, risk_input: RiskInput) -> RiskResult:
        return self.result


@dataclass
class RecordingExecutionPort:
    request_ids: list[str] = field(default_factory=list)

    async def execute_stored(
        self, request_id: str, escalation_id: str | None = None
    ) -> ExecutionResult:
        self.request_ids.append(request_id)
        return ExecutionResult(
            request_id=request_id,
            status=ActionStatus.FAILED,
            audit_ref=AuditRef(seq=1, entry_hash="0" * 64),
            escalation_id=escalation_id,
        )
