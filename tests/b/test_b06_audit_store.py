"""B06 unit, security, concurrency, transaction, and tamper-detection tests for SqlAuditLog."""

from __future__ import annotations

import asyncio
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from contracts.audit_models import AuditEvent
from contracts.audit_ports import AuditLog
from contracts.common import ActionType, AuditRef
from contracts.reason_codes import ReasonCode
from gateway.audit.append import AuditWriteError, SqlAuditLog
from gateway.audit.checkpoints import CheckpointStore
from gateway.audit.models import AuditLogRecord
from gateway.audit.schema import create_audit_schema
from gateway.audit.verify import verify_audit_chain
from gateway.identity.crypto import (
    GENESIS_PREV_HASH,
    audit_entry_hash,
    generate_ed25519_keypair,
    public_key_to_jwk,
    sign_audit_entry_hash,
)
from gateway.identity.errors import ResourceNotFoundError
from gateway.identity.models import SqlAlchemyTx
from gateway.identity.registry import SqlAgentRegistry
from gateway.identity.schema import (
    create_identity_schema,
    create_identity_session_factory,
    create_sqlite_identity_engine,
)
from tests.support.platform import FrozenClock


def _make_shared_gateway_db(
    db_file: Path,
) -> tuple[Engine, sessionmaker[Session]]:
    engine = create_sqlite_identity_engine(f"sqlite:///{db_file.as_posix()}")
    create_identity_schema(engine)
    create_audit_schema(engine)
    return engine, create_identity_session_factory(engine)


def _make_event(
    clock: FrozenClock,
    *,
    event_type: str = "policy.decision",
    request_id: str | None = "req_1",
    agent_id: str | None = "agt_billpal",
    customer_id: str | None = "cust_001",
    decision: str | None = "ALLOW",
    reason_codes: tuple[str, ...] = (),
    safe_payload: dict[str, Any] | None = None,
) -> AuditEvent:
    return AuditEvent(
        event_type=event_type,
        request_id=request_id,
        customer_id=customer_id,
        agent_id=agent_id,
        mandate_id="mnd_demo_1",
        mandate_hash="sha256:" + ("a" * 64),
        decision=decision,
        reason_codes=reason_codes,
        safe_payload=safe_payload if safe_payload is not None else {"amount_paise": 185000},
        occurred_at=clock.now(),
    )


def _copy_db_without_triggers(src_db: Path, dst_db: Path) -> Engine:
    """Copy SQLite DB to an isolated tamper path and drop append-only triggers on the copy."""

    shutil.copy2(src_db, dst_db)
    engine = create_sqlite_identity_engine(f"sqlite:///{dst_db.as_posix()}")
    with engine.begin() as conn:
        conn.execute(text("DROP TRIGGER IF EXISTS trg_audit_log_no_update"))
        conn.execute(text("DROP TRIGGER IF EXISTS trg_audit_log_no_delete"))
    return engine


class TestAuditStoreAndCheckpoints:
    async def test_append_one_and_multiple_events_with_checkpoints_and_protocol(
        self, tmp_path: Path
    ) -> None:
        db_file = tmp_path / "gateway.db"
        cp_file = tmp_path / "audit_checkpoints.jsonl"
        engine, session_factory = _make_shared_gateway_db(db_file)
        clock = FrozenClock(datetime(2026, 11, 18, 16, 0, 0, tzinfo=UTC))
        audit_key, audit_pub = generate_ed25519_keypair()
        cp_store = CheckpointStore(cp_file)
        audit_log: AuditLog = SqlAuditLog(
            session_factory=session_factory,
            signing_key=audit_key,
            clock=clock,
            checkpoint_store=cp_store,
            checkpoint_interval=5,
        )

        try:
            # 1. Append single event and verify genesis link & signature.
            ref1 = await audit_log.append(_make_event(clock, request_id="req_1"))
            assert ref1.seq == 1
            report1 = await audit_log.verify()
            assert report1.valid is True
            assert report1.entries_checked == 1
            assert report1.head_seq == 1
            assert report1.head_hash == ref1.entry_hash
            assert report1.anchored_through_seq == 0
            assert report1.first_failure is None

            # 2. Append 5 more events -> auto-checkpoint triggers at seq=5; seq=6 succeeds after.
            refs = [ref1]
            for idx in range(2, 7):
                clock.advance(seconds=1)
                refs.append(await audit_log.append(_make_event(clock, request_id=f"req_{idx}")))

            assert [r.seq for r in refs] == [1, 2, 3, 4, 5, 6]
            report6 = await audit_log.verify()
            assert report6.valid is True
            assert report6.entries_checked == 6
            assert report6.head_seq == 6
            assert report6.head_hash == refs[-1].entry_hash
            # Valid entries after an older checkpoint (seq=5) keep chain valid.
            assert report6.anchored_through_seq == 5

            # 3. On-demand checkpoint advances anchor to head (seq=6).
            cp_ref = await audit_log.checkpoint()
            assert cp_ref == refs[-1]
            report_anchored = await audit_log.verify()
            assert report_anchored.valid is True
            assert report_anchored.anchored_through_seq == 6

            # Verify external checkpoint file has 2 verified checkpoints (seq 5 and seq 6).
            loaded_cps = cp_store.load_verified(audit_pub)
            assert [cp.seq for cp in loaded_cps] == [5, 6]
        finally:
            engine.dispose()

    async def test_live_append_only_triggers_block_update_and_delete(
        self, tmp_path: Path
    ) -> None:
        db_file = tmp_path / "gateway_triggers.db"
        engine, session_factory = _make_shared_gateway_db(db_file)
        clock = FrozenClock(datetime(2026, 11, 18, 16, 0, 0, tzinfo=UTC))
        audit_key, _ = generate_ed25519_keypair()
        audit_log = SqlAuditLog(
            session_factory=session_factory, signing_key=audit_key, clock=clock
        )

        try:
            await audit_log.append(_make_event(clock, request_id="req_1"))

            with pytest.raises(DBAPIError, match="append-only: UPDATE is forbidden"):
                with engine.begin() as conn:
                    conn.execute(text("UPDATE audit_log SET decision = 'DENY' WHERE seq = 1"))

            with pytest.raises(DBAPIError, match="append-only: DELETE is forbidden"):
                with engine.begin() as conn:
                    conn.execute(text("DELETE FROM audit_log WHERE seq = 1"))

            report = await audit_log.verify()
            assert report.valid is True
            assert report.entries_checked == 1
        finally:
            engine.dispose()


class TestAuditTamperDetection:
    async def _seed_five_events_and_checkpoint(
        self, tmp_path: Path
    ) -> tuple[Path, CheckpointStore, Ed25519PrivateKey, AuditRef]:
        db_file = tmp_path / "orig_gateway.db"
        cp_file = tmp_path / "checkpoints.jsonl"
        engine, session_factory = _make_shared_gateway_db(db_file)
        clock = FrozenClock(datetime(2026, 11, 18, 16, 0, 0, tzinfo=UTC))
        audit_key, _ = generate_ed25519_keypair()
        cp_store = CheckpointStore(cp_file)
        audit_log = SqlAuditLog(
            session_factory=session_factory,
            signing_key=audit_key,
            clock=clock,
            checkpoint_store=cp_store,
        )
        try:
            for idx in range(1, 6):
                clock.advance(seconds=1)
                await audit_log.append(_make_event(clock, request_id=f"req_{idx}"))
            head_anchor = await audit_log.checkpoint()
            assert head_anchor.seq == 5
        finally:
            engine.dispose()
        return db_file, cp_store, audit_key, head_anchor

    async def test_detects_modified_entry_middle_deletion_rehash_without_key_and_tail_truncation(
        self, tmp_path: Path
    ) -> None:
        orig_db, cp_store, audit_key, head_anchor = await self._seed_five_events_and_checkpoint(
            tmp_path
        )
        audit_pub = audit_key.public_key()

        # 1. Modify an event (content edit on seq 2) -> entry_hash_mismatch at seq 2.
        edit_db = tmp_path / "tamper_edit.db"
        edit_engine = _copy_db_without_triggers(orig_db, edit_db)
        try:
            with edit_engine.begin() as conn:
                conn.execute(text("UPDATE audit_log SET decision = 'DENY' WHERE seq = 2"))
            report_edit = await verify_audit_chain(
                session_factory=create_identity_session_factory(edit_engine),
                public_key=audit_pub,
                checkpoint_store=cp_store,
            )
            assert report_edit.valid is False
            assert report_edit.first_failure is not None
            assert report_edit.first_failure.seq == 2
            assert report_edit.first_failure.reason == "entry_hash_mismatch"
        finally:
            edit_engine.dispose()

        # 2. Delete a middle event (seq 3) -> sequence_gap at seq 3.
        del_db = tmp_path / "tamper_delete.db"
        del_engine = _copy_db_without_triggers(orig_db, del_db)
        try:
            with del_engine.begin() as conn:
                conn.execute(text("DELETE FROM audit_log WHERE seq = 3"))
            report_del = await verify_audit_chain(
                session_factory=create_identity_session_factory(del_engine),
                public_key=audit_pub,
                checkpoint_store=cp_store,
            )
            assert report_del.valid is False
            assert report_del.first_failure is not None
            assert report_del.first_failure.seq == 3
            assert report_del.first_failure.reason.startswith("sequence_gap")
        finally:
            del_engine.dispose()

        # 3. Modify row 2 and re-hash rows 2..5 WITHOUT the signing key (using rogue key).
        rehash_db = tmp_path / "tamper_rehash.db"
        rehash_engine = _copy_db_without_triggers(orig_db, rehash_db)
        rogue_key, _ = generate_ed25519_keypair()
        try:
            rehash_sf = create_identity_session_factory(rehash_engine)
            with rehash_sf() as session:
                with session.begin():
                    rows = list(
                        session.execute(
                            select(AuditLogRecord).order_by(AuditLogRecord.seq.asc())
                        )
                        .scalars()
                        .all()
                    )
                    prev = GENESIS_PREV_HASH
                    for rec in rows:
                        if rec.seq == 2:
                            rec.decision = "DENY"
                        rec.prev_hash = prev
                        new_hash = audit_entry_hash(prev, rec.to_canonical_entry())
                        rec.entry_hash = new_hash
                        if rec.seq >= 2:
                            # Attacker lacks real audit key and signs with rogue key instead.
                            rec.sig = sign_audit_entry_hash(rogue_key, new_hash)
                        prev = new_hash
            report_rehash = await verify_audit_chain(
                session_factory=rehash_sf,
                public_key=audit_pub,
                checkpoint_store=cp_store,
            )
            assert report_rehash.valid is False
            assert report_rehash.first_failure is not None
            assert report_rehash.first_failure.seq == 2
            assert report_rehash.first_failure.reason == "invalid_signature"
        finally:
            rehash_engine.dispose()

        # 4. Truncate tail below retained checkpoint (delete seq 4 and 5) -> tail_truncation at 5.
        trunc_db = tmp_path / "tamper_truncate.db"
        trunc_engine = _copy_db_without_triggers(orig_db, trunc_db)
        try:
            with trunc_engine.begin() as conn:
                conn.execute(text("DELETE FROM audit_log WHERE seq >= 4"))
            report_trunc = await verify_audit_chain(
                session_factory=create_identity_session_factory(trunc_engine),
                public_key=audit_pub,
                checkpoint_store=cp_store,
            )
            assert report_trunc.valid is False
            assert report_trunc.head_seq == 3
            assert report_trunc.first_failure is not None
            assert report_trunc.first_failure.seq == 5
            assert report_trunc.first_failure.reason.startswith("tail_truncation")

            # Also verify explicit anchor parameter works independently of checkpoint_store.
            report_explicit = await verify_audit_chain(
                session_factory=create_identity_session_factory(trunc_engine),
                public_key=audit_pub,
                anchor=head_anchor,
            )
            assert report_explicit.valid is False
            assert report_explicit.first_failure is not None
            assert report_explicit.first_failure.seq == 5
        finally:
            trunc_engine.dispose()


class TestAuditConcurrencyTransactionsAndRedaction:
    async def test_concurrent_appends_form_one_contiguous_valid_chain(
        self, tmp_path: Path
    ) -> None:
        db_file = tmp_path / "concurrent_audit.db"
        engine, session_factory = _make_shared_gateway_db(db_file)
        clock = FrozenClock(datetime(2026, 11, 18, 16, 0, 0, tzinfo=UTC))
        audit_key, _ = generate_ed25519_keypair()
        audit_log = SqlAuditLog(
            session_factory=session_factory, signing_key=audit_key, clock=clock
        )

        workers = 16
        barrier = threading.Barrier(workers)

        def _append_worker(idx: int) -> AuditRef:
            barrier.wait()
            return asyncio.run(
                audit_log.append(_make_event(clock, request_id=f"req_conc_{idx}"))
            )

        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_append_worker, idx) for idx in range(workers)]
                refs = [f.result() for f in futures]

            assert sorted(r.seq for r in refs) == list(range(1, workers + 1))
            assert len({r.entry_hash for r in refs}) == workers

            report = await audit_log.verify()
            assert report.valid is True
            assert report.entries_checked == workers
            assert report.head_seq == workers
        finally:
            engine.dispose()

    async def test_transaction_rollback_hides_event_and_checkpoint_and_failure_propagates(
        self, tmp_path: Path
    ) -> None:
        db_file = tmp_path / "shared_tx.db"
        cp_file = tmp_path / "tx_checkpoints.jsonl"
        engine, session_factory = _make_shared_gateway_db(db_file)
        clock = FrozenClock(datetime(2026, 11, 18, 16, 0, 0, tzinfo=UTC))
        audit_key, _ = generate_ed25519_keypair()
        cp_store = CheckpointStore(cp_file)
        audit_log = SqlAuditLog(
            session_factory=session_factory,
            signing_key=audit_key,
            clock=clock,
            checkpoint_store=cp_store,
            checkpoint_interval=1,
        )
        registry = SqlAgentRegistry(session_factory, clock, audit_log=audit_log)

        try:
            # 1. Rollback caller-owned tx -> neither agent, audit event, nor checkpoint persists!
            _, pub1 = generate_ed25519_keypair()
            with session_factory() as session:
                with session.begin():
                    tx = SqlAlchemyTx(id="tx_rollback_1", session=session)
                    await registry.register(
                        name="RolledBackAgent",
                        vendor="Vendor",
                        public_jwk=public_key_to_jwk(pub1),
                        max_scopes=[ActionType.BILL_PAY.value],
                        agent_id="agt_rb",
                        tx=tx,
                    )
                    session.rollback()

            with pytest.raises(ResourceNotFoundError):
                await registry.get("agt_rb")
            report_after_rb = await audit_log.verify()
            assert report_after_rb.valid is True
            assert report_after_rb.entries_checked == 0
            assert not cp_file.exists()

            # 2. Commit caller-owned tx -> agent, audit event, and post-commit checkpoint persist.
            with session_factory() as session:
                with session.begin():
                    tx = SqlAlchemyTx(id="tx_commit_1", session=session)
                    await registry.register(
                        name="CommittedAgent",
                        vendor="Vendor",
                        public_jwk=public_key_to_jwk(pub1),
                        max_scopes=[ActionType.BILL_PAY.value],
                        agent_id="agt_ok",
                        tx=tx,
                    )

            assert (await registry.get("agt_ok")).agent_id == "agt_ok"
            report_after_commit = await audit_log.verify()
            assert report_after_commit.valid is True
            assert report_after_commit.entries_checked == 1
            assert report_after_commit.anchored_through_seq == 1

            # 3. Simulate DB audit failure -> AuditWriteError propagates & blocks state mutation.
            with engine.begin() as conn:
                conn.execute(text("DROP TABLE audit_log"))

            _, pub2 = generate_ed25519_keypair()
            with pytest.raises(AuditWriteError) as write_exc:
                await registry.register(
                    name="NoAuditNoState",
                    vendor="Vendor",
                    public_jwk=public_key_to_jwk(pub2),
                    max_scopes=[ActionType.BILL_PAY.value],
                    agent_id="agt_blocked",
                )
            assert write_exc.value.code is ReasonCode.E_AUDIT_WRITE_FAILED
            assert write_exc.value.http_status == 503
            with pytest.raises(ResourceNotFoundError):
                await registry.get("agt_blocked")
        finally:
            engine.dispose()

    async def test_no_pin_private_key_or_raw_token_stored_in_audit_records(
        self, tmp_path: Path
    ) -> None:
        db_file = tmp_path / "redaction.db"
        engine, session_factory = _make_shared_gateway_db(db_file)
        clock = FrozenClock(datetime(2026, 11, 18, 16, 0, 0, tzinfo=UTC))
        audit_key, _ = generate_ed25519_keypair()
        audit_log = SqlAuditLog(
            session_factory=session_factory, signing_key=audit_key, clock=clock
        )

        raw_jwt = "eyJhbGciOiJFZERTQSJ9.eyJzdWIiOiJjdXN0XzAwMSJ9.signaturebytes1234567890"
        sample_proof = "eyJ0eXAiOiJkcG9wK2p3dCJ9.eyJodG0iOiJQT1NUIn0.proofsigbytes1234567890"
        pem_secret = (
            "-----BEGIN PRIVATE KEY-----\n"
            "MC4CAQAwBQYDK2VwBCIEINotARealKey\n"
            "-----END PRIVATE KEY-----"
        )

        try:
            event = _make_event(
                clock,
                safe_payload={
                    "pin": "4821",
                    "password": "ops-secret-password",
                    "access_token": raw_jwt,
                    "session_token": raw_jwt,
                    "private_key": pem_secret,
                    "jwk": {
                        "kty": "OKP",
                        "crv": "Ed25519",
                        "x": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo",
                        "d": "PRIVATE_JWK_D_SECRET",
                    },
                    "accidental_auth_header": f"DPoP {raw_jwt}",
                    "accidental_raw_jwt": raw_jwt,
                    "proof_jws": sample_proof,
                },
            )
            await audit_log.append(event)

            with session_factory() as session:
                stored_payload = str(
                    session.execute(
                        text("SELECT safe_payload_json FROM audit_log WHERE seq = 1")
                    ).scalar_one()
                )

            for forbidden in (
                "4821",
                "ops-secret-password",
                raw_jwt,
                "BEGIN PRIVATE KEY",
                "PRIVATE_JWK_D_SECRET",
            ):
                assert forbidden not in stored_payload

            # Legitimate proof_jws evidence field is preserved for offline verification.
            assert sample_proof in stored_payload

            # Entry hash and signature remain completely valid over the sanitized record.
            report = await audit_log.verify()
            assert report.valid is True
            assert report.entries_checked == 1

            # Oversize arbitrary payload (> 16 KiB) rejected with AuditWriteError.
            with pytest.raises(AuditWriteError):
                await audit_log.append(
                    _make_event(
                        clock,
                        safe_payload={f"k{i}": i for i in range(2500)},
                    )
                )
        finally:
            engine.dispose()
