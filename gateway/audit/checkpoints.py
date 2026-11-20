"""Member B independent append-only signed external checkpoint file."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from contracts.common import AuditRef, ContractModel
from gateway.identity.crypto import (
    canonical_json,
    ed25519_sign_b64url,
    ed25519_verify_b64url,
    format_utc_timestamp,
    public_key_thumbprint,
    validate_hex_sha256,
)


class CheckpointError(ValueError):
    """Raised when a checkpoint file is malformed or fails Ed25519 verification."""


class SignedCheckpoint(ContractModel):
    """Externally anchored signed audit checkpoint.

    Note: A local checkpoint file detects post-anchor tail truncation on the gateway DB,
    but does not protect against full host and signing-key compromise.
    """

    seq: int
    entry_hash: str
    created_at: str
    key_id: str
    sig: str


def _checkpoint_signing_bytes(payload: dict[str, Any]) -> bytes:
    return canonical_json(
        {
            "seq": payload["seq"],
            "entry_hash": payload["entry_hash"],
            "created_at": payload["created_at"],
            "key_id": payload["key_id"],
        }
    )


def create_signed_checkpoint(
    *,
    seq: int,
    entry_hash: str,
    created_at: datetime,
    signing_key: Ed25519PrivateKey,
) -> SignedCheckpoint:
    """Create an Ed25519-signed checkpoint for `(seq, entry_hash)`."""

    if seq < 1:
        raise CheckpointError("checkpoint seq must be >= 1")
    validate_hex_sha256(entry_hash, field_name="entry_hash")

    key_id = public_key_thumbprint(signing_key.public_key())
    created_iso = format_utc_timestamp(created_at)
    unsigned = {
        "seq": seq,
        "entry_hash": entry_hash,
        "created_at": created_iso,
        "key_id": key_id,
    }
    sig = ed25519_sign_b64url(signing_key, _checkpoint_signing_bytes(unsigned))
    return SignedCheckpoint(
        seq=seq,
        entry_hash=entry_hash,
        created_at=created_iso,
        key_id=key_id,
        sig=sig,
    )


def verify_signed_checkpoint(
    checkpoint: SignedCheckpoint,
    public_key: Ed25519PublicKey,
) -> bool:
    """Verify a checkpoint's Ed25519 signature and key thumbprint."""

    if checkpoint.seq < 1:
        return False
    try:
        validate_hex_sha256(checkpoint.entry_hash, field_name="entry_hash")
    except ValueError:
        return False
    expected_kid = public_key_thumbprint(public_key)
    if checkpoint.key_id != expected_kid:
        return False
    return ed25519_verify_b64url(
        public_key,
        _checkpoint_signing_bytes(checkpoint.model_dump(mode="json")),
        checkpoint.sig,
    )


class CheckpointStore:
    """Independent local append-only JSONL store for signed audit checkpoints."""

    def __init__(self, file_path: str | Path) -> None:
        self._path = Path(file_path)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def append_checkpoint(self, checkpoint: SignedCheckpoint) -> AuditRef:
        """Append a signed checkpoint line durably with flush + fsync."""

        line = canonical_json(checkpoint.model_dump(mode="json")).decode("utf-8") + "\n"
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
        return AuditRef(seq=checkpoint.seq, entry_hash=checkpoint.entry_hash)

    def load_verified(self, public_key: Ed25519PublicKey) -> tuple[SignedCheckpoint, ...]:
        """Load and cryptographically verify every checkpoint in the file."""

        if not self._path.exists():
            return ()

        checkpoints: list[SignedCheckpoint] = []
        last_seq = 0
        with self._lock:
            lines = self._path.read_text(encoding="utf-8").splitlines()

        for line_no, raw_line in enumerate(lines, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
                cp = SignedCheckpoint.model_validate(parsed)
            except Exception as exc:
                raise CheckpointError(
                    f"malformed checkpoint record at line {line_no}"
                ) from exc

            if not verify_signed_checkpoint(cp, public_key):
                raise CheckpointError(
                    f"invalid checkpoint signature at line {line_no} (seq={cp.seq})"
                )
            if cp.seq < last_seq:
                raise CheckpointError(
                    f"non-monotonic checkpoint seq at line {line_no}: {cp.seq} < {last_seq}"
                )
            last_seq = cp.seq
            checkpoints.append(cp)

        return tuple(checkpoints)

    def latest_anchor(self, public_key: Ed25519PublicKey) -> AuditRef | None:
        """Return the highest verified anchored `AuditRef`, or `None` if no checkpoints exist."""

        verified = self.load_verified(public_key)
        if not verified:
            return None
        latest = verified[-1]
        return AuditRef(seq=latest.seq, entry_hash=latest.entry_hash)


__all__ = [
    "CheckpointError",
    "CheckpointStore",
    "SignedCheckpoint",
    "create_signed_checkpoint",
    "verify_signed_checkpoint",
]
