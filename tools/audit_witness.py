"""Offline operator CLI for an independent dispute checkpoint witness."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from gateway.audit.dispute import (
    MAX_PREFIX,
    PrefixSeal,
    ProofError,
    Trust,
    checked_links,
    matches_prefix,
    seal_prefix,
)
from gateway.audit.models import AuditLogRecord
from gateway.identity.crypto import (
    canonical_json,
    format_utc_timestamp,
    load_ed25519_private_key_pem,
    public_key_to_jwk,
    sha256_hex,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoints (
    seq INTEGER PRIMARY KEY, trust_hash TEXT NOT NULL, seal TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS prevent_checkpoint_update BEFORE UPDATE ON checkpoints
BEGIN SELECT RAISE(ABORT, 'Witness journal is append-only'); END;
CREATE TRIGGER IF NOT EXISTS prevent_checkpoint_delete BEFORE DELETE ON checkpoints
BEGIN SELECT RAISE(ABORT, 'Witness journal is append-only'); END;
CREATE TRIGGER IF NOT EXISTS prevent_checkpoint_replacement BEFORE INSERT ON checkpoints
WHEN NEW.seq <= (SELECT MAX(seq) FROM checkpoints)
BEGIN SELECT RAISE(ABORT, 'Witness journal requires advancing sequences'); END;
"""


def publish(
    *, database: Path, trust: Trust, key_file: Path, journal: Path,
    through_sequence: int = MAX_PREFIX,
) -> PrefixSeal:
    """Verify and durably accept one extension of the last witnessed audit prefix."""
    trust.validate_keys()
    if not 1 <= through_sequence <= MAX_PREFIX:
        raise ProofError(f"Choose a publication sequence between 1 and {MAX_PREFIX}.")
    key = load_ed25519_private_key_pem(key_file.read_bytes())
    if public_key_to_jwk(key.public_key()) != trust.witness_key.model_dump():
        raise ProofError("The witness key differs from the registered key.")
    if database.resolve() == journal.resolve():
        raise ProofError("The witness journal must be separate from the audit database.")

    with closing(sqlite3.connect(journal, timeout=30)) as state:
        state.executescript(_SCHEMA)
        state.execute("PRAGMA synchronous = FULL")
        state.execute("BEGIN IMMEDIATE")
        with state:
            with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    "SELECT * FROM audit_log WHERE seq <= ? ORDER BY seq", (through_sequence,)
                )
                links = checked_links(
                    (AuditLogRecord(**dict(row)) for row in rows), trust.audit_key.load()
                )

            trust_hash = sha256_hex(canonical_json(trust.model_dump()))
            latest = state.execute(
                "SELECT seq, trust_hash, seal FROM checkpoints ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            if latest is not None:
                seq, stored_trust, encoded = latest
                previous = PrefixSeal.model_validate_json(encoded)
                if (
                    stored_trust != trust_hash or previous.seq != seq
                    or previous.purpose != "witness-checkpoint"
                    or previous.log_id != trust.log_id
                    or not previous.verify(trust.witness_key.load())
                ):
                    raise ProofError("The journal does not match the registered log and keys.")
                if len(links) < seq:
                    raise ProofError("Checkpoint rollback rejected.")
                if not matches_prefix(previous, links):
                    raise ProofError("Checkpoint conflicts with previously witnessed history.")
                if len(links) == seq:
                    return previous

            seal = seal_prefix(
                key=key, purpose="witness-checkpoint", log_id=trust.log_id,
                published_at=format_utc_timestamp(datetime.now(UTC)), links=links,
            )
            state.execute(
                "INSERT INTO checkpoints VALUES (?, ?, ?)",
                (seal.seq, trust_hash, canonical_json(seal.model_dump()).decode()),
            )
            return seal


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser("register")
    register.add_argument("--log-id", required=True)
    register.add_argument("--audit-public", type=Path, required=True)
    register.add_argument("--witness-key", type=Path, required=True)
    register.add_argument("--trust", type=Path, required=True)

    checkpoint = commands.add_parser("checkpoint")
    for name in ("trust", "audit-db", "witness-key", "journal", "output"):
        checkpoint.add_argument(f"--{name}", type=Path, required=True)
    checkpoint.add_argument("--through-sequence", type=int, default=MAX_PREFIX)
    args = parser.parse_args()

    try:
        if args.command == "register":
            trust = Trust.model_validate({
                "log_id": args.log_id,
                "audit_key": json.loads(args.audit_public.read_bytes())["public_jwk"],
                "witness_key": public_key_to_jwk(
                    load_ed25519_private_key_pem(args.witness_key.read_bytes()).public_key()
                ),
            })
            trust.validate_keys()
            data = canonical_json(trust.model_dump())
            with args.trust.open("xb") as out:
                out.write(data)
            print(f"Trust manifest SHA-256 {sha256_hex(data)}")
        else:
            trust = Trust.model_validate_json(args.trust.read_bytes())
            seal = publish(
                database=args.audit_db, trust=trust, key_file=args.witness_key,
                journal=args.journal, through_sequence=args.through_sequence,
            )
            data = canonical_json(seal.model_dump())
            try:
                with args.output.open("xb") as out:
                    out.write(data)
            except FileExistsError:
                if args.output.read_bytes() != data:
                    raise
            print(f"Witness checkpoint published at sequence {seal.seq}")
        return 0
    except (OSError, ValueError, sqlite3.Error, KeyError) as exc:
        message = str(exc) if isinstance(exc, ProofError) else "Witness input or storage error."
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
