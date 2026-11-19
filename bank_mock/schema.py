"""Member A separate bank database schema and connection factory."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    event,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

bank_metadata = MetaData()


class BankBase(DeclarativeBase):
    metadata = bank_metadata


class AccountTable(BankBase):
    __tablename__ = "accounts"

    account_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    balance_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class PayeeTable(BankBase):
    __tablename__ = "payees"

    payee_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class LedgerTable(BankBase):
    __tablename__ = "ledger"

    entry_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), ForeignKey("accounts.account_id"), nullable=False, index=True)
    entry_type: Mapped[str] = mapped_column(String(16), nullable=False)  # "DEBIT" | "CREDIT"
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    reference: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)


class BankIdempotencyTable(BankBase):
    __tablename__ = "bank_idempotency_records"

    bank_idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    bank_operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_paise: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


def create_bank_db_engine(db_url: str, echo: bool = False) -> AsyncEngine:
    """Create AsyncEngine for the bank mock with WAL, busy_timeout, and foreign keys."""
    if "sqlite" in db_url and "///" in db_url:
        path_str = db_url.split("///", 1)[1]
        if path_str and not path_str.startswith(":memory:"):
            Path(path_str).resolve().parent.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(db_url, echo=echo)

    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


def create_bank_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
