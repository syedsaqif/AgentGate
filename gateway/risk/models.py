"""Member C private risk persistence models."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Local declarative base for Member C risk models until A's composition root merges it."""


class RiskAttemptRecord(Base):
    """Stores every evaluated action attempt, including denials and unauthenticated traffic."""

    __tablename__ = "risk_attempts"

    attempt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    agent_id: Mapped[str] = mapped_column(String(64), index=True)
    mandate_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    action_type: Mapped[str] = mapped_column(String(32))
    amount_paise: Mapped[int] = mapped_column(Integer, default=0)
    payee_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verdict: Mapped[str] = mapped_column(String(32))  # e.g. ALLOW, STEP_UP, DENY, AUTH_FAILED
    decision: Mapped[str] = mapped_column(String(32))  # e.g. ALLOW, ESCALATE, DENY
    risk_score: Mapped[int] = mapped_column(Integer, default=0)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)


class RiskBaselineRecord(Base):
    """Stores verified behavioral baselines per (customer_id, agent_id, mandate_id)."""

    __tablename__ = "risk_baselines"

    baseline_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    agent_id: Mapped[str] = mapped_column(String(64), index=True)
    mandate_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    median_amount_paise: Mapped[int] = mapped_column(Integer, default=0)
    mad_amount_paise: Mapped[int] = mapped_column(Integer, default=0)
    min_amount_paise: Mapped[int] = mapped_column(Integer, default=0)
    max_amount_paise: Mapped[int] = mapped_column(Integer, default=0)
    typical_hour_start: Mapped[int] = mapped_column(Integer, default=9)
    typical_hour_end: Mapped[int] = mapped_column(Integer, default=18)
    known_payees_json: Mapped[str] = mapped_column(String(1024), default="[]")
    amounts_json: Mapped[str] = mapped_column(String(4096), default="[]")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
