"""Member A gateway database, SQLite WAL PRAGMAs, and UnitOfWork implementation."""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Any

from sqlalchemy import MetaData, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from contracts.common import Tx, UnitOfWork

# Shared metadata registry for gateway tables
metadata = MetaData()


class Base(DeclarativeBase):
    """Declarative base sharing metadata across gateway modules."""

    metadata = metadata


def create_db_engine(db_url: str, echo: bool = False) -> AsyncEngine:
    """Create AsyncEngine with SQLite WAL, busy_timeout, foreign_keys, and synchronous PRAGMAs."""
    engine = create_async_engine(db_url, echo=echo)

    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass  # in-memory SQLite does not support WAL
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    @event.listens_for(engine.sync_engine, "begin")
    def do_begin_immediate(conn: Any) -> None:
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create an async session factory."""
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


class SqliteTx(Tx):
    """Concrete transaction context wrapping an AsyncSession."""

    def __init__(self, tx_id: str, session: AsyncSession) -> None:
        self._id = tx_id
        self.session = session

    @property
    def id(self) -> str:
        return self._id


class SqliteUnitOfWork(UnitOfWork):
    """Async unit of work managing explicit BEGIN IMMEDIATE transactions with nesting support."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._stack: ContextVar[list[tuple[AsyncSession, Any, SqliteTx]] | None] = ContextVar(
            f"uow_stack_{id(self)}", default=None
        )

    async def __aenter__(self) -> SqliteTx:
        stack = list(self._stack.get() or [])
        if not stack:
            # Outermost transaction: create new session and begin
            session = self._session_factory()
            await session.begin()
            tx = SqliteTx(tx_id=f"tx_{uuid.uuid4().hex}", session=session)
            stack.append((session, None, tx))
            self._stack.set(stack)
            return tx
        else:
            # Nested transaction: create savepoint on active session
            active_session, _, active_tx = stack[-1]
            savepoint = await active_session.begin_nested()
            nested_tx = SqliteTx(tx_id=f"{active_tx.id}_sub{len(stack)}", session=active_session)
            stack.append((active_session, savepoint, nested_tx))
            self._stack.set(stack)
            return nested_tx

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> bool | None:
        stack = list(self._stack.get() or [])
        if not stack:
            return None

        session, savepoint, _ = stack.pop()
        self._stack.set(stack)

        if savepoint is not None:
            # Nested savepoint
            if exc_type is not None:
                await savepoint.rollback()
            else:
                await savepoint.commit()
        else:
            # Outermost transaction
            try:
                if exc_type is not None:
                    await session.rollback()
                else:
                    await session.commit()
            finally:
                await session.close()

        return None
