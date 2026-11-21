"""Member B safe disposable-copy tamper demonstration tool (S10a–d).

Safeguards:
- NEVER modifies the source database in place (`source.resolve() != target.resolve()` enforced).
- Copies the source SQLite database safely via SQLite's online backup API (WAL-safe).
- Drops append-only triggers ONLY on the disposable copy to simulate host-level DB tampering.
- Leaves original checkpoints untouched so tail truncation below the retained anchor is detected.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from sqlalchemy import select, text

from gateway.audit.models import AuditLogRecord
from gateway.identity.crypto import (
    GENESIS_PREV_HASH,
    audit_entry_hash,
    generate_ed25519_keypair,
    sign_audit_entry_hash,
)
from gateway.identity.schema import (
    create_identity_session_factory,
    create_sqlite_identity_engine,
)

TamperMode = Literal["edit", "delete", "rehash", "truncate"]


def _copy_sqlite_safely(source: Path, target: Path, *, overwrite: bool) -> None:
    if not source.exists():
        raise FileNotFoundError(f"Source database does not exist: {source}")
    if source.resolve() == target.resolve():
        raise ValueError("Refusing to tamper with source database in place; choose a separate copy")
    if target.exists():
        if not overwrite:
            raise FileExistsError(f"Refusing to overwrite existing target file: {target}")
        target.unlink()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{target}{suffix}")
            if sidecar.exists():
                sidecar.unlink()

    target.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    dst_conn = sqlite3.connect(target.as_posix())
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()


def create_tampered_copy(
    *,
    source_db: str | Path,
    target_db: str | Path,
    mode: TamperMode,
    seq: int | None = None,
    overwrite: bool = False,
) -> dict[str, object]:
    """Create a disposable copy of `source_db` and apply one S10 tamper variant to the copy."""

    src = Path(source_db)
    dst = Path(target_db)
    _copy_sqlite_safely(src, dst, overwrite=overwrite)

    engine = create_sqlite_identity_engine(f"sqlite:///{dst.as_posix()}")
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP TRIGGER IF EXISTS trg_audit_log_no_update"))
            conn.execute(text("DROP TRIGGER IF EXISTS trg_audit_log_no_delete"))

        session_factory = create_identity_session_factory(engine)
        with session_factory() as session:
            with session.begin():
                rows = list(
                    session.execute(
                        select(AuditLogRecord).order_by(AuditLogRecord.seq.asc())
                    )
                    .scalars()
                    .all()
                )
                if not rows:
                    raise ValueError("Cannot tamper with an empty audit_log copy")

                head_seq = rows[-1].seq
                if mode == "edit":
                    target_seq = seq if seq is not None else min(2, head_seq)
                    rec = session.get(AuditLogRecord, target_seq)
                    if rec is None:
                        raise ValueError(f"audit_log seq {target_seq} not found")
                    rec.decision = "DENY" if rec.decision != "DENY" else "ALLOW"
                elif mode == "delete":
                    target_seq = seq if seq is not None else max(1, head_seq // 2)
                    rec = session.get(AuditLogRecord, target_seq)
                    if rec is None:
                        raise ValueError(f"audit_log seq {target_seq} not found")
                    session.delete(rec)
                elif mode == "rehash":
                    target_seq = seq if seq is not None else min(2, head_seq)
                    rogue_key, _ = generate_ed25519_keypair()
                    prev = GENESIS_PREV_HASH
                    found = False
                    for row in rows:
                        if row.seq == target_seq:
                            found = True
                            row.decision = "DENY" if row.decision != "DENY" else "ALLOW"
                        row.prev_hash = prev
                        recomputed = audit_entry_hash(prev, row.to_canonical_entry())
                        row.entry_hash = recomputed
                        if row.seq >= target_seq:
                            row.sig = sign_audit_entry_hash(rogue_key, recomputed)
                        prev = recomputed
                    if not found:
                        raise ValueError(f"audit_log seq {target_seq} not found")
                elif mode == "truncate":
                    target_seq = seq if seq is not None else head_seq
                    if target_seq < 1 or target_seq > head_seq:
                        raise ValueError(f"invalid truncate seq {target_seq} for head {head_seq}")
                    session.execute(
                        text("DELETE FROM audit_log WHERE seq >= :target_seq"),
                        {"target_seq": target_seq},
                    )
                else:
                    raise ValueError(f"unsupported tamper mode: {mode!r}")

        return {
            "source_db": str(src),
            "target_db": str(dst),
            "mode": mode,
            "target_seq": target_seq,
        }
    finally:
        engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.tamper",
        description="Create a disposable tampered SQLite copy for S10a-d verification demos.",
    )
    parser.add_argument("--source", required=True, help="Path to source gateway SQLite DB.")
    parser.add_argument("--target", required=True, help="Path to disposable copy SQLite DB.")
    parser.add_argument(
        "--mode",
        required=True,
        choices=("edit", "delete", "rehash", "truncate"),
        help="S10 tamper variant to apply strictly to the disposable copy.",
    )
    parser.add_argument("--seq", type=int, default=None, help="Target sequence number.")
    parser.add_argument(
        "--overwrite", action="store_true", help="Overwrite existing disposable target copy."
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    summary = create_tampered_copy(
        source_db=args.source,
        target_db=args.target,
        mode=args.mode,
        seq=args.seq,
        overwrite=args.overwrite,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
