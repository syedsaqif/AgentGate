"""Member B tamper-evident audit chain, signature, and external checkpoint verifier."""

from __future__ import annotations

import hmac

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import select

from contracts.audit_models import VerifyFailure, VerifyReport
from contracts.common import AuditRef
from gateway.identity.crypto import (
    GENESIS_PREV_HASH,
    audit_entry_hash,
    verify_audit_entry_signature,
)
from gateway.identity.models import SessionFactory, maybe_await, session_scope

from .checkpoints import CheckpointError, CheckpointStore
from .models import AuditLogRecord


async def verify_audit_chain(
    *,
    session_factory: SessionFactory,
    public_key: Ed25519PublicKey,
    checkpoint_store: CheckpointStore | None = None,
    anchor: AuditRef | None = None,
) -> VerifyReport:
    """Verify sequence contiguity, hash links, recomputed hashes, Ed25519 sigs, and anchors."""

    async with session_scope(session_factory, write=False) as session:
        stmt = select(AuditLogRecord).order_by(AuditLogRecord.seq.asc())
        result = await maybe_await(session.execute(stmt))
        rows: list[AuditLogRecord] = list(result.scalars().all())

    entries_checked = 0
    expected_seq = 1
    expected_prev = GENESIS_PREV_HASH
    head_seq = 0
    head_hash = GENESIS_PREV_HASH
    hash_by_seq: dict[int, str] = {}

    for row in rows:
        entries_checked += 1

        if row.seq != expected_seq:
            return VerifyReport(
                valid=False,
                entries_checked=entries_checked,
                head_seq=head_seq,
                head_hash=head_hash,
                anchored_through_seq=0,
                first_failure=VerifyFailure(
                    seq=expected_seq,
                    reason=f"sequence_gap: expected seq {expected_seq}, found {row.seq}",
                ),
            )

        if not hmac.compare_digest(row.prev_hash, expected_prev):
            return VerifyReport(
                valid=False,
                entries_checked=entries_checked,
                head_seq=row.seq,
                head_hash=row.entry_hash,
                anchored_through_seq=0,
                first_failure=VerifyFailure(
                    seq=row.seq,
                    reason="prev_hash_mismatch",
                ),
            )

        try:
            canonical_entry = row.to_canonical_entry()
            recomputed_hash = audit_entry_hash(expected_prev, canonical_entry)
        except Exception as exc:
            return VerifyReport(
                valid=False,
                entries_checked=entries_checked,
                head_seq=row.seq,
                head_hash=row.entry_hash,
                anchored_through_seq=0,
                first_failure=VerifyFailure(
                    seq=row.seq,
                    reason=f"canonical_hash_error: {exc}",
                ),
            )

        if not hmac.compare_digest(row.entry_hash, recomputed_hash):
            return VerifyReport(
                valid=False,
                entries_checked=entries_checked,
                head_seq=row.seq,
                head_hash=row.entry_hash,
                anchored_through_seq=0,
                first_failure=VerifyFailure(
                    seq=row.seq,
                    reason="entry_hash_mismatch",
                ),
            )

        if not verify_audit_entry_signature(public_key, row.entry_hash, row.sig):
            return VerifyReport(
                valid=False,
                entries_checked=entries_checked,
                head_seq=row.seq,
                head_hash=row.entry_hash,
                anchored_through_seq=0,
                first_failure=VerifyFailure(
                    seq=row.seq,
                    reason="invalid_signature",
                ),
            )

        hash_by_seq[row.seq] = row.entry_hash
        head_seq = row.seq
        head_hash = row.entry_hash
        expected_prev = row.entry_hash
        expected_seq += 1

    # Gather all external anchors (independent checkpoint file + optional caller anchor).
    anchors: list[AuditRef] = []
    if checkpoint_store is not None:
        try:
            checkpoints = checkpoint_store.load_verified(public_key)
        except CheckpointError as exc:
            return VerifyReport(
                valid=False,
                entries_checked=entries_checked,
                head_seq=head_seq,
                head_hash=head_hash,
                anchored_through_seq=0,
                first_failure=VerifyFailure(
                    seq=max(1, head_seq),
                    reason=f"checkpoint_error: {exc}",
                ),
            )
        anchors.extend(AuditRef(seq=cp.seq, entry_hash=cp.entry_hash) for cp in checkpoints)

    if anchor is not None:
        anchors.append(anchor)

    anchors.sort(key=lambda item: item.seq)
    anchored_through_seq = 0
    for item in anchors:
        if head_seq < item.seq:
            return VerifyReport(
                valid=False,
                entries_checked=entries_checked,
                head_seq=head_seq,
                head_hash=head_hash,
                anchored_through_seq=anchored_through_seq,
                first_failure=VerifyFailure(
                    seq=item.seq,
                    reason=(
                        f"tail_truncation: head_seq {head_seq} is below anchored seq {item.seq}"
                    ),
                ),
            )
        chain_hash = hash_by_seq.get(item.seq)
        if chain_hash is None or not hmac.compare_digest(chain_hash, item.entry_hash):
            return VerifyReport(
                valid=False,
                entries_checked=entries_checked,
                head_seq=head_seq,
                head_hash=head_hash,
                anchored_through_seq=anchored_through_seq,
                first_failure=VerifyFailure(
                    seq=item.seq,
                    reason="anchor_hash_mismatch",
                ),
            )
        anchored_through_seq = max(anchored_through_seq, item.seq)

    return VerifyReport(
        valid=True,
        entries_checked=entries_checked,
        head_seq=head_seq,
        head_hash=head_hash,
        anchored_through_seq=anchored_through_seq,
        first_failure=None,
    )


__all__ = ["verify_audit_chain"]
