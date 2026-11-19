"""Member A bank mock database repository."""

from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from bank_mock.schema import AccountTable, BankIdempotencyTable, LedgerTable, PayeeTable


async def get_account(session: AsyncSession, account_id: str) -> AccountTable | None:
    stmt = select(AccountTable).where(AccountTable.account_id == account_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_payee(session: AsyncSession, payee_id: str) -> PayeeTable | None:
    stmt = select(PayeeTable).where(PayeeTable.payee_id == payee_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_idempotency_record(
    session: AsyncSession, bank_idempotency_key: str
) -> BankIdempotencyTable | None:
    stmt = select(BankIdempotencyTable).where(
        BankIdempotencyTable.bank_idempotency_key == bank_idempotency_key
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_ledger_entries(
    session: AsyncSession,
    account_id: str,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[LedgerTable], str | None]:
    stmt = (
        select(LedgerTable)
        .where(LedgerTable.account_id == account_id)
        .order_by(desc(LedgerTable.created_at), desc(LedgerTable.entry_id))
        .limit(min(limit, 100) + 1)
    )
    if cursor:
        stmt = stmt.where(LedgerTable.entry_id < cursor)

    result = await session.execute(stmt)
    rows = list(result.scalars().all())

    next_cursor = None
    if len(rows) > limit:
        next_cursor = rows[limit - 1].entry_id
        rows = rows[:limit]

    return rows, next_cursor
