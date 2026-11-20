"""Member A mandate and usage SQLAlchemy 2 database schema."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from gateway.platform.db import Base


class MandateTable(Base):
    __tablename__ = "mandates"

    mandate_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    terms_json: Mapped[str] = mapped_column(Text, nullable=False)
    mandate_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    sig: Mapped[str] = mapped_column(String(256), nullable=False)
    key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class MandateUsageTable(Base):
    __tablename__ = "mandate_usage"

    mandate_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("mandates.mandate_id", ondelete="CASCADE"), primary_key=True
    )
    grant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_key: Mapped[str] = mapped_column(String(64), primary_key=True)

    committed_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    committed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
