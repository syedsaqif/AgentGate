"""Member B atomic SQLite-backed DPoP proof replay cache."""

from __future__ import annotations

from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from contracts.common import Clock, Tx

from .crypto import format_utc_timestamp
from .models import ProofReplayRecord, SessionFactory, maybe_await, session_scope


class ProofReplayCache(Protocol):
    async def claim(
        self,
        *,
        jkt: str,
        proof_jti: str,
        iat_epoch: int,
        tolerance_seconds: int = 60,
        tx: Tx | None = None,
    ) -> bool: ...

    async def prune_expired(self, tx: Tx | None = None) -> int: ...


class SqlProofReplayCache:
    """Persistent atomic replay cache keyed by `(registered_jkt, proof_jti)`.

    Uses SQLite `BEGIN IMMEDIATE` + composite primary key + `ON CONFLICT DO NOTHING`
    so concurrent reuse of the same proof across threads or coroutines has strictly one winner.
    Entries are retained through at least `max(iat + tolerance, now + tolerance)` to cover
    the full proof acceptance window including future clock skew.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def claim(
        self,
        *,
        jkt: str,
        proof_jti: str,
        iat_epoch: int,
        tolerance_seconds: int = 60,
        tx: Tx | None = None,
    ) -> bool:
        """Atomically claim `(jkt, proof_jti)`; returns True only for the single winning caller."""

        now = self._clock.now()
        now_epoch = int(now.timestamp())
        # Retain through full future-skew window: a proof dated `now + tolerance` stays
        # valid until `iat + tolerance` (= `now + 2 * tolerance`).
        retain_until_epoch = max(
            iat_epoch + tolerance_seconds,
            now_epoch + tolerance_seconds,
        )
        claimed_at = format_utc_timestamp(now)

        async with session_scope(self._session_factory, tx, write=True) as session:
            # Prune only entries strictly past their retention horizon so active proofs
            # can never be evicted prematurely.
            await maybe_await(
                session.execute(
                    delete(ProofReplayRecord).where(
                        ProofReplayRecord.retain_until_epoch < now_epoch
                    )
                )
            )
            stmt = (
                sqlite_insert(ProofReplayRecord)
                .values(
                    jkt=jkt,
                    proof_jti=proof_jti,
                    iat_epoch=iat_epoch,
                    retain_until_epoch=retain_until_epoch,
                    claimed_at=claimed_at,
                )
                .on_conflict_do_nothing(index_elements=["jkt", "proof_jti"])
            )
            result = await maybe_await(session.execute(stmt))
            await maybe_await(session.flush())
            return bool(result.rowcount == 1)

    async def prune_expired(self, tx: Tx | None = None) -> int:
        """Delete replay rows whose retention window has strictly elapsed."""

        now_epoch = int(self._clock.now().timestamp())
        async with session_scope(self._session_factory, tx, write=True) as session:
            result = await maybe_await(
                session.execute(
                    delete(ProofReplayRecord).where(
                        ProofReplayRecord.retain_until_epoch < now_epoch
                    )
                )
            )
            await maybe_await(session.flush())
            return int(result.rowcount or 0)

    async def is_claimed(
        self, jkt: str, proof_jti: str, tx: Tx | None = None
    ) -> bool:
        """Check whether `(jkt, proof_jti)` currently has a retained replay record."""

        now_epoch = int(self._clock.now().timestamp())
        async with session_scope(self._session_factory, tx, write=False) as session:
            stmt = select(ProofReplayRecord.retain_until_epoch).where(
                ProofReplayRecord.jkt == jkt,
                ProofReplayRecord.proof_jti == proof_jti,
                ProofReplayRecord.retain_until_epoch >= now_epoch,
            )
            result = await maybe_await(session.execute(stmt))
            return result.scalar_one_or_none() is not None


__all__ = [
    "ProofReplayCache",
    "SqlProofReplayCache",
]
