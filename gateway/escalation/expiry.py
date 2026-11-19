"""Member C escalation expiry / TTL sweeper."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.escalation_models import EscalationState
from gateway.escalation.models import EscalationRecord


async def expire_due_in_session(session: AsyncSession, now: datetime) -> int:
    """Transition PENDING escalations that have passed their deadline to EXPIRED.

    Runs atomically within the provided SQLAlchemy session.
    """
    stmt = (
        update(EscalationRecord)
        .where(
            EscalationRecord.state == EscalationState.PENDING.value,
            EscalationRecord.expires_at <= now,
        )
        .values(state=EscalationState.EXPIRED.value)
    )
    res = await session.execute(stmt)
    rowcount: int = getattr(res, "rowcount", 0)
    return rowcount


__all__ = ["expire_due_in_session"]
