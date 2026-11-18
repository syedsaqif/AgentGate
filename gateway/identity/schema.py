"""Member B identity-owned SQLAlchemy table registration and SQLite WAL helpers."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, Engine, MetaData, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .models import IdentityBase

IDENTITY_METADATA: MetaData = IdentityBase.metadata


def register_identity_tables(target_metadata: MetaData) -> None:
    """Register identity-owned tables into a shared SQLAlchemy `MetaData`."""

    for table in IDENTITY_METADATA.tables.values():
        if table.name not in target_metadata.tables:
            table.to_metadata(target_metadata)


def create_identity_schema(engine_or_conn: Engine | Connection) -> None:
    """Create identity-owned tables if they do not already exist."""

    IDENTITY_METADATA.create_all(engine_or_conn)


def create_sqlite_identity_engine(database_url: str, *, busy_timeout_ms: int = 5000) -> Engine:
    """Create a SQLite engine configured with WAL mode, foreign keys, and BEGIN IMMEDIATE."""

    normalized_url = database_url.replace("sqlite+aiosqlite://", "sqlite://", 1)
    engine = create_engine(
        normalized_url,
        future=True,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_conn: Any, _connection_record: Any) -> None:
        dbapi_conn.isolation_level = None
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def _do_begin(conn: Any) -> None:
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def create_identity_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create a standard non-expiring SQLAlchemy 2 session factory."""

    return sessionmaker(engine, expire_on_commit=False)


__all__ = [
    "IDENTITY_METADATA",
    "create_identity_schema",
    "create_identity_session_factory",
    "create_sqlite_identity_engine",
    "register_identity_tables",
]
