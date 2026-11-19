"""Member A gateway database, SQLite WAL PRAGMAs, and UnitOfWork implementation."""

from __future__ import annotations

import uuid
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
    """Async unit of work managing explicit BEGIN IMMEDIATE transactions."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self._tx: SqliteTx | None = None

    async def __aenter__(self) -> SqliteTx:
        self._session = self._session_factory()
        await self._session.begin()
        self._tx = SqliteTx(tx_id=f"tx_{uuid.uuid4().hex}", session=self._session)
        return self._tx

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> bool | None:
        if self._session is None:
            return None
        try:
            if exc_type is not None:
                await self._session.rollback()
            else:
                await self._session.commit()
        finally:
            await self._session.close()
            self._session = None
            self._tx = None
        return None
