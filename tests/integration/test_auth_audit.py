"""B08 real Member B integration tests: SDK <-> real auth/tokens/proofs <-> real signed audit.

Uses 100% real Member B providers (`SqlAgentRegistry`, `SqlHumanAuthService`,
`SqlProofReplayCache`, `DPoPProofVerifier`, `SqlTokenService`, `SqlIdentityVerifier`,
`SqlAuditLog`, `CheckpointStore`, and real `AgentGateClient` SDK) with injected boundary
doubles for unfinished Member A/C ports (`FixtureMandateReader`, recording BankClient).
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from agent_sdk import AgentAPIError, AgentGateClient, build_dpop_proof, generate_agent_keypair
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
from contracts.identity_models import AgentStatus
from contracts.reason_codes import ReasonCode
from gateway.audit.append import AuditWriteError
from gateway.audit.evidence import build_request_evidence_payload
from gateway.audit.schema import create_audit_schema
from gateway.audit.verify import verify_audit_chain
from gateway.identity.crypto import (
    ed25519_verify_b64url,
    generate_ed25519_keypair,
    jwk_to_public_key,
    mandate_hash,
    sign_prefixed_sha256,
)
from gateway.identity.human_auth import HumanSessionKeyring
from gateway.identity.models import SqlAlchemyTx
from gateway.identity.schema import (
    create_identity_schema,
    create_identity_session_factory,
    create_sqlite_identity_engine,
)
from gateway.identity.wiring import (
    MemberBProviders,
    assert_real_member_b_providers,
    build_real_member_b_providers,
    mount_member_b_routes,
)
from tests.support.b.providers import (
    FixtureAgentRegistry,
    FixtureHumanAuth,
    FixtureIdentityVerifier,
    FixtureMandateReader,
    MemoryAuditLog,
)
from tests.support.platform import FrozenClock
from tools.tamper import create_tampered_copy

pytestmark = pytest.mark.integration

GATEWAY_ORIGIN = "http://127.0.0.1:8000"
DEMO_PIN = "4821"
OPS_PASSWORD = "ops-integration-secret"
REG_PASSWORD = "reg-integration-secret"


class _RealModeLifecycleHarness:
    """Test-only Member A Lifecycle seam executing real Member B state + token revocation."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        providers: MemberBProviders,
        mandate_reader: FixtureMandateReader,
    ) -> None:
        self._sf = session_factory
        self._providers = providers
        self._mandate_reader = mandate_reader

    async def revoke_mandate(self, mandate_id: str) -> None:
        with self._sf() as session:
            with session.begin():
                tx = SqlAlchemyTx(id=f"tx_{secrets.token_hex(4)}", session=session)
                snap = self._mandate_reader.mandates[mandate_id]
                self._mandate_reader.mandates[mandate_id] = snap.model_copy(
                    update={"status": MandateStatus.REVOKED}
                )
                await self._providers.token_service.revoke_for_mandate(mandate_id, tx)

    async def suspend_agent(self, agent_id: str, reason_codes: tuple[str, ...]) -> None:
        with self._sf() as session:
            with session.begin():
                tx = SqlAlchemyTx(id=f"tx_{secrets.token_hex(4)}", session=session)
                await self._providers.registry.set_status(agent_id, AgentStatus.SUSPENDED, tx)
                await self._providers.token_service.revoke_for_agent(agent_id, tx)
                await self._providers.audit_log.append(
                    AuditEvent(
                        event_type="agent.suspended",
                        agent_id=agent_id,
                        reason_codes=reason_codes,
                        safe_payload={"status": AgentStatus.SUSPENDED.value},
                        occurred_at=self._providers.audit_log._clock.now(),
                    ),
                    tx=tx,
                )

    async def suspend_customer_delegation(
        self, customer_id: str, agent_id: str, reason_codes: tuple[str, ...]
    ) -> None:
        return None

    async def reinstate_agent(self, agent_id: str) -> None:
        with self._sf() as session:
            with session.begin():
                tx = SqlAlchemyTx(id=f"tx_{secrets.token_hex(4)}", session=session)
                await self._providers.registry.set_status(agent_id, AgentStatus.ACTIVE, tx)
                await self._providers.audit_log.append(
                    AuditEvent(
                        event_type="agent.reinstated",
                        agent_id=agent_id,
                        safe_payload={"status": AgentStatus.ACTIVE.value},
                        occurred_at=self._providers.audit_log._clock.now(),
                    ),
                    tx=tx,
                )


class _RecordingBankAdapter:
    """Boundary double tracking exact call order vs committed audit sequences."""

    def __init__(self, clock: FrozenClock, event_order: list[str]) -> None:
        self._clock = clock
        self._event_order = event_order
        self.calls: list[dict[str, Any]] = []

    async def pay_bill(self, action: CanonicalAction, bank_idempotency_key: str) -> BankResult:
        self._event_order.append("bank.pay_bill")
        self.calls.append(
            {"request_id": action.request_id, "amount_paise": action.params.amount_paise}
        )
        return BankResult(
            status=BankStatus.EXECUTED,
            bank_operation_id=f"bank_op_{len(self.calls)}",
            bank_idempotency_key=bank_idempotency_key,
            balance_paise=12500000 - action.params.amount_paise,
            executed_at=self._clock.now(),
        )


def _make_mandate(
    *,
    mandate_id: str,
    customer_id: str,
    agent_id: str,
    valid_from: datetime,
    valid_until: datetime,
) -> MandateSnapshot:
    terms = MandateTerms(
        mandate_id=mandate_id,
        version=1,
        customer_id=customer_id,
        agent_id=agent_id,
        valid_from=valid_from,
        valid_until=valid_until,
        grants=(
            Grant(
                grant_id="grt_bill_1",
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
    m_key, _ = generate_ed25519_keypair()
    return MandateSnapshot(
        terms=terms,
        status=MandateStatus.ACTIVE,
        mandate_hash=m_hash,
        sig=sign_prefixed_sha256(m_key, m_hash),
        key_id="mandate-k1",
    )


class TestRealModeGuard:
    def test_real_and_scripted_profiles_reject_fake_member_b_providers(
        self, tmp_path: Path
    ) -> None:
        db_file = tmp_path / "guard.db"
        engine = create_sqlite_identity_engine(f"sqlite:///{db_file.as_posix()}")
        create_identity_schema(engine)
        create_audit_schema(engine)
        sf = create_identity_session_factory(engine)
        clock = FrozenClock(datetime(2026, 11, 18, 18, 0, 0, tzinfo=UTC))
        providers = build_real_member_b_providers(
            session_factory=sf,
            clock=clock,
            mandate_reader=FixtureMandateReader(),
            trusted_origin=GATEWAY_ORIGIN,
            token_signing_key=generate_ed25519_keypair()[0],
            audit_signing_key=generate_ed25519_keypair()[0],
            human_keyring=HumanSessionKeyring.generate(),
        )
        try:
            # All real providers pass in real and scripted modes.
            for prof in ("real", "scripted"):
                assert_real_member_b_providers(
                    profile=prof,
                    identity_verifier=providers.identity_verifier,
                    human_auth=providers.human_auth,
                    registry=providers.registry,
                    token_service=providers.token_service,
                    audit_log=providers.audit_log,
                )

            # Replacing ANY single B provider with a fake fails in real mode!
            with pytest.raises(RuntimeError, match="identity_verifier"):
                assert_real_member_b_providers(
                    profile="real",
                    identity_verifier=FixtureIdentityVerifier(),
                    human_auth=providers.human_auth,
                    registry=providers.registry,
                    token_service=providers.token_service,
                    audit_log=providers.audit_log,
                )
            with pytest.raises(RuntimeError, match="human_auth"):
                assert_real_member_b_providers(
                    profile="real",
                    identity_verifier=providers.identity_verifier,
                    human_auth=FixtureHumanAuth(),
                    registry=providers.registry,
                    token_service=providers.token_service,
                    audit_log=providers.audit_log,
                )
            with pytest.raises(RuntimeError, match="registry"):
                assert_real_member_b_providers(
                    profile="real",
                    identity_verifier=providers.identity_verifier,
                    human_auth=providers.human_auth,
                    registry=FixtureAgentRegistry(),
                    token_service=providers.token_service,
                    audit_log=providers.audit_log,
                )
            with pytest.raises(RuntimeError, match="audit_log"):
                assert_real_member_b_providers(
                    profile="real",
                    identity_verifier=providers.identity_verifier,
                    human_auth=providers.human_auth,
                    registry=providers.registry,
                    token_service=providers.token_service,
                    audit_log=MemoryAuditLog(),
                )
        finally:
            engine.dispose()


class TestEndToEndAuthAuditIntegration:
    async def test_s6_s10_s11_revocation_write_ahead_and_secret_scan(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)

        db_file = tmp_path / "b08_gateway.db"
        cp_file = tmp_path / "b08_checkpoints.jsonl"
        engine = create_sqlite_identity_engine(f"sqlite:///{db_file.as_posix()}")
        create_identity_schema(engine)
        create_audit_schema(engine)
        sf = create_identity_session_factory(engine)
        clock = FrozenClock(datetime(2026, 11, 18, 18, 0, 0, tzinfo=UTC))
        now = clock.now()

        mandate_reader = FixtureMandateReader(
            mandates={
                "mnd_demo_1": _make_mandate(
                    mandate_id="mnd_demo_1",
                    customer_id="cust_001",
                    agent_id="agt_billpal",
                    valid_from=now - timedelta(minutes=5),
                    valid_until=now + timedelta(hours=1),
                ),
                "mnd_other_agent": _make_mandate(
                    mandate_id="mnd_other_agent",
                    customer_id="cust_001",
                    agent_id="agt_paywise",
                    valid_from=now - timedelta(minutes=5),
                    valid_until=now + timedelta(hours=1),
                ),
            }
        )

        token_key, _ = generate_ed25519_keypair()
        audit_key, audit_pub = generate_ed25519_keypair()
        human_keyring = HumanSessionKeyring.generate()
        providers = build_real_member_b_providers(
            session_factory=sf,
            clock=clock,
            mandate_reader=mandate_reader,
            trusted_origin=GATEWAY_ORIGIN,
            token_signing_key=token_key,
            audit_signing_key=audit_key,
            human_keyring=human_keyring,
            checkpoint_path=cp_file,
            token_ttl_seconds=300,
        )
        assert_real_member_b_providers(
            profile="real",
            identity_verifier=providers.identity_verifier,
            human_auth=providers.human_auth,
            registry=providers.registry,
            token_service=providers.token_service,
            audit_log=providers.audit_log,
        )

        lifecycle = _RealModeLifecycleHarness(sf, providers, mandate_reader)
        event_order: list[str] = []
        bank = _RecordingBankAdapter(clock, event_order)
        force_audit_failure = {"enabled": False}
        non_auth_responses: list[str] = []

        app = FastAPI()
        mount_member_b_routes(app, providers, lifecycle=lifecycle)

        @app.post("/v1/payments/bills")
        async def _integrated_pay_bill(request: Request, response: Response) -> dict[str, Any]:
            raw_bytes = await request.body()
            req_id = f"req_{secrets.token_hex(6)}"
            response.headers["X-Request-ID"] = req_id
            auth_ctx = await providers.identity_verifier.authenticate(
                raw_bytes, dict(request.headers), request.method, str(request.url)
            )
            parsed = json.loads(raw_bytes.decode("utf-8"))
            idem_key = request.headers.get("Idempotency-Key", "missing-key")
            mandate = await mandate_reader.get(auth_ctx.mandate_id)
            agent = await providers.registry.get(auth_ctx.agent_id)
            action = CanonicalAction(
                request_id=req_id,
                customer_id=auth_ctx.customer_id,
                agent_id=auth_ctx.agent_id,
                mandate_id=auth_ctx.mandate_id,
                mandate_hash=auth_ctx.mandate_hash,
                action_type=ActionType.BILL_PAY,
                params=ActionParams(
                    from_account=parsed["from_account"],
                    payee_id=parsed["payee_id"],
                    amount_paise=parsed["amount_paise"],
                    currency=parsed["currency"],
                    reference=parsed.get("reference"),
                ),
                intent=Intent(**parsed["intent"]),
                idempotency_key=idem_key,
                received_at=clock.now(),
            )
            signed_req = SignedRequestEvidence(
                proof_jws=request.headers["DPoP"],
                proof_jti=auth_ctx.proof_jti,
                body_sha256=sha256_base64url(raw_bytes),
                action_hash=action_hash(action),
            )

            # Write-ahead audit commit BEFORE contacting bank ("no log, no action").
            if force_audit_failure["enabled"]:
                raise AuditWriteError("Simulated pre-execution audit storage outage.")

            with sf() as session:
                with session.begin():
                    tx = SqlAlchemyTx(id=f"tx_{req_id}", session=session)
                    await providers.audit_log.append(
                        AuditEvent(
                            event_type="request.received",
                            request_id=req_id,
                            customer_id=auth_ctx.customer_id,
                            agent_id=auth_ctx.agent_id,
                            mandate_id=auth_ctx.mandate_id,
                            mandate_hash=auth_ctx.mandate_hash,
                            safe_payload=build_request_evidence_payload(
                                mandate=mandate,
                                agent=agent,
                                action=action,
                                signed_request=signed_req,
                            ),
                            occurred_at=clock.now(),
                        ),
                        tx=tx,
                    )
                    await providers.audit_log.append(
                        AuditEvent(
                            event_type="policy.decision",
                            request_id=req_id,
                            customer_id=auth_ctx.customer_id,
                            agent_id=auth_ctx.agent_id,
                            mandate_id=auth_ctx.mandate_id,
                            mandate_hash=auth_ctx.mandate_hash,
                            decision="ALLOW",
                            risk={"score": 12, "level": "LOW"},
                            safe_payload={"action_hash": signed_req.action_hash},
                            occurred_at=clock.now(),
                        ),
                        tx=tx,
                    )
            event_order.append("audit.decision.committed")

            # Contact bank strictly AFTER audit transaction committed.
            bank_res = await bank.pay_bill(action, f"bank-{idem_key}")

            final_ref = await providers.audit_log.append(
                AuditEvent(
                    event_type="bank.executed",
                    request_id=req_id,
                    customer_id=auth_ctx.customer_id,
                    agent_id=auth_ctx.agent_id,
                    mandate_id=auth_ctx.mandate_id,
                    mandate_hash=auth_ctx.mandate_hash,
                    decision="ALLOW",
                    safe_payload={"bank_result": bank_res.model_dump(mode="json")},
                    occurred_at=clock.now(),
                )
            )
            payload = {
                "request_id": req_id,
                "decision": "ALLOW",
                "status": "EXECUTED",
                "bank_result": bank_res.model_dump(mode="json"),
                "audit_ref": final_ref.model_dump(mode="json"),
            }
            non_auth_responses.append(json.dumps(payload))
            return payload

        try:
            # Seed humans and register two agents via real Member B services.
            await providers.human_auth.seed_customer(
                customer_id="cust_001", display_name="Demo Customer", pin=DEMO_PIN
            )
            await providers.human_auth.seed_operator(
                operator_id="ops_001",
                display_name="Ops Reviewer",
                role=HumanRole.OPS,
                password=OPS_PASSWORD,
            )
            await providers.human_auth.seed_operator(
                operator_id="reg_001",
                display_name="Registrar",
                role=HumanRole.REGISTRAR,
                password=REG_PASSWORD,
            )

            kp_billpal = generate_agent_keypair()
            kp_paywise = generate_agent_keypair()
            kp_rogue = generate_agent_keypair()
            await providers.registry.register(
                name="BillPal",
                vendor="FinBot Labs",
                public_jwk=kp_billpal.public_jwk(),
                max_scopes=[ActionType.BILL_PAY.value, ActionType.BALANCE_READ.value],
                agent_id="agt_billpal",
            )
            await providers.registry.register(
                name="PayWise",
                vendor="Other Labs",
                public_jwk=kp_paywise.public_jwk(),
                max_scopes=[ActionType.BILL_PAY.value],
                agent_id="agt_paywise",
            )

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url=GATEWAY_ORIGIN
            ) as raw_client:
                # Login Ops for audit & agent administration.
                ops_login = await raw_client.post(
                    "/v1/ops/login",
                    json={"operator_id": "ops_001", "password": OPS_PASSWORD},
                )
                assert ops_login.status_code == 200
                ops_token = ops_login.json()["session_token"]

                # --- Happy path via REAL Python SDK + Write-ahead audit ordering ---
                async with AgentGateClient(
                    gateway_url=GATEWAY_ORIGIN,
                    agent_id="agt_billpal",
                    mandate_id="mnd_demo_1",
                    keypair=kp_billpal,
                    clock=clock,
                    transport=transport,
                ) as sdk_client:
                    pay_resp = await sdk_client.pay_bill(
                        from_account="ACC-001",
                        payee_id="BILL-ELEC",
                        amount_paise=185000,
                        reference="NOV-2026",
                        task="pay_electricity_bill",
                        category="utilities.electricity",
                        note="Monthly bill",
                        idempotency_key="bill-int-001",
                    )
                    assert pay_resp["status"] == "EXECUTED"
                    executed_request_id = pay_resp["request_id"]
                    assert event_order == ["audit.decision.committed", "bank.pay_bill"]
                    assert len(bank.calls) == 1

                    active_access_token = sdk_client.token_manager.cached_token
                    assert active_access_token is not None
                    raw_agent_token = active_access_token.access_token

                    # --- S6a: Missing credentials -> 401 E_AUTH_MISSING ---
                    s6a = await raw_client.post("/v1/payments/bills", json={"from_account": "A"})
                    assert s6a.status_code == 401
                    assert s6a.json()["error"]["code"] == ReasonCode.E_AUTH_MISSING.value

                    # --- S6b: Stolen token with PayWise's proof key -> 401 KEY_MISMATCH ---
                    sample_body = canonical_json(
                        {
                            "from_account": "ACC-001",
                            "payee_id": "BILL-ELEC",
                            "amount_paise": 10000,
                            "currency": "INR",
                            "intent": {"task": "pay", "category": "utilities"},
                        }
                    )
                    stolen_proof = build_dpop_proof(
                        keypair=kp_paywise,
                        method="POST",
                        gateway_origin=GATEWAY_ORIGIN,
                        path_or_url="/v1/payments/bills",
                        body_bytes=sample_body,
                        access_token=raw_agent_token,
                        now=clock.now(),
                    )
                    s6b = await raw_client.post(
                        "/v1/payments/bills",
                        content=sample_body,
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"DPoP {raw_agent_token}",
                            "DPoP": stolen_proof.jwt,
                            "Idempotency-Key": "bill-s6b",
                        },
                    )
                    assert s6b.status_code == 401
                    assert s6b.json()["error"]["code"] == ReasonCode.E_AUTH_KEY_MISMATCH.value

                    # --- S6c: Replayed proof -> 401 E_AUTH_REPLAY ---
                    replay_proof = build_dpop_proof(
                        keypair=kp_billpal,
                        method="POST",
                        gateway_origin=GATEWAY_ORIGIN,
                        path_or_url="/v1/payments/bills",
                        body_bytes=sample_body,
                        access_token=raw_agent_token,
                        now=clock.now(),
                        jti="prf_s6c_fixed",
                    )
                    first_use = await raw_client.post(
                        "/v1/payments/bills",
                        content=sample_body,
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"DPoP {raw_agent_token}",
                            "DPoP": replay_proof.jwt,
                            "Idempotency-Key": "bill-s6c-1",
                        },
                    )
                    assert first_use.status_code == 200
                    s6c = await raw_client.post(
                        "/v1/payments/bills",
                        content=sample_body,
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"DPoP {raw_agent_token}",
                            "DPoP": replay_proof.jwt,
                            "Idempotency-Key": "bill-s6c-2",
                        },
                    )
                    assert s6c.status_code == 401
                    assert s6c.json()["error"]["code"] == ReasonCode.E_AUTH_REPLAY.value

                    # --- S6d: Unknown/unregistered rogue key -> 401 E_AUTH_PROOF_INVALID ---
                    tok_req_bytes = canonical_json(
                        {"client_id": "agt_rogue", "mandate_id": "mnd_demo_1"}
                    )
                    rogue_proof = build_dpop_proof(
                        keypair=kp_rogue,
                        method="POST",
                        gateway_origin=GATEWAY_ORIGIN,
                        path_or_url="/v1/oauth/token",
                        body_bytes=tok_req_bytes,
                        access_token=None,
                        now=clock.now(),
                    )
                    s6d = await raw_client.post(
                        "/v1/oauth/token",
                        content=tok_req_bytes,
                        headers={"Content-Type": "application/json", "DPoP": rogue_proof.jwt},
                    )
                    assert s6d.status_code == 401
                    assert s6d.json()["error"]["code"] == ReasonCode.E_AUTH_PROOF_INVALID.value

                    # --- S6e: Wrong mandate binding -> 403 E_AUTH_AGENT_MANDATE_MISMATCH ---
                    wrong_m_bytes = canonical_json(
                        {"client_id": "agt_billpal", "mandate_id": "mnd_other_agent"}
                    )
                    wrong_m_proof = build_dpop_proof(
                        keypair=kp_billpal,
                        method="POST",
                        gateway_origin=GATEWAY_ORIGIN,
                        path_or_url="/v1/oauth/token",
                        body_bytes=wrong_m_bytes,
                        access_token=None,
                        now=clock.now(),
                    )
                    s6e = await raw_client.post(
                        "/v1/oauth/token",
                        content=wrong_m_bytes,
                        headers={"Content-Type": "application/json", "DPoP": wrong_m_proof.jwt},
                    )
                    assert s6e.status_code == 403
                    assert (
                        s6e.json()["error"]["code"]
                        == ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH.value
                    )

                    # --- S6g: Modified signed request body -> 401 E_AUTH_PROOF_INVALID ---
                    signed_good_proof = build_dpop_proof(
                        keypair=kp_billpal,
                        method="POST",
                        gateway_origin=GATEWAY_ORIGIN,
                        path_or_url="/v1/payments/bills",
                        body_bytes=sample_body,
                        access_token=raw_agent_token,
                        now=clock.now(),
                    )
                    tampered_wire_body = sample_body.replace(b"10000", b"10001")
                    s6g = await raw_client.post(
                        "/v1/payments/bills",
                        content=tampered_wire_body,
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"DPoP {raw_agent_token}",
                            "DPoP": signed_good_proof.jwt,
                            "Idempotency-Key": "bill-s6g",
                        },
                    )
                    assert s6g.status_code == 401
                    assert s6g.json()["error"]["code"] == ReasonCode.E_AUTH_PROOF_INVALID.value

                    # --- S6h: Agent token attempting human-only endpoints -> 401 INVALID_TOKEN ---
                    for human_path in (
                        "/v1/ops/agents",
                        "/v1/audit/entries",
                        "/v1/audit/verify",
                        f"/v1/audit/evidence/{executed_request_id}",
                    ):
                        s6h = await raw_client.get(
                            human_path, headers={"Authorization": f"Bearer {raw_agent_token}"}
                        )
                        assert s6h.status_code == 401
                        assert (
                            s6h.json()["error"]["code"] == ReasonCode.E_AUTH_INVALID_TOKEN.value
                        )

                    # --- Audit failure before bank execution -> 503 & ZERO new bank calls ---
                    calls_before_outage = len(bank.calls)
                    force_audit_failure["enabled"] = True
                    with pytest.raises(AgentAPIError) as audit_err:
                        await sdk_client.pay_bill(
                            from_account="ACC-001",
                            payee_id="BILL-ELEC",
                            amount_paise=25000,
                            task="pay",
                            category="utilities",
                            idempotency_key="bill-outage-1",
                        )
                    force_audit_failure["enabled"] = False
                    assert audit_err.value.status_code == 503
                    assert audit_err.value.error_code == ReasonCode.E_AUDIT_WRITE_FAILED.value
                    assert len(bank.calls) == calls_before_outage

                    # --- S6f: Expired token -> 401 E_AUTH_EXPIRED on raw call, SDK refreshes ---
                    clock.advance(seconds=301)
                    exp_proof = build_dpop_proof(
                        keypair=kp_billpal,
                        method="POST",
                        gateway_origin=GATEWAY_ORIGIN,
                        path_or_url="/v1/payments/bills",
                        body_bytes=sample_body,
                        access_token=raw_agent_token,
                        now=clock.now(),
                    )
                    s6f_raw = await raw_client.post(
                        "/v1/payments/bills",
                        content=sample_body,
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"DPoP {raw_agent_token}",
                            "DPoP": exp_proof.jwt,
                            "Idempotency-Key": "bill-s6f-raw",
                        },
                    )
                    assert s6f_raw.status_code == 401
                    assert s6f_raw.json()["error"]["code"] == ReasonCode.E_AUTH_EXPIRED.value

                    # SDK transparently refreshes while mandate & agent remain active.
                    refreshed_pay = await sdk_client.pay_bill(
                        from_account="ACC-001",
                        payee_id="BILL-ELEC",
                        amount_paise=15000,
                        task="pay",
                        category="utilities",
                        idempotency_key="bill-s6f-refresh",
                    )
                    assert refreshed_pay["status"] == "EXECUTED"
                    assert sdk_client.token_manager.cached_token is not None
                    refreshed_token = sdk_client.token_manager.cached_token.access_token

                    # --- Suspended / revoked agent blocked immediately ---
                    susp = await raw_client.post(
                        "/v1/ops/agents/agt_billpal/suspend",
                        json={"reason": "E_RISK_BREAKER_OPEN"},
                        headers={"Authorization": f"Bearer {ops_token}"},
                    )
                    assert susp.status_code == 200
                    with pytest.raises(AgentAPIError) as susp_err:
                        await sdk_client.pay_bill(
                            from_account="ACC-001",
                            payee_id="BILL-ELEC",
                            amount_paise=15000,
                            task="pay",
                            category="utilities",
                            idempotency_key="bill-after-suspend",
                        )
                    assert susp_err.value.status_code == 403
                    assert susp_err.value.error_code == ReasonCode.E_AUTH_AGENT_SUSPENDED.value

                    # --- Reinstate agent: old revoked token fails 401 E_AUTH_TOKEN_REVOKED ---
                    rein = await raw_client.post(
                        "/v1/ops/agents/agt_billpal/reinstate",
                        headers={"Authorization": f"Bearer {ops_token}"},
                    )
                    assert rein.status_code == 200
                    old_tok_proof = build_dpop_proof(
                        keypair=kp_billpal,
                        method="POST",
                        gateway_origin=GATEWAY_ORIGIN,
                        path_or_url="/v1/payments/bills",
                        body_bytes=sample_body,
                        access_token=refreshed_token,
                        now=clock.now(),
                    )
                    old_tok_resp = await raw_client.post(
                        "/v1/payments/bills",
                        content=sample_body,
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"DPoP {refreshed_token}",
                            "DPoP": old_tok_proof.jwt,
                            "Idempotency-Key": "bill-old-tok",
                        },
                    )
                    assert old_tok_resp.status_code == 401
                    assert (
                        old_tok_resp.json()["error"]["code"]
                        == ReasonCode.E_AUTH_TOKEN_REVOKED.value
                    )

                    # --- Revoked mandate blocks new protected request -> 403 E_MANDATE_REVOKED ---
                    await lifecycle.revoke_mandate("mnd_demo_1")
                    sdk_client.token_manager.invalidate()
                    with pytest.raises(AgentAPIError) as m_rev_err:
                        await sdk_client.pay_bill(
                            from_account="ACC-001",
                            payee_id="BILL-ELEC",
                            amount_paise=15000,
                            task="pay",
                            category="utilities",
                            idempotency_key="bill-after-mandate-revoke",
                        )
                    assert m_rev_err.value.status_code == 403
                    assert m_rev_err.value.error_code == ReasonCode.E_MANDATE_REVOKED.value

                # --- Anchor checkpoint at head, verify S11 EvidenceBundle & S10a-d Tamper ---
                head_anchor = await providers.audit_log.checkpoint()
                assert head_anchor.seq >= 10

                ver_resp = await raw_client.get(
                    "/v1/audit/verify", headers={"Authorization": f"Bearer {ops_token}"}
                )
                assert ver_resp.status_code == 200
                assert ver_resp.json()["valid"] is True
                assert ver_resp.json()["anchored_through_seq"] == head_anchor.seq

                # S11: Fetch complete EvidenceBundle and verify offline proof signature!
                ev_resp = await raw_client.get(
                    f"/v1/audit/evidence/{executed_request_id}",
                    headers={"Authorization": f"Bearer {ops_token}"},
                )
                assert ev_resp.status_code == 200
                bundle = ev_resp.json()
                assert bundle["request_id"] == executed_request_id
                assert bundle["mandate"]["terms"]["mandate_id"] == "mnd_demo_1"
                assert bundle["agent"]["agent_id"] == "agt_billpal"
                assert bundle["action"]["params"]["amount_paise"] == 185000
                assert bundle["bank_outcome"]["status"] == "EXECUTED"
                assert bundle["verification"]["valid"] is True
                h_b64, p_b64, s_b64 = bundle["signed_request"]["proof_jws"].split(".")
                assert ed25519_verify_b64url(
                    jwk_to_public_key(bundle["agent"]["public_jwk"]),
                    f"{h_b64}.{p_b64}".encode("ascii"),
                    s_b64,
                )

                # S10a-d: Disposable tamper copies all detected against retained checkpoint!
                assert providers.checkpoint_store is not None
                for mode, seq_num, prefix in (
                    ("edit", 2, "entry_hash_mismatch"),
                    ("delete", 3, "sequence_gap"),
                    ("rehash", 2, "invalid_signature"),
                    ("truncate", head_anchor.seq - 1, "tail_truncation"),
                ):
                    copy_db = tmp_path / f"s10_{mode}.db"
                    create_tampered_copy(
                        source_db=db_file,
                        target_db=copy_db,
                        mode=mode,  # type: ignore[arg-type]
                        seq=seq_num,
                    )
                    copy_engine = create_sqlite_identity_engine(f"sqlite:///{copy_db.as_posix()}")
                    try:
                        tamper_report = await verify_audit_chain(
                            session_factory=create_identity_session_factory(copy_engine),
                            public_key=audit_pub,
                            checkpoint_store=providers.checkpoint_store,
                        )
                        assert tamper_report.valid is False
                        assert tamper_report.first_failure is not None
                        assert tamper_report.first_failure.reason.startswith(prefix)
                    finally:
                        copy_engine.dispose()

                # Live DB still intact after all S10 copies.
                assert (await providers.audit_log.verify()).valid is True

                # --- Comprehensive Secret-Leak Scan ---
                entries_resp = await raw_client.get(
                    "/v1/audit/entries?limit=100",
                    headers={"Authorization": f"Bearer {ops_token}"},
                )
                assert entries_resp.status_code == 200

                with sf() as session:
                    db_dump_parts = [
                        str(
                            session.execute(
                                text("SELECT * FROM audit_log")
                            ).fetchall()
                        ),
                        str(
                            session.execute(
                                text("SELECT * FROM issued_tokens")
                            ).fetchall()
                        ),
                        str(
                            session.execute(
                                text("SELECT * FROM human_sessions")
                            ).fetchall()
                        ),
                        str(
                            session.execute(
                                text("SELECT * FROM customers")
                            ).fetchall()
                        ),
                        str(
                            session.execute(
                                text("SELECT * FROM operators")
                            ).fetchall()
                        ),
                    ]

                scan_corpus = "\n".join(
                    [
                        json.dumps(bundle),
                        entries_resp.text,
                        *non_auth_responses,
                        cp_file.read_text(encoding="utf-8"),
                        caplog.text,
                        *db_dump_parts,
                    ]
                )
                for secret in (
                    DEMO_PIN,
                    OPS_PASSWORD,
                    REG_PASSWORD,
                    raw_agent_token,
                    refreshed_token,
                    ops_token,
                    "BEGIN PRIVATE KEY",
                ):
                    assert secret not in scan_corpus
        finally:
            engine.dispose()
