"""Member A mandate database repository."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.mandates.schema import MandateTable, MandateUsageTable


async def insert_mandate(session: AsyncSession, mandate: MandateTable) -> None:
    session.add(mandate)


async def insert_usage_row(session: AsyncSession, usage: MandateUsageTable) -> None:
    session.add(usage)


async def get_mandate(
    session: AsyncSession, mandate_id: str
) -> MandateTable | None:
    stmt = select(MandateTable).where(MandateTable.mandate_id == mandate_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_usage_rows(
    session: AsyncSession, mandate_id: str
) -> list[MandateUsageTable]:
    stmt = select(MandateUsageTable).where(MandateUsageTable.mandate_id == mandate_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_mandates_by_customer(
    session: AsyncSession,
    customer_id: str,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[MandateTable], str | None]:
    stmt = (
        select(MandateTable)
        .where(MandateTable.customer_id == customer_id)
        .order_by(desc(MandateTable.created_at), desc(MandateTable.mandate_id))
        .limit(min(limit, 100) + 1)
    )
    if cursor:
        stmt = stmt.where(MandateTable.mandate_id < cursor)

    result = await session.execute(stmt)
    rows = list(result.scalars().all())

    next_cursor = None
    if len(rows) > limit:
        next_cursor = rows[limit - 1].mandate_id
        rows = rows[:limit]

    return rows, next_cursor


async def update_mandate_status(
    session: AsyncSession, mandate_id: str, status: str
) -> None:
    stmt = (
        update(MandateTable)
        .where(MandateTable.mandate_id == mandate_id)
        .values(status=status, updated_at=datetime.now(UTC))
    )
    await session.execute(stmt)
