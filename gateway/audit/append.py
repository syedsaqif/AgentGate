"""Member B transactional, serialized, Ed25519-signed append-only audit store."""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Mapping
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from sqlalchemy import event as sa_event
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from contracts.audit_models import AuditEvent, EvidenceBundle, VerifyReport
from contracts.common import AuditRef, Clock, OpaqueId, Page, Tx
from contracts.reason_codes import HTTP_STATUS_BY_CODE, ReasonCode
from gateway.identity.crypto import (
    GENESIS_PREV_HASH,
    audit_entry_hash,
    b64url_decode,
    b64url_encode,
    canonical_json,
    format_utc_timestamp,
    sha256_prefixed,
    sign_audit_entry_hash,
)
from gateway.identity.errors import ValidationDomainError
from gateway.identity.models import SessionFactory, maybe_await, session_scope

from .checkpoints import CheckpointStore, create_signed_checkpoint
from .evidence import assemble_evidence_bundle
from .models import AuditLogRecord
from .verify import verify_audit_chain

DEFAULT_CHECKPOINT_INTERVAL = 50
_MAX_PAYLOAD_BYTES = 16_384
_MAX_FIELD_CHARS = 1024
_MAX_PROOF_JWS_CHARS = 8192

_SENSITIVE_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "pin",
        "pin_hash",
        "password",
        "secret",
        "secret_hash",
        "private_key",
        "private_key_pem",
        "access_token",
        "session_token",
        "raw_token",
        "authorization",
        "d",
    }
)
_REDACTED_VALUE = "[REDACTED]"
_JWT_LIKE_RE = re.compile(r"^[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}$")


class AuditWriteError(RuntimeError):
    """Raised when appending or checkpointing an audit record fails (503 E_AUDIT_WRITE_FAILED)."""

    def __init__(self, message: str = "Audit write failed.") -> None:
        super().__init__(message)
        self.code = ReasonCode.E_AUDIT_WRITE_FAILED
        self.http_status = HTTP_STATUS_BY_CODE[ReasonCode.E_AUDIT_WRITE_FAILED]
        self.message = message


def _looks_like_secret_string(value: str, *, key_name: str | None = None) -> bool:
    if "PRIVATE KEY" in value and "-----BEGIN" in value:
        return True
    if value.startswith("$argon2"):
        return True
    lowered = value.lower()
    if lowered.startswith(("bearer ", "dpop ")):
        return True
    # Only allow compact JWS under the explicit `proof_jws` evidence field (never access tokens).
    if key_name != "proof_jws" and _JWT_LIKE_RE.fullmatch(value.strip()):
        return True
    return False


def sanitize_audit_value(value: Any, *, key_name: str | None = None) -> Any:
    """Recursively redact sensitive keys/values and cap oversize arbitrary text fields."""

    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for k, v in value.items():
            key_str = str(k)
            if key_str.lower() in _SENSITIVE_FIELD_NAMES:
                sanitized[key_str] = _REDACTED_VALUE
            else:
                sanitized[key_str] = sanitize_audit_value(v, key_name=key_str)
        return sanitized

    if isinstance(value, list | tuple):
        return [sanitize_audit_value(item, key_name=key_name) for item in value]

    if isinstance(value, str):
        if _looks_like_secret_string(value, key_name=key_name):
            return _REDACTED_VALUE
        max_len = _MAX_PROOF_JWS_CHARS if key_name == "proof_jws" else _MAX_FIELD_CHARS
        if len(value) > max_len:
            digest = sha256_prefixed(value.encode("utf-8"))
            return {"truncated": value[:128], "sha256": digest, "original_length": len(value)}
        return value

    return value


def _sanitize_payload_dict(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    cleaned = sanitize_audit_value(dict(payload))
    assert isinstance(cleaned, dict)
    encoded_size = len(canonical_json(cleaned))
    if encoded_size > _MAX_PAYLOAD_BYTES:
        raise AuditWriteError(
            f"Audit safe_payload exceeds maximum allowed size ({_MAX_PAYLOAD_BYTES} bytes)."
        )
    return cleaned


class SqlAuditLog:
    """Persistent, hash-chained, Ed25519-signed audit store conforming to `AuditLog`.

    Uses the shared gateway SQLite WAL database (`audit_log` table with append-only triggers).
    Joins caller-owned `Tx` transactions without committing them; standalone writes serialize
    cleanly via `BEGIN IMMEDIATE` + lock serialization so concurrent writers form one
    contiguous valid chain.
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        signing_key: Ed25519PrivateKey,
        clock: Clock,
        checkpoint_store: CheckpointStore | None = None,
        checkpoint_interval: int = DEFAULT_CHECKPOINT_INTERVAL,
        max_busy_retries: int = 8,
    ) -> None:
        if checkpoint_interval < 1:
            raise ValueError("checkpoint_interval must be >= 1")
        self._session_factory = session_factory
        self._signing_key = signing_key
        self._public_key: Ed25519PublicKey = signing_key.public_key()
        self._clock = clock
        self._checkpoint_store = checkpoint_store
        self._checkpoint_interval = checkpoint_interval
        self._max_busy_retries = max_busy_retries
        self._write_lock = threading.Lock()

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self._public_key

    @property
    def checkpoint_store(self) -> CheckpointStore | None:
        return self._checkpoint_store

    def _write_checkpoint_sync(self, seq: int, entry_hash: str) -> AuditRef | None:
        if self._checkpoint_store is None:
            return None
        cp = create_signed_checkpoint(
            seq=seq,
            entry_hash=entry_hash,
            created_at=self._clock.now(),
            signing_key=self._signing_key,
        )
        return self._checkpoint_store.append_checkpoint(cp)

    def _schedule_post_commit_checkpoint(
        self, session: Any, seq: int, entry_hash: str
    ) -> None:
        """Write an external checkpoint only if the caller-owned transaction actually commits."""

        if self._checkpoint_store is None:
            return
        sync_session = getattr(session, "sync_session", session)

        def _on_commit(_sess: Any) -> None:
            sa_event.remove(sync_session, "after_rollback", _on_rollback)
            self._write_checkpoint_sync(seq, entry_hash)

        def _on_rollback(_sess: Any) -> None:
            sa_event.remove(sync_session, "after_commit", _on_commit)

        sa_event.listen(sync_session, "after_commit", _on_commit, once=True)
        sa_event.listen(sync_session, "after_rollback", _on_rollback, once=True)

    async def _append_in_session(
        self, session: Any, event_dto: AuditEvent
    ) -> tuple[AuditRef, bool]:
        head_stmt = (
            select(AuditLogRecord.seq, AuditLogRecord.entry_hash)
            .order_by(AuditLogRecord.seq.desc())
            .limit(1)
        )
        head_res = await maybe_await(session.execute(head_stmt))
        head_row = head_res.first()

        if head_row is None:
            next_seq = 1
            prev_hash = GENESIS_PREV_HASH
        else:
            next_seq = int(head_row[0]) + 1
            prev_hash = str(head_row[1])

        safe_payload = _sanitize_payload_dict(event_dto.safe_payload)
        safe_risk = (
            _sanitize_payload_dict(event_dto.risk) if event_dto.risk is not None else None
        )
        occurred_iso = format_utc_timestamp(event_dto.occurred_at)
        reason_codes = list(event_dto.reason_codes)

        canonical_entry: dict[str, Any] = {
            "seq": next_seq,
            "prev_hash": prev_hash,
            "event_type": event_dto.event_type,
            "request_id": event_dto.request_id,
            "correlation_id": event_dto.correlation_id,
            "customer_id": event_dto.customer_id,
            "agent_id": event_dto.agent_id,
            "mandate_id": event_dto.mandate_id,
            "mandate_hash": event_dto.mandate_hash,
            "decision": event_dto.decision,
            "reason_codes": reason_codes,
            "risk": safe_risk,
            "safe_payload": safe_payload,
            "occurred_at": occurred_iso,
        }
        entry_hash = audit_entry_hash(prev_hash, canonical_entry)
        sig = sign_audit_entry_hash(self._signing_key, entry_hash)

        record = AuditLogRecord(
            seq=next_seq,
            prev_hash=prev_hash,
            event_type=event_dto.event_type,
            request_id=event_dto.request_id,
            correlation_id=event_dto.correlation_id,
            customer_id=event_dto.customer_id,
            agent_id=event_dto.agent_id,
            mandate_id=event_dto.mandate_id,
            mandate_hash=event_dto.mandate_hash,
            decision=event_dto.decision,
            reason_codes_json=json.dumps(reason_codes),
            risk_json=json.dumps(safe_risk) if safe_risk is not None else None,
            safe_payload_json=json.dumps(safe_payload),
            occurred_at=occurred_iso,
            entry_hash=entry_hash,
            sig=sig,
        )
        session.add(record)
        await maybe_await(session.flush())

        should_checkpoint = (
            self._checkpoint_store is not None
            and (next_seq % self._checkpoint_interval == 0)
        )
        return AuditRef(seq=next_seq, entry_hash=entry_hash), should_checkpoint

    async def append(self, event: AuditEvent, tx: Tx | None = None) -> AuditRef:
        """Append a signed, hash-chained audit entry; propagates `AuditWriteError` on failure."""

        if tx is not None:
            try:
                async with session_scope(self._session_factory, tx, write=True) as session:
                    ref, should_cp = await self._append_in_session(session, event)
                    if should_cp:
                        self._schedule_post_commit_checkpoint(session, ref.seq, ref.entry_hash)
                    return ref
            except AuditWriteError:
                raise
            except Exception as exc:
                raise AuditWriteError(f"Audit append failed inside transaction: {exc}") from exc

        # Standalone append: serialize in-process writers and retry bounded SQLite busy locks.
        last_exc: Exception | None = None
        for attempt in range(self._max_busy_retries):
            try:
                with self._write_lock:
                    async with session_scope(self._session_factory, write=True) as session:
                        ref, should_cp = await self._append_in_session(session, event)
                # Write external checkpoint strictly AFTER DB commit succeeds.
                if should_cp:
                    try:
                        self._write_checkpoint_sync(ref.seq, ref.entry_hash)
                    except Exception as exc:
                        raise AuditWriteError(f"Audit checkpoint write failed: {exc}") from exc
                return ref
            except OperationalError as exc:
                last_exc = exc
                if "locked" in str(exc).lower() and attempt + 1 < self._max_busy_retries:
                    await asyncio.sleep(0.01 * (attempt + 1))
                    continue
                raise AuditWriteError(f"Audit append database error: {exc}") from exc
            except AuditWriteError:
                raise
            except Exception as exc:
                raise AuditWriteError(f"Audit append failed: {exc}") from exc

        raise AuditWriteError(f"Audit append failed after busy retries: {last_exc}")

    async def verify(self, anchor: AuditRef | None = None) -> VerifyReport:
        """Verify full chain integrity, Ed25519 signatures, and checkpoint/anchor coverage."""

        return await verify_audit_chain(
            session_factory=self._session_factory,
            public_key=self._public_key,
            checkpoint_store=self._checkpoint_store,
            anchor=anchor,
        )

    async def checkpoint(self) -> AuditRef:
        """Persist an on-demand signed external checkpoint at the current committed chain head."""

        if self._checkpoint_store is None:
            raise AuditWriteError("No checkpoint store is configured for this audit log.")

        async with session_scope(self._session_factory, write=False) as session:
            head_stmt = (
                select(AuditLogRecord.seq, AuditLogRecord.entry_hash)
                .order_by(AuditLogRecord.seq.desc())
                .limit(1)
            )
            res = await maybe_await(session.execute(head_stmt))
            row = res.first()

        if row is None:
            raise AuditWriteError("Cannot create checkpoint on an empty audit log.")

        try:
            ref = self._write_checkpoint_sync(int(row[0]), str(row[1]))
            assert ref is not None
            return ref
        except Exception as exc:
            raise AuditWriteError(f"Failed to persist audit checkpoint: {exc}") from exc

    async def evidence(
        self, request_id: OpaqueId, *, anchor: AuditRef | None = None
    ) -> EvidenceBundle:
        """Assemble recorded dispute `EvidenceBundle` from immutable audit snapshots."""

        return await assemble_evidence_bundle(
            session_factory=self._session_factory,
            request_id=request_id,
            public_key=self._public_key,
            checkpoint_store=self._checkpoint_store,
            anchor=anchor,
        )

    async def list_entries(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        event_type: str | None = None,
        request_id: str | None = None,
        customer_id: str | None = None,
        agent_id: str | None = None,
    ) -> Page:
        """Return a bounded, redacted, cursor-paginated `Page` of audit log entries."""

        if limit < 1 or limit > 100:
            raise ValidationDomainError("limit must be between 1 and 100")

        after_seq: int | None = None
        if cursor is not None:
            try:
                decoded = b64url_decode(cursor).decode("ascii")
                after_seq = int(decoded)
                if after_seq < 1:
                    raise ValueError("cursor seq must be >= 1")
            except Exception as exc:
                raise ValidationDomainError("invalid audit pagination cursor") from exc

        async with session_scope(self._session_factory, write=False) as session:
            stmt = select(AuditLogRecord).order_by(AuditLogRecord.seq.asc())
            if after_seq is not None:
                stmt = stmt.where(AuditLogRecord.seq > after_seq)
            if event_type is not None:
                stmt = stmt.where(AuditLogRecord.event_type == event_type)
            if request_id is not None:
                stmt = stmt.where(AuditLogRecord.request_id == request_id)
            if customer_id is not None:
                stmt = stmt.where(AuditLogRecord.customer_id == customer_id)
            if agent_id is not None:
                stmt = stmt.where(AuditLogRecord.agent_id == agent_id)
            stmt = stmt.limit(limit + 1)
            result = await maybe_await(session.execute(stmt))
            records: list[AuditLogRecord] = list(result.scalars().all())

        has_more = len(records) > limit
        page_rows = records[:limit]
        items = tuple(
            {
                **row.to_canonical_entry(),
                "entry_hash": row.entry_hash,
                "sig": row.sig,
            }
            for row in page_rows
        )
        next_cursor = (
            b64url_encode(str(page_rows[-1].seq).encode("ascii")) if has_more else None
        )
        return Page(items=items, next_cursor=next_cursor)


__all__ = [
    "DEFAULT_CHECKPOINT_INTERVAL",
    "AuditWriteError",
    "SqlAuditLog",
    "sanitize_audit_value",
]
