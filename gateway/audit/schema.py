"""Member B audit table registration and SQLite append-only trigger setup."""

from __future__ import annotations

from sqlalchemy import Connection, Engine, MetaData, text

from .models import AuditBase

AUDIT_METADATA: MetaData = AuditBase.metadata

APPEND_ONLY_TRIGGER_SQL: tuple[str, ...] = (
    """
    CREATE TRIGGER IF NOT EXISTS trg_audit_log_no_update
    BEFORE UPDATE ON audit_log
    BEGIN
        SELECT RAISE(ABORT, 'audit_log is append-only: UPDATE is forbidden');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_audit_log_no_delete
    BEFORE DELETE ON audit_log
    BEGIN
        SELECT RAISE(ABORT, 'audit_log is append-only: DELETE is forbidden');
    END;
    """,
)


def register_audit_tables(target_metadata: MetaData) -> None:
    """Register `audit_log` into a shared gateway SQLAlchemy `MetaData`."""

    for table in AUDIT_METADATA.tables.values():
        if table.name not in target_metadata.tables:
            table.to_metadata(target_metadata)


def install_audit_triggers(engine_or_conn: Engine | Connection) -> None:
    """Install SQLite append-only BEFORE UPDATE and BEFORE DELETE triggers on `audit_log`."""

    if isinstance(engine_or_conn, Engine):
        with engine_or_conn.begin() as conn:
            for ddl in APPEND_ONLY_TRIGGER_SQL:
                conn.execute(text(ddl))
    else:
        for ddl in APPEND_ONLY_TRIGGER_SQL:
            engine_or_conn.execute(text(ddl))


def create_audit_schema(engine_or_conn: Engine | Connection) -> None:
    """Create the `audit_log` table and install its append-only SQLite triggers."""

    AUDIT_METADATA.create_all(engine_or_conn)
    install_audit_triggers(engine_or_conn)


__all__ = [
    "APPEND_ONLY_TRIGGER_SQL",
    "AUDIT_METADATA",
    "create_audit_schema",
    "install_audit_triggers",
    "register_audit_tables",
]
