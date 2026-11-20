"""Member A execution ledger, action storage, and idempotency database schema."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from gateway.platform.db import Base


class IdempotencyRecordTable(Base):
    """Atomic gateway unique business operation record per customer/agent/mandate."""

    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint(
            "customer_id",
            "agent_id",
            "mandate_id",
            "idempotency_key",
            name="uq_idempotency_scope",
        ),
    )

    idempotency_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    mandate_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    request_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    cached_status: Mapped[str] = mapped_column(String(32), nullable=False, default="RECEIVED")
    cached_response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class ActionTable(Base):
    """Immutable canonical action record, separate note, and execution lifecycle status."""

    __tablename__ = "actions"

    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    mandate_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    mandate_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    canonical_json: Mapped[str] = mapped_column(Text, nullable=False)
    action_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    bank_idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="RECEIVED", index=True
    )
    bank_operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    audit_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audit_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    escalation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class ReservationTable(Base):
    """Atomic multi-grant reservation tracking pending and committed budget spend."""

    __tablename__ = "reservations"

    reservation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("actions.request_id", ondelete="CASCADE"), nullable=False, index=True
    )
    mandate_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    grant_window_keys_json: Mapped[str] = mapped_column(Text, nullable=False)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    count_delta: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="RESERVED", index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
