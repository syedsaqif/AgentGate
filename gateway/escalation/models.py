"""Member C private escalation persistence models."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from contracts.audit_models import AuditRef
from contracts.core_models import CanonicalAction, ExecutionResult
from contracts.escalation_models import Escalation, EscalationState

try:
    from gateway.platform.db import Base
except ImportError:

    class Base(DeclarativeBase):  # type: ignore[no-redef]
        """Fallback local declarative base if platform.db is not yet available."""


class EscalationRecord(Base):
    """Stores human approval workflows, bound action hash, trusted summary, and decision state."""

    __tablename__ = "escalations"

    escalation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    agent_id: Mapped[str] = mapped_column(String(64), index=True)
    mandate_id: Mapped[str] = mapped_column(String(64), index=True)
    action_type: Mapped[str] = mapped_column(String(32))
    action_json: Mapped[str] = mapped_column(Text)
    action_hash: Mapped[str] = mapped_column(String(128), index=True)
    trusted_summary_json: Mapped[str] = mapped_column(Text)
    reason_codes_json: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    state: Mapped[str] = mapped_column(
        String(32), default=EscalationState.PENDING.value, index=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    audit_ref_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audit_ref_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def to_dto(self) -> Escalation:
        """Convert database record to the frozen Escalation ContractModel."""
        action = CanonicalAction.model_validate_json(self.action_json)
        trusted_summary = json.loads(self.trusted_summary_json)
        reason_codes = tuple(json.loads(self.reason_codes_json))
        result = (
            ExecutionResult.model_validate_json(self.result_json)
            if self.result_json
            else None
        )
        audit_ref = (
            AuditRef(seq=self.audit_ref_seq, entry_hash=self.audit_ref_hash)
            if self.audit_ref_seq is not None and self.audit_ref_hash is not None
            else None
        )
        expires_at = (
            self.expires_at
            if self.expires_at.tzinfo is not None
            else self.expires_at.replace(tzinfo=UTC)
        )
        decided_at = (
            (
                self.decided_at
                if self.decided_at.tzinfo is not None
                else self.decided_at.replace(tzinfo=UTC)
            )
            if self.decided_at is not None
            else None
        )
        return Escalation(
            escalation_id=self.escalation_id,
            request_id=self.request_id,
            customer_id=self.customer_id,
            agent_id=self.agent_id,
            mandate_id=self.mandate_id,
            action=action,
            action_hash=self.action_hash,
            trusted_summary=trusted_summary,
            reason_codes=reason_codes,
            expires_at=expires_at,
            state=EscalationState(self.state),
            decided_at=decided_at,
            decided_by=self.decided_by,
            result=result,
            audit_ref=audit_ref,
        )

    @classmethod
    def from_dto(cls, esc: Escalation, created_at: datetime) -> EscalationRecord:
        """Create a database record from an Escalation ContractModel."""
        action_json = esc.action.model_dump_json()
        trusted_summary_json = json.dumps(esc.trusted_summary)
        reason_codes_json = json.dumps(list(esc.reason_codes))
        result_json = esc.result.model_dump_json() if esc.result else None
        audit_ref_seq = esc.audit_ref.seq if esc.audit_ref else None
        audit_ref_hash = esc.audit_ref.entry_hash if esc.audit_ref else None

        state_val = esc.state.value if hasattr(esc.state, "value") else str(esc.state)

        return cls(
            escalation_id=str(esc.escalation_id),
            request_id=str(esc.request_id),
            customer_id=str(esc.customer_id),
            agent_id=str(esc.agent_id),
            mandate_id=str(esc.mandate_id),
            action_type=str(esc.action.action_type),
            action_json=action_json,
            action_hash=str(esc.action_hash),
            trusted_summary_json=trusted_summary_json,
            reason_codes_json=reason_codes_json,
            expires_at=esc.expires_at,
            state=state_val,
            decided_at=esc.decided_at,
            decided_by=esc.decided_by,
            result_json=result_json,
            audit_ref_seq=audit_ref_seq,
            audit_ref_hash=audit_ref_hash,
            created_at=created_at,
        )


__all__ = ["Base", "EscalationRecord"]
