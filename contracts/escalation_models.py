"""Member C owned escalation boundary DTOs."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from .common import AuditRef, ContractModel, OpaqueId
from .core_models import CanonicalAction, ExecutionResult


class EscalationState(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    EXECUTED = "EXECUTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class EscalationDecision(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class Escalation(ContractModel):
    escalation_id: OpaqueId
    request_id: OpaqueId
    customer_id: OpaqueId
    agent_id: OpaqueId
    mandate_id: OpaqueId
    action: CanonicalAction
    action_hash: OpaqueId
    trusted_summary: dict[str, Any]
    reason_codes: tuple[str, ...]
    expires_at: datetime
    state: EscalationState
    decided_at: datetime | None = None
    decided_by: str | None = None
    result: ExecutionResult | None = None
    audit_ref: AuditRef | None = None
