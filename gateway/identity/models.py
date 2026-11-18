"""Member B private SQLAlchemy 2 ORM models and transaction helpers for identity tables.

ORM objects are strictly private to `gateway/identity/` and never cross module boundaries.
Supports both synchronous SQLAlchemy `Session` (compatible with the locked `uv.lock`
without `greenlet`) and async `AsyncSession` if provided by an external caller `Tx`.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from contracts.common import Tx


class IdentityBase(DeclarativeBase):
    """Declarative base for Member B identity-owned tables."""


class CustomerRecord(IdentityBase):
    __tablename__ = "customers"

    customer_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    pin_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)


class OperatorRecord(IdentityBase):
    __tablename__ = "operators"

    operator_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)


class HumanSessionRecord(IdentityBase):
    __tablename__ = "human_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    issued_at: Mapped[str] = mapped_column(String(40), nullable=False)
    expires_at: Mapped[str] = mapped_column(String(40), nullable=False)
    revoked_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class AgentRecord(IdentityBase):
    __tablename__ = "agents"

    agent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    vendor: Mapped[str] = mapped_column(String(120), nullable=False)
    public_jwk_json: Mapped[str] = mapped_column(Text, nullable=False)
    jkt: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    max_scopes_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


class ProofReplayRecord(IdentityBase):
    __tablename__ = "proof_replays"

    jkt: Mapped[str] = mapped_column(String(64), primary_key=True)
    proof_jti: Mapped[str] = mapped_column(String(128), primary_key=True)
    iat_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    retain_until_epoch: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    claimed_at: Mapped[str] = mapped_column(String(40), nullable=False)


class IssuedTokenRecord(IdentityBase):
    __tablename__ = "issued_tokens"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    mandate_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    mandate_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    jkt: Mapped[str] = mapped_column(String(64), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    issued_at: Mapped[str] = mapped_column(String(40), nullable=False)
    expires_at: Mapped[str] = mapped_column(String(40), nullable=False)
    revoked_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


@dataclass(frozen=True)
class SqlAlchemyTx:
    """Member B `Tx` adapter wrapping an active SQLAlchemy session."""

    id: str
    session: Any


type SessionFactory = Callable[[], Any]


async def maybe_await(value: Any) -> Any:
    """Await `value` if it is awaitable, otherwise return it directly."""

    if inspect.isawaitable(value):
        return await value
    return value


def extract_session(tx: Tx) -> Any:
    """Extract the underlying SQLAlchemy session (sync or async) from an opaque `Tx`."""

    session = getattr(tx, "session", None)
    if session is not None and hasattr(session, "execute") and hasattr(session, "flush"):
        return session
    if hasattr(tx, "execute") and hasattr(tx, "flush"):
        return tx
    raise TypeError("Tx does not expose a SQLAlchemy session")


@asynccontextmanager
async def session_scope(
    session_factory: SessionFactory,
    tx: Tx | None = None,
    *,
    write: bool = False,
) -> AsyncIterator[Any]:
    """Yield either the caller-owned `Tx` session (never committing it) or a local session."""

    if tx is not None:
        yield extract_session(tx)
        return

    session = session_factory()
    if isinstance(session, Session):
        with session:
            if write:
                with session.begin():
                    yield session
            else:
                yield session
        return

    async with session:
        if write:
            async with session.begin():
                yield session
        else:
            yield session


__all__ = [
    "AgentRecord",
    "CustomerRecord",
    "HumanSessionRecord",
    "IdentityBase",
    "IssuedTokenRecord",
    "OperatorRecord",
    "ProofReplayRecord",
    "SessionFactory",
    "SqlAlchemyTx",
    "extract_session",
    "maybe_await",
    "session_scope",
]
