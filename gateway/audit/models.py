"""Member B private SQLAlchemy 2 ORM model for the append-only signed `audit_log` table.

Lives in the same gateway SQLite WAL database as all other gateway tables; never creates
a separate audit database and never leaks ORM models across module boundaries.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import CheckConstraint, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class AuditBase(DeclarativeBase):
    """Declarative base for Member B audit-owned table."""


class AuditLogRecord(AuditBase):
    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint("seq >= 1", name="ck_audit_log_seq_positive"),
        CheckConstraint("length(prev_hash) = 64", name="ck_audit_log_prev_hash_len"),
        CheckConstraint("length(entry_hash) = 64", name="ck_audit_log_entry_hash_len"),
    )

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    mandate_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    mandate_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason_codes_json: Mapped[str] = mapped_column(Text, nullable=False)
    risk_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    safe_payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[str] = mapped_column(String(40), nullable=False)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    sig: Mapped[str] = mapped_column(String(128), nullable=False)

    def to_canonical_entry(self) -> dict[str, Any]:
        """Reconstruct the exact canonical entry dict (excluding `entry_hash` and `sig`)."""

        return {
            "seq": self.seq,
            "prev_hash": self.prev_hash,
            "event_type": self.event_type,
            "request_id": self.request_id,
            "correlation_id": self.correlation_id,
            "customer_id": self.customer_id,
            "agent_id": self.agent_id,
            "mandate_id": self.mandate_id,
            "mandate_hash": self.mandate_hash,
            "decision": self.decision,
            "reason_codes": json.loads(self.reason_codes_json),
            "risk": json.loads(self.risk_json) if self.risk_json is not None else None,
            "safe_payload": json.loads(self.safe_payload_json),
            "occurred_at": self.occurred_at,
        }


__all__ = ["AuditBase", "AuditLogRecord"]
