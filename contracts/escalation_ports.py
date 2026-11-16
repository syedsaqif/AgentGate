"""Member C owned escalation protocol."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .common import OpaqueId, Tx
from .core_models import CanonicalAction
from .escalation_models import Escalation, EscalationDecision
from .risk_models import RiskResult


class EscalationService(Protocol):
    async def create(
        self, action: CanonicalAction, assessment: RiskResult, tx: Tx
    ) -> Escalation: ...

    async def get_owned(self, escalation_id: OpaqueId, subject_id: OpaqueId) -> Escalation: ...

    async def decide(
        self,
        escalation_id: OpaqueId,
        decision: EscalationDecision,
        subject_id: OpaqueId,
        tx: Tx,
    ) -> Escalation: ...

    async def claim_execution(
        self, escalation_id: OpaqueId, expected_hash: str, tx: Tx
    ) -> Escalation: ...

    async def cancel_for_mandate(self, mandate_id: OpaqueId, tx: Tx) -> None: ...

    async def cancel_for_agent(self, agent_id: OpaqueId, tx: Tx) -> None: ...

    async def expire_due(self, now: datetime) -> int: ...
