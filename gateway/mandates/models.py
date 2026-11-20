"""Member A private mandate persistence models."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gateway.platform.db import Base


class MandateRecord(Base):
    """Persistent storage for approved, signed customer mandates."""

    __tablename__ = "mandates"

    mandate_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terms_json: Mapped[str] = mapped_column(Text, nullable=False)
    mandate_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    sig: Mapped[str] = mapped_column(Text, nullable=False)
    key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    usages: Mapped[list[MandateUsageRecord]] = relationship(
        "MandateUsageRecord", back_populates="mandate", cascade="all, delete-orphan"
    )


class MandateUsageRecord(Base):
    """Persistent tracking of financial budget and transaction limits per grant."""

    __tablename__ = "mandate_usage"

    usage_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mandate_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("mandates.mandate_id"), nullable=False, index=True
    )
    grant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    committed_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    remaining_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining_count: Mapped[int] = mapped_column(Integer, nullable=False)
    window: Mapped[str] = mapped_column(String(32), nullable=False, default="mandate")

    mandate: Mapped[MandateRecord] = relationship("MandateRecord", back_populates="usages")
