"""B07 unit, HTTP route, evidence bundle, and safe tamper-copy tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from agent_sdk import build_dpop_proof, generate_agent_keypair
from contracts.audit_models import AuditEvent, SignedRequestEvidence
from contracts.canonical import action_hash, canonical_json, sha256_base64url
from contracts.common import ActionType, HumanRole
from contracts.core_models import (
    ActionParams,
    BankResult,
    BankStatus,
    CanonicalAction,
    Grant,
    Intent,
    MandateSnapshot,
    MandateStatus,
    MandateTerms,
    StepUpRules,
)
from gateway.audit.append import SqlAuditLog
from gateway.audit.checkpoints import CheckpointStore
from gateway.audit.evidence import build_request_evidence_payload
from gateway.audit.routes import build_audit_router, install_audit_error_handlers
from gateway.audit.schema import create_audit_schema
from gateway.audit.verify import verify_audit_chain
from gateway.identity.crypto import (
    ed25519_verify_b64url,
    generate_ed25519_keypair,
    jwk_to_public_key,
    mandate_hash,
    sign_prefixed_sha256,
)
from gateway.identity.human_auth import HumanSessionKeyring, SqlHumanAuthService
from gateway.identity.registry import SqlAgentRegistry
from gateway.identity.routes import build_identity_router, install_identity_error_handlers
from gateway.identity.schema import (
    create_identity_schema,
    create_identity_session_factory,
    create_sqlite_identity_engine,
)
from tests.support.platform import FrozenClock
from tools.tamper import create_tampered_copy
from tools.tamper import main as tamper_cli_main


def _build_sample_mandate(now: datetime) -> MandateSnapshot:
    terms = MandateTerms(
        mandate_id="mnd_s11_1",
        version=1,
        customer_id="cust_001",
        agent_id="agt_billpal",
        valid_from=now - timedelta(minutes=5),
        valid_until=now + timedelta(hours=1),
        grants=(
            Grant(
                grant_id="grt_1",
                action=ActionType.BILL_PAY,
                account_ids=("ACC-001",),
                payee_ids=("BILL-ELEC",),
                max_per_txn_paise=500000,
                max_total_paise=1000000,
                max_count=5,
                category="utilities.electricity",
            ),
        ),
        step_up=StepUpRules(amount_above_paise=300000),
        risk_profile="balanced",
    )
    m_hash = mandate_hash(terms)
    mandate_key, _ = generate_ed25519_keypair()
    return MandateSnapshot(
        terms=terms,
        status=MandateStatus.ACTIVE,
        mandate_hash=m_hash,
        sig=sign_prefixed_sha256(mandate_key, m_hash),
        key_id="mandate-k1",
    )


class TestEvidenceAndAuditRoutes:
    async def test_s11_evidence_bundle_assembly_and_ops_only_audit_routes(
        self, tmp_path: Path
    ) -> None:
        db_file = tmp_path / "b07_gateway.db"
        cp_file = tmp_path / "b07_checkpoints.jsonl"
        engine = create_sqlite_identity_engine(f"sqlite:///{db_file.as_posix()}")
        create_identity_schema(engine)
        create_audit_schema(engine)
        session_factory = create_identity_session_factory(engine)
        clock = FrozenClock(datetime(2026, 11, 18, 17, 0, 0, tzinfo=UTC))
        audit_key, _ = generate_ed25519_keypair()
        cp_store = CheckpointStore(cp_file)
        audit_log = SqlAuditLog(
            session_factory=session_factory,
            signing_key=audit_key,
            clock=clock,
            checkpoint_store=cp_store,
        )
        registry = SqlAgentRegistry(session_factory, clock, audit_log=audit_log)
        human_auth = SqlHumanAuthService(
            session_factory, clock, HumanSessionKeyring.generate(), audit_log=audit_log
        )

        try:
            await human_auth.seed_customer(
                customer_id="cust_001", display_name="Demo", pin="4821"
            )
            await human_auth.seed_operator(
                operator_id="ops_001",
                display_name="Ops",
                role=HumanRole.OPS,
                password="ops-password-1",
            )
            await human_auth.seed_operator(
                operator_id="reg_001",
                display_name="Reg",
                role=HumanRole.REGISTRAR,
                password="reg-password-1",
            )

            agent_kp = generate_agent_keypair()
            agent = await registry.register(
                name="BillPal",
                vendor="FinBot Labs",
                public_jwk=agent_kp.public_jwk(),
                max_scopes=[ActionType.BILL_PAY.value],
                agent_id="agt_billpal",
            )

            now = clock.now()
            mandate = _build_sample_mandate(now)
            raw_body = canonical_json(
                {
                    "from_account": "ACC-001",
                    "payee_id": "BILL-ELEC",
                    "amount_paise": 480000,
                    "currency": "INR",
                    "reference": "NOV-2026",
                    "intent": {
                        "task": "pay_electricity_bill",
                        "category": "utilities.electricity",
                        "note": "Monthly bill",
                    },
                }
            )
            built_proof = build_dpop_proof(
                keypair=agent_kp,
                method="POST",
                gateway_origin="http://127.0.0.1:8000",
                path_or_url="/v1/payments/bills",
                body_bytes=raw_body,
                access_token="sample.access.token",
                now=now,
                jti="prf_s11_1",
            )
            action = CanonicalAction(
                request_id="req_s11_1",
                customer_id="cust_001",
                agent_id=agent.agent_id,
                mandate_id=mandate.terms.mandate_id,
                mandate_hash=mandate.mandate_hash,
                action_type=ActionType.BILL_PAY,
                params=ActionParams(
                    from_account="ACC-001",
                    payee_id="BILL-ELEC",
                    amount_paise=480000,
                    currency="INR",
                    reference="NOV-2026",
                ),
                intent=Intent(
                    task="pay_electricity_bill",
                    category="utilities.electricity",
                    note="Monthly bill",
                ),
                idempotency_key="bill-nov-s11",
                received_at=now,
            )
            signed_req = SignedRequestEvidence(
                proof_jws=built_proof.jwt,
                proof_jti=built_proof.jti,
                body_sha256=sha256_base64url(raw_body),
                action_hash=action_hash(action),
            )

            # Append full transaction lifecycle events to audit_log.
            await audit_log.append(
                AuditEvent(
                    event_type="request.received",
                    request_id="req_s11_1",
                    customer_id="cust_001",
                    agent_id=agent.agent_id,
                    mandate_id=mandate.terms.mandate_id,
                    mandate_hash=mandate.mandate_hash,
                    safe_payload=build_request_evidence_payload(
                        mandate=mandate,
                        agent=agent,
                        action=action,
                        signed_request=signed_req,
                    ),
                    occurred_at=now,
                )
            )
            clock.advance(seconds=1)
            await audit_log.append(
                AuditEvent(
                    event_type="policy.decision",
                    request_id="req_s11_1",
                    customer_id="cust_001",
                    agent_id=agent.agent_id,
                    mandate_id=mandate.terms.mandate_id,
                    mandate_hash=mandate.mandate_hash,
                    decision="ESCALATE",
                    reason_codes=(),
                    risk={"score": 45, "level": "MEDIUM"},
                    safe_payload={"action_hash": signed_req.action_hash},
                    occurred_at=clock.now(),
                )
            )
            clock.advance(seconds=2)
            await audit_log.append(
                AuditEvent(
                    event_type="escalation.approved",
                    request_id="req_s11_1",
                    customer_id="cust_001",
                    agent_id=agent.agent_id,
                    mandate_id=mandate.terms.mandate_id,
                    mandate_hash=mandate.mandate_hash,
                    decision="APPROVE",
                    safe_payload={"escalation_id": "esc_s11_1", "pin_verified": True},
                    occurred_at=clock.now(),
                )
            )
            clock.advance(seconds=1)
            bank_res = BankResult(
                status=BankStatus.EXECUTED,
                bank_operation_id="bank_op_1",
                bank_idempotency_key="bank-idem-s11",
                balance_paise=12020000,
                executed_at=clock.now(),
            )
            await audit_log.append(
                AuditEvent(
                    event_type="bank.executed",
                    request_id="req_s11_1",
                    customer_id="cust_001",
                    agent_id=agent.agent_id,
                    mandate_id=mandate.terms.mandate_id,
                    mandate_hash=mandate.mandate_hash,
                    decision="ALLOW",
                    safe_payload={"bank_result": bank_res.model_dump(mode="json")},
                    occurred_at=clock.now(),
                )
            )
            await audit_log.checkpoint()

            # Mount routes and verify HTTP behavior and role separation.
            app = FastAPI()
            install_identity_error_handlers(app)
            install_audit_error_handlers(app)
            app.include_router(build_identity_router(registry=registry, human_auth=human_auth))
            app.include_router(build_audit_router(audit_log=audit_log, human_auth=human_auth))

            cust_tok = (
                await human_auth.login_customer(customer_id="cust_001", pin="4821")
            ).session_token
            ops_tok = (
                await human_auth.login_ops(operator_id="ops_001", password="ops-password-1")
            ).session_token
            reg_tok = (
                await human_auth.login_ops(operator_id="reg_001", password="reg-password-1")
            ).session_token

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8000"
            ) as client:
                # Customer & registrar tokens CANNOT access /v1/audit/* routes.
                for path in (
                    "/v1/audit/entries",
                    "/v1/audit/verify",
                    "/v1/audit/evidence/req_s11_1",
                ):
                    assert (await client.get(path)).status_code == 401
                    assert (
                        await client.get(path, headers={"Authorization": f"Bearer {cust_tok}"})
                    ).status_code == 401
                    assert (
                        await client.get(path, headers={"Authorization": f"Bearer {reg_tok}"})
                    ).status_code == 401

                # Ops lists paginated & filtered entries.
                page1 = await client.get(
                    "/v1/audit/entries?limit=3",
                    headers={"Authorization": f"Bearer {ops_tok}"},
                )
                assert page1.status_code == 200
                p1_body: dict[str, Any] = page1.json()
                assert len(p1_body["items"]) == 3
                assert p1_body["next_cursor"] is not None

                filtered = await client.get(
                    "/v1/audit/entries?request_id=req_s11_1",
                    headers={"Authorization": f"Bearer {ops_tok}"},
                )
                assert filtered.status_code == 200
                assert len(filtered.json()["items"]) == 4

                # Ops verifies chain.
                ver = await client.get(
                    "/v1/audit/verify", headers={"Authorization": f"Bearer {ops_tok}"}
                )
                assert ver.status_code == 200
                ver_body = ver.json()
                assert ver_body["valid"] is True
                assert ver_body["anchored_through_seq"] >= 5

                # Ops fetches S11 evidence bundle.
                ev_resp = await client.get(
                    "/v1/audit/evidence/req_s11_1",
                    headers={"Authorization": f"Bearer {ops_tok}"},
                )
                assert ev_resp.status_code == 200
                bundle = ev_resp.json()
                assert bundle["request_id"] == "req_s11_1"
                assert bundle["mandate"]["mandate_hash"] == mandate.mandate_hash
                assert bundle["agent"]["jkt"] == agent.jkt
                assert bundle["action"]["params"]["amount_paise"] == 480000
                assert len(bundle["decisions"]) >= 1
                assert len(bundle["approval_history"]) == 1
                assert bundle["bank_outcome"]["bank_operation_id"] == "bank_op_1"
                assert len(bundle["audit_references"]) == 4
                assert bundle["verification"]["valid"] is True

                # Offline cryptographic verification of proof_jws inside EvidenceBundle!
                proof_jws: str = bundle["signed_request"]["proof_jws"]
                h_b64, p_b64, s_b64 = proof_jws.split(".")
                pub = jwk_to_public_key(bundle["agent"]["public_jwk"])
                assert ed25519_verify_b64url(pub, f"{h_b64}.{p_b64}".encode("ascii"), s_b64)

                # Unknown request_id -> 404.
                missing = await client.get(
                    "/v1/audit/evidence/req_does_not_exist",
                    headers={"Authorization": f"Bearer {ops_tok}"},
                )
                assert missing.status_code == 404
        finally:
            engine.dispose()


class TestSafeTamperTool:
    async def test_tamper_tool_refuses_in_place_and_detects_all_four_s10_variants(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src_db = tmp_path / "live_gateway.db"
        cp_file = tmp_path / "live_checkpoints.jsonl"
        engine = create_sqlite_identity_engine(f"sqlite:///{src_db.as_posix()}")
        create_audit_schema(engine)
        sf = create_identity_session_factory(engine)
        clock = FrozenClock(datetime(2026, 11, 18, 17, 30, 0, tzinfo=UTC))
        audit_key, audit_pub = generate_ed25519_keypair()
        cp_store = CheckpointStore(cp_file)
        audit_log = SqlAuditLog(
            session_factory=sf,
            signing_key=audit_key,
            clock=clock,
            checkpoint_store=cp_store,
        )

        try:
            for idx in range(1, 6):
                clock.advance(seconds=1)
                await audit_log.append(
                    AuditEvent(
                        event_type="policy.decision",
                        request_id=f"req_{idx}",
                        decision="ALLOW",
                        safe_payload={"step": idx},
                        occurred_at=clock.now(),
                    )
                )
            await audit_log.checkpoint()

            # Safeguard 1: refusing to tamper with source DB in place.
            with pytest.raises(ValueError, match="in place"):
                create_tampered_copy(source_db=src_db, target_db=src_db, mode="edit")

            expected_reasons = {
                "edit": (2, "entry_hash_mismatch"),
                "delete": (3, "sequence_gap"),
                "rehash": (2, "invalid_signature"),
                "truncate": (5, "tail_truncation"),
            }
            seq_args = {"edit": "2", "delete": "3", "rehash": "2", "truncate": "4"}

            for mode, (expected_seq, reason_prefix) in expected_reasons.items():
                copy_db = tmp_path / f"copy_{mode}.db"
                rc = tamper_cli_main(
                    [
                        "--source",
                        str(src_db),
                        "--target",
                        str(copy_db),
                        "--mode",
                        mode,
                        "--seq",
                        seq_args[mode],
                    ]
                )
                assert rc == 0
                summary = json.loads(capsys.readouterr().out)
                assert summary["mode"] == mode

                # Safeguard 2: refuses overwrite without --overwrite.
                with pytest.raises(FileExistsError):
                    create_tampered_copy(source_db=src_db, target_db=copy_db, mode=mode)  # type: ignore[arg-type]

                copy_engine = create_sqlite_identity_engine(f"sqlite:///{copy_db.as_posix()}")
                try:
                    report = await verify_audit_chain(
                        session_factory=create_identity_session_factory(copy_engine),
                        public_key=audit_pub,
                        checkpoint_store=cp_store,
                    )
                    assert report.valid is False
                    assert report.first_failure is not None
                    assert report.first_failure.seq == expected_seq
                    assert report.first_failure.reason.startswith(reason_prefix)
                finally:
                    copy_engine.dispose()

            # Safeguard 3: original live DB remains 100% intact and valid!
            live_report = await audit_log.verify()
            assert live_report.valid is True
            assert live_report.entries_checked == 5
            assert live_report.anchored_through_seq == 5
        finally:
            engine.dispose()
