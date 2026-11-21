"""The independent witness accepts extensions and rejects alternative audit history."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.orm import sessionmaker

from contracts.audit_models import AuditEvent
from gateway.audit.append import SqlAuditLog
from gateway.audit.dispute import ProofError, Trust
from gateway.audit.schema import create_audit_schema
from gateway.identity.crypto import public_key_to_jwk, serialize_ed25519_private_key_pem
from gateway.identity.schema import create_sqlite_identity_engine
from gateway.platform.clock import SystemClock
from tools.audit_witness import publish


@pytest.mark.asyncio
async def test_witness_journal_refuses_rollback_and_a_validly_signed_fork(tmp_path: Path) -> None:
    audit_key = Ed25519PrivateKey.generate()
    witness_key = Ed25519PrivateKey.generate()
    witness_pem = tmp_path / "witness.pem"
    witness_pem.write_bytes(serialize_ed25519_private_key_pem(witness_key))
    trust = Trust.model_validate({
        "log_id": "bank-log",
        "audit_key": public_key_to_jwk(audit_key.public_key()),
        "witness_key": public_key_to_jwk(witness_key.public_key()),
    })
    journal = tmp_path / "independent.db"

    async def populate(db: Path, event_type: str) -> SqlAuditLog:
        engine = create_sqlite_identity_engine(f"sqlite:///{db.as_posix()}")
        create_audit_schema(engine)
        log = SqlAuditLog(
            session_factory=sessionmaker(engine), signing_key=audit_key, clock=SystemClock()
        )
        await log.append(AuditEvent(event_type=event_type, occurred_at=datetime.now(UTC)))
        return log

    source = tmp_path / "gateway.db"
    log = await populate(source, "first")
    first = publish(database=source, trust=trust, key_file=witness_pem, journal=journal)
    assert first.seq == 1
    assert first.verify(witness_key.public_key())
    assert publish(database=source, trust=trust, key_file=witness_pem, journal=journal) == first

    await log.append(AuditEvent(event_type="second", occurred_at=datetime.now(UTC)))
    second = publish(database=source, trust=trust, key_file=witness_pem, journal=journal)
    assert second.seq == 2
    assert second.verify(witness_key.public_key())
    assert second.log_id == "bank-log"
    with pytest.raises(ProofError, match="rollback"):
        publish(
            database=source, trust=trust, key_file=witness_pem, journal=journal, through_sequence=1
        )

    competing = tmp_path / "competing.db"
    fork = await populate(competing, "different first")
    await fork.append(AuditEvent(event_type="second", occurred_at=datetime.now(UTC)))
    with pytest.raises(ProofError, match="conflicts"):
        publish(database=competing, trust=trust, key_file=witness_pem, journal=journal)

    with closing(sqlite3.connect(journal)) as state:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            state.execute("DELETE FROM checkpoints")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            state.execute("UPDATE checkpoints SET seal = 'x'")
        assert state.execute("SELECT count(*) FROM checkpoints").fetchone() == (2,)
