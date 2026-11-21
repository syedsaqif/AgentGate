"""B04 unit, security, and HTTP tests for Agent Token Lifecycle & Revocation."""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from contracts.common import ActionType, HumanRole
from contracts.core_models import (
    Grant,
    MandateSnapshot,
    MandateStatus,
    MandateTerms,
    StepUpRules,
)
from contracts.identity_models import AgentPublicIdentity, AgentStatus
from contracts.reason_codes import ReasonCode
from gateway.identity.crypto import (
    access_token_hash_b64url,
    b64url_encode,
    body_sha256_b64url,
    canonical_json,
    ed25519_sign_b64url,
    generate_ed25519_keypair,
    mandate_hash,
    public_key_to_jwk,
    sha256_hex,
    sign_prefixed_sha256,
)
from gateway.identity.errors import AuthenticationError, AuthorizationError
from gateway.identity.human_auth import HumanSessionKeyring, SqlHumanAuthService
from gateway.identity.models import IssuedTokenRecord, SqlAlchemyTx
from gateway.identity.proofs import DPOP_ALG, DPOP_TYP, DPoPProofVerifier
from gateway.identity.registry import SqlAgentRegistry
from gateway.identity.replay import SqlProofReplayCache
from gateway.identity.routes import build_identity_router, install_identity_error_handlers
from gateway.identity.schema import (
    create_identity_schema,
    create_identity_session_factory,
    create_sqlite_identity_engine,
)
from gateway.identity.tokens import (
    AGENT_TOKEN_ALG,
    AGENT_TOKEN_AUDIENCE,
    AGENT_TOKEN_ISSUER,
    AGENT_TOKEN_TYP,
    MAX_TOKEN_TTL_SECONDS,
    SqlIdentityVerifier,
    SqlTokenService,
)
from tests.support.b.providers import FixtureMandateReader, MemoryAuditLog
from tests.support.platform import FrozenClock

TRUSTED_ORIGIN = "http://127.0.0.1:8000"
SAMPLE_BILL_BODY = canonical_json(
    {
        "from_account": "ACC-001",
        "payee_id": "BILL-ELEC",
        "amount_paise": 185000,
        "currency": "INR",
        "reference": "NOV-2026",
        "intent": {
            "task": "pay_electricity_bill",
            "category": "utilities.electricity",
            "note": "Monthly electricity bill",
        },
    }
)


def _make_mandate(
    *,
    mandate_id: str = "mnd_demo_1",
    customer_id: str = "cust_001",
    agent_id: str = "agt_billpal",
    valid_from: datetime,
    valid_until: datetime,
    status: MandateStatus = MandateStatus.ACTIVE,
    mandate_key: Ed25519PrivateKey | None = None,
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
                currency="INR",
                max_per_txn_paise=500000,
                max_total_paise=1000000,
                max_count=5,
                window="mandate",
                category="utilities.electricity",
            ),
        ),
        step_up=StepUpRules(amount_above_paise=300000),
        risk_profile="balanced",
    )
    m_hash = mandate_hash(terms)
    key = mandate_key or generate_ed25519_keypair()[0]
    sig = sign_prefixed_sha256(key, m_hash)
    return MandateSnapshot(
        terms=terms,
        status=status,
        mandate_hash=m_hash,
        sig=sig,
        key_id="mandate-issuer-k1",
    )


def _build_proof(
    *,
    private_key: Ed25519PrivateKey,
    method: str,
    path: str,
    body: bytes,
    iat: int,
    access_token: str | None = None,
    jti: str | None = None,
) -> str:
    header = {
        "typ": DPOP_TYP,
        "alg": DPOP_ALG,
        "jwk": public_key_to_jwk(private_key.public_key()),
    }
    claims: dict[str, Any] = {
        "htm": method,
        "htu": f"{TRUSTED_ORIGIN}{path}",
        "iat": iat,
        "jti": jti or f"prf_{secrets.token_hex(8)}",
        "bsh": body_sha256_b64url(body),
    }
    if access_token is not None:
        claims["ath"] = access_token_hash_b64url(access_token)
    h_b64 = b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p_b64 = b64url_encode(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    s_b64 = ed25519_sign_b64url(private_key, f"{h_b64}.{p_b64}".encode("ascii"))
    return f"{h_b64}.{p_b64}.{s_b64}"


type TokenEnv = tuple[
    Engine,
    sessionmaker[Session],
    FrozenClock,
    MemoryAuditLog,
    SqlAgentRegistry,
    FixtureMandateReader,
    SqlTokenService,
    SqlIdentityVerifier,
    SqlHumanAuthService,
    Ed25519PrivateKey,
    Ed25519PrivateKey,
    AgentPublicIdentity,
    Ed25519PrivateKey,
    AgentPublicIdentity,
]


@pytest.fixture
async def token_env(tmp_path: Path) -> AsyncIterator[TokenEnv]:
    db_file = tmp_path / "tokens_b04.db"
    engine = create_sqlite_identity_engine(f"sqlite:///{db_file.as_posix()}")
    create_identity_schema(engine)
    session_factory = create_identity_session_factory(engine)
    clock = FrozenClock(datetime(2026, 11, 18, 14, 0, 0, tzinfo=UTC))
    audit_log = MemoryAuditLog()
    registry = SqlAgentRegistry(session_factory, clock, audit_log=audit_log)
    replay_cache = SqlProofReplayCache(session_factory, clock)
    proof_verifier = DPoPProofVerifier(
        registry=registry,
        replay_cache=replay_cache,
        clock=clock,
        trusted_origin=TRUSTED_ORIGIN,
        audit_log=audit_log,
    )

    token_signing_key, _ = generate_ed25519_keypair()
    now = clock.now()
    mandate_reader = FixtureMandateReader(
        mandates={
            "mnd_active": _make_mandate(
                mandate_id="mnd_active",
                customer_id="cust_001",
                agent_id="agt_billpal",
                valid_from=now - timedelta(minutes=5),
                valid_until=now + timedelta(hours=1),
                status=MandateStatus.ACTIVE,
            ),
            "mnd_short": _make_mandate(
                mandate_id="mnd_short",
                customer_id="cust_001",
                agent_id="agt_billpal",
                valid_from=now - timedelta(seconds=10),
                valid_until=now + timedelta(seconds=90),
                status=MandateStatus.ACTIVE,
            ),
            "mnd_expired": _make_mandate(
                mandate_id="mnd_expired",
                customer_id="cust_001",
                agent_id="agt_billpal",
                valid_from=now - timedelta(hours=2),
                valid_until=now - timedelta(seconds=1),
                status=MandateStatus.EXPIRED,
            ),
            "mnd_revoked": _make_mandate(
                mandate_id="mnd_revoked",
                customer_id="cust_001",
                agent_id="agt_billpal",
                valid_from=now - timedelta(minutes=5),
                valid_until=now + timedelta(hours=1),
                status=MandateStatus.REVOKED,
            ),
            "mnd_future": _make_mandate(
                mandate_id="mnd_future",
                customer_id="cust_001",
                agent_id="agt_billpal",
                valid_from=now + timedelta(minutes=10),
                valid_until=now + timedelta(hours=1),
                status=MandateStatus.ACTIVE,
            ),
            "mnd_other_agent": _make_mandate(
                mandate_id="mnd_other_agent",
                customer_id="cust_001",
                agent_id="agt_paywise",
                valid_from=now - timedelta(minutes=5),
                valid_until=now + timedelta(hours=1),
                status=MandateStatus.ACTIVE,
            ),
        }
    )

    token_service = SqlTokenService(
        session_factory=session_factory,
        clock=clock,
        signing_key=token_signing_key,
        proof_verifier=proof_verifier,
        mandate_reader=mandate_reader,
        audit_log=audit_log,
    )
    identity_verifier = SqlIdentityVerifier(
        session_factory=session_factory,
        clock=clock,
        token_public_key=token_signing_key.public_key(),
        proof_verifier=proof_verifier,
        mandate_reader=mandate_reader,
        audit_log=audit_log,
    )
    human_auth = SqlHumanAuthService(
        session_factory,
        clock,
        HumanSessionKeyring.generate(),
        audit_log=audit_log,
    )
    await human_auth.seed_customer(customer_id="cust_001", display_name="Demo", pin="4821")
    await human_auth.seed_operator(
        operator_id="ops_001",
        display_name="Ops",
        role=HumanRole.OPS,
        password="ops-secret-password",
    )

    priv_a, pub_a = generate_ed25519_keypair()
    agent_a = await registry.register(
        name="BillPal",
        vendor="FinBot Labs",
        public_jwk=public_key_to_jwk(pub_a),
        max_scopes=[ActionType.BILL_PAY.value, ActionType.BALANCE_READ.value],
        agent_id="agt_billpal",
    )
    priv_b, pub_b = generate_ed25519_keypair()
    agent_b = await registry.register(
        name="PayWise",
        vendor="Other Labs",
        public_jwk=public_key_to_jwk(pub_b),
        max_scopes=[ActionType.BILL_PAY.value],
        agent_id="agt_paywise",
    )
    audit_log.events.clear()
    try:
        yield (
            engine,
            session_factory,
            clock,
            audit_log,
            registry,
            mandate_reader,
            token_service,
            identity_verifier,
            human_auth,
            token_signing_key,
            priv_a,
            agent_a,
            priv_b,
            agent_b,
        )
    finally:
        engine.dispose()


async def _issue_for(
    token_service: SqlTokenService,
    clock: FrozenClock,
    private_key: Ed25519PrivateKey,
    *,
    client_id: str,
    mandate_id: str,
) -> tuple[str, int]:
    body = canonical_json({"client_id": client_id, "mandate_id": mandate_id})
    proof = _build_proof(
        private_key=private_key,
        method="POST",
        path="/v1/oauth/token",
        body=body,
        iat=int(clock.now().timestamp()),
    )
    res = await token_service.issue_token(
        client_id=client_id,
        mandate_id=mandate_id,
        proof_jwt=proof,
        method="POST",
        request_url="/v1/oauth/token",
        request_bytes=body,
    )
    return res.access_token, res.expires_in


class TestTokenIssuanceAndClaims:
    async def test_active_mandate_issues_key_bound_jwt_with_capped_ttl_and_no_secret_leak(
        self, token_env: TokenEnv
    ) -> None:
        (
            _,
            session_factory,
            clock,
            audit_log,
            _,
            mandate_reader,
            token_service,
            identity_verifier,
            _,
            token_signing_key,
            priv_a,
            agent_a,
            _,
            _,
        ) = token_env

        # 1. Active mandate with 1-hour lifetime -> TTL capped at 600s.
        token, expires_in = await _issue_for(
            token_service, clock, priv_a, client_id="agt_billpal", mandate_id="mnd_active"
        )
        assert expires_in == MAX_TOKEN_TTL_SECONDS

        header = jwt.get_unverified_header(token)
        assert header["alg"] == AGENT_TOKEN_ALG
        assert header["typ"] == AGENT_TOKEN_TYP
        assert header["kid"] == token_service.key_id

        claims = jwt.decode(
            token,
            token_signing_key.public_key(),
            algorithms=[AGENT_TOKEN_ALG],
            issuer=AGENT_TOKEN_ISSUER,
            audience=AGENT_TOKEN_AUDIENCE,
            options={"verify_exp": False},
        )
        expected_mandate = mandate_reader.mandates["mnd_active"]
        assert claims["iss"] == AGENT_TOKEN_ISSUER
        assert claims["aud"] == AGENT_TOKEN_AUDIENCE
        assert claims["sub"] == "cust_001"
        assert claims["act"] == {"sub": "agt_billpal"}
        assert claims["mandate_id"] == "mnd_active"
        assert claims["mandate_hash"] == expected_mandate.mandate_hash
        assert claims["cnf"] == {"jkt": agent_a.jkt}
        assert claims["exp"] - claims["iat"] == MAX_TOKEN_TTL_SECONDS

        # 2. Short 90-second mandate -> TTL capped at 90s (never exceeds mandate expiry).
        short_token, short_ttl = await _issue_for(
            token_service, clock, priv_a, client_id="agt_billpal", mandate_id="mnd_short"
        )
        assert short_ttl == 90

        # 3. Resource authentication succeeds with bound proof and returns full AuthContext.
        proof = _build_proof(
            private_key=priv_a,
            method="POST",
            path="/v1/payments/bills",
            body=SAMPLE_BILL_BODY,
            iat=int(clock.now().timestamp()),
            access_token=token,
            jti="prf_res_1",
        )
        auth_ctx = await identity_verifier.authenticate(
            SAMPLE_BILL_BODY,
            {"Authorization": f"DPoP {token}", "DPoP": proof},
            "POST",
            f"{TRUSTED_ORIGIN}/v1/payments/bills",
        )
        assert auth_ctx.customer_id == "cust_001"
        assert auth_ctx.agent_id == "agt_billpal"
        assert auth_ctx.mandate_id == "mnd_active"
        assert auth_ctx.mandate_hash == expected_mandate.mandate_hash
        assert auth_ctx.jkt == agent_a.jkt
        assert auth_ctx.token_jti == claims["jti"]
        assert auth_ctx.proof_jti == "prf_res_1"

        # 4. Raw tokens NEVER stored in SQLite or emitted in audit events.
        with session_factory() as session:
            raw_hashes: list[object] = list(
                session.execute(text("SELECT token_hash FROM issued_tokens")).scalars().all()
            )
            stored_hashes: list[str] = [str(val) for val in raw_hashes]
        assert token not in stored_hashes
        assert short_token not in stored_hashes
        assert sha256_hex(token.encode("ascii")) in stored_hashes

        audit_blob = json.dumps([e.model_dump(mode="json") for e in audit_log.events])
        assert token not in audit_blob
        assert short_token not in audit_blob

    async def test_issuance_rejects_expired_revoked_future_wrong_agent_and_suspended(
        self, token_env: TokenEnv
    ) -> None:
        (
            _,
            session_factory,
            clock,
            _,
            registry,
            _,
            token_service,
            _,
            _,
            _,
            priv_a,
            _,
            priv_b,
            _,
        ) = token_env

        # Expired mandate -> 401 E_AUTH_EXPIRED.
        with pytest.raises(AuthenticationError) as exp_exc:
            await _issue_for(
                token_service, clock, priv_a, client_id="agt_billpal", mandate_id="mnd_expired"
            )
        assert exp_exc.value.code is ReasonCode.E_AUTH_EXPIRED
        assert exp_exc.value.http_status == 401

        # Revoked mandate -> 403 E_MANDATE_REVOKED.
        with pytest.raises(AuthorizationError) as rev_exc:
            await _issue_for(
                token_service, clock, priv_a, client_id="agt_billpal", mandate_id="mnd_revoked"
            )
        assert rev_exc.value.code is ReasonCode.E_MANDATE_REVOKED
        assert rev_exc.value.http_status == 403

        # Not-yet-valid mandate -> 403 E_AUTH_AGENT_MANDATE_MISMATCH.
        with pytest.raises(AuthorizationError) as fut_exc:
            await _issue_for(
                token_service, clock, priv_a, client_id="agt_billpal", mandate_id="mnd_future"
            )
        assert fut_exc.value.code is ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH
        assert fut_exc.value.http_status == 403

        # Wrong agent for mandate -> 403 E_AUTH_AGENT_MANDATE_MISMATCH.
        with pytest.raises(AuthorizationError) as wrong_agt_exc:
            await _issue_for(
                token_service, clock, priv_a, client_id="agt_billpal", mandate_id="mnd_other_agent"
            )
        assert wrong_agt_exc.value.code is ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH

        # Mismatched client_id vs proof key -> 401 E_AUTH_KEY_MISMATCH.
        with pytest.raises(AuthenticationError) as client_mismatch:
            await _issue_for(
                token_service, clock, priv_b, client_id="agt_billpal", mandate_id="mnd_active"
            )
        assert client_mismatch.value.code is ReasonCode.E_AUTH_KEY_MISMATCH
        assert client_mismatch.value.http_status == 401

        # Suspended agent -> 403 E_AUTH_AGENT_SUSPENDED.
        with session_factory() as session:
            with session.begin():
                tx = SqlAlchemyTx(id="tx_susp", session=session)
                await registry.set_status("agt_billpal", AgentStatus.SUSPENDED, tx)

        with pytest.raises(AuthorizationError) as susp_exc:
            await _issue_for(
                token_service, clock, priv_a, client_id="agt_billpal", mandate_id="mnd_active"
            )
        assert susp_exc.value.code is ReasonCode.E_AUTH_AGENT_SUSPENDED
        assert susp_exc.value.http_status == 403


class TestLiveAuthorityAndRevocationPrecedence:
    async def test_live_revoked_mandate_suspended_agent_and_revoked_token_precedence(
        self, token_env: TokenEnv
    ) -> None:
        (
            _,
            session_factory,
            clock,
            _,
            registry,
            mandate_reader,
            token_service,
            identity_verifier,
            _,
            _,
            priv_a,
            _,
            _,
            _,
        ) = token_env

        token, _ = await _issue_for(
            token_service, clock, priv_a, client_id="agt_billpal", mandate_id="mnd_active"
        )

        async def _call_resource() -> None:
            proof = _build_proof(
                private_key=priv_a,
                method="POST",
                path="/v1/payments/bills",
                body=SAMPLE_BILL_BODY,
                iat=int(clock.now().timestamp()),
                access_token=token,
            )
            await identity_verifier.authenticate(
                SAMPLE_BILL_BODY,
                {"Authorization": f"DPoP {token}", "DPoP": proof},
                "POST",
                "/v1/payments/bills",
            )

        # 1. Revoke mandate + revoke_for_mandate -> MUST return 403 E_MANDATE_REVOKED.
        active_m = mandate_reader.mandates["mnd_active"]
        mandate_reader.mandates["mnd_active"] = active_m.model_copy(
            update={"status": MandateStatus.REVOKED}
        )
        with session_factory() as session:
            with session.begin():
                tx = SqlAlchemyTx(id="tx_rev_m", session=session)
                await token_service.revoke_for_mandate("mnd_active", tx)

        with pytest.raises(AuthorizationError) as m_rev_exc:
            await _call_resource()
        assert m_rev_exc.value.code is ReasonCode.E_MANDATE_REVOKED
        assert m_rev_exc.value.http_status == 403

        # 2. Restore mandate active, suspend agent + revoke_for_agent -> 403 E_AUTH_AGENT_SUSPENDED.
        mandate_reader.mandates["mnd_active"] = active_m
        token2, _ = await _issue_for(
            token_service, clock, priv_a, client_id="agt_billpal", mandate_id="mnd_active"
        )
        with session_factory() as session:
            with session.begin():
                tx = SqlAlchemyTx(id="tx_susp_a", session=session)
                await registry.set_status("agt_billpal", AgentStatus.SUSPENDED, tx)
                await token_service.revoke_for_agent("agt_billpal", tx)

        proof2 = _build_proof(
            private_key=priv_a,
            method="POST",
            path="/v1/payments/bills",
            body=SAMPLE_BILL_BODY,
            iat=int(clock.now().timestamp()),
            access_token=token2,
        )
        with pytest.raises(AuthorizationError) as a_susp_exc:
            await identity_verifier.authenticate(
                SAMPLE_BILL_BODY,
                {"Authorization": f"DPoP {token2}", "DPoP": proof2},
                "POST",
                "/v1/payments/bills",
            )
        assert a_susp_exc.value.code is ReasonCode.E_AUTH_AGENT_SUSPENDED
        assert a_susp_exc.value.http_status == 403

        # 3. Reinstate agent -> old revoked token2 stays revoked (401 E_AUTH_TOKEN_REVOKED).
        with session_factory() as session:
            with session.begin():
                tx = SqlAlchemyTx(id="tx_rein", session=session)
                await registry.set_status("agt_billpal", AgentStatus.ACTIVE, tx)

        proof3 = _build_proof(
            private_key=priv_a,
            method="POST",
            path="/v1/payments/bills",
            body=SAMPLE_BILL_BODY,
            iat=int(clock.now().timestamp()),
            access_token=token2,
        )
        with pytest.raises(AuthenticationError) as tok_rev_exc:
            await identity_verifier.authenticate(
                SAMPLE_BILL_BODY,
                {"Authorization": f"DPoP {token2}", "DPoP": proof3},
                "POST",
                "/v1/payments/bills",
            )
        assert tok_rev_exc.value.code is ReasonCode.E_AUTH_TOKEN_REVOKED
        assert tok_rev_exc.value.http_status == 401

        # Freshly issued token after reinstatement works cleanly.
        fresh_token, _ = await _issue_for(
            token_service, clock, priv_a, client_id="agt_billpal", mandate_id="mnd_active"
        )
        proof4 = _build_proof(
            private_key=priv_a,
            method="POST",
            path="/v1/payments/bills",
            body=SAMPLE_BILL_BODY,
            iat=int(clock.now().timestamp()),
            access_token=fresh_token,
        )
        ctx = await identity_verifier.authenticate(
            SAMPLE_BILL_BODY,
            {"Authorization": f"DPoP {fresh_token}", "DPoP": proof4},
            "POST",
            "/v1/payments/bills",
        )
        assert ctx.agent_id == "agt_billpal"

    async def test_revocation_respects_caller_tx_rollback(self, token_env: TokenEnv) -> None:
        (
            _,
            session_factory,
            clock,
            _,
            _,
            _,
            token_service,
            identity_verifier,
            _,
            _,
            priv_a,
            _,
            _,
            _,
        ) = token_env
        token, _ = await _issue_for(
            token_service, clock, priv_a, client_id="agt_billpal", mandate_id="mnd_active"
        )

        # Rollback caller tx -> revocation must NOT persist.
        with session_factory() as session:
            with session.begin():
                tx = SqlAlchemyTx(id="tx_rb", session=session)
                await token_service.revoke_for_agent("agt_billpal", tx)
                session.rollback()

        proof = _build_proof(
            private_key=priv_a,
            method="POST",
            path="/v1/payments/bills",
            body=SAMPLE_BILL_BODY,
            iat=int(clock.now().timestamp()),
            access_token=token,
        )
        ctx = await identity_verifier.authenticate(
            SAMPLE_BILL_BODY,
            {"Authorization": f"DPoP {token}", "DPoP": proof},
            "POST",
            "/v1/payments/bills",
        )
        assert ctx.agent_id == "agt_billpal"


class TestTokenAttackAndMismatchCases:
    async def test_expired_token_unknown_key_and_stolen_token_with_wrong_key(
        self, token_env: TokenEnv
    ) -> None:
        (
            _,
            _,
            clock,
            _,
            _,
            _,
            token_service,
            identity_verifier,
            _,
            _,
            priv_a,
            agent_a,
            priv_b,
            _,
        ) = token_env

        token_a, _ = await _issue_for(
            token_service, clock, priv_a, client_id="agt_billpal", mandate_id="mnd_active"
        )

        # 1. Stolen token used with Agent B's registered key -> 401 E_AUTH_KEY_MISMATCH.
        stolen_proof = _build_proof(
            private_key=priv_b,
            method="POST",
            path="/v1/payments/bills",
            body=SAMPLE_BILL_BODY,
            iat=int(clock.now().timestamp()),
            access_token=token_a,
        )
        with pytest.raises(AuthenticationError) as km_exc:
            await identity_verifier.authenticate(
                SAMPLE_BILL_BODY,
                {"Authorization": f"DPoP {token_a}", "DPoP": stolen_proof},
                "POST",
                "/v1/payments/bills",
            )
        assert km_exc.value.code is ReasonCode.E_AUTH_KEY_MISMATCH
        assert km_exc.value.http_status == 401

        # 2. Token signed by an unknown Ed25519 key -> 401 E_AUTH_INVALID_TOKEN.
        rogue_signing_key, _ = generate_ed25519_keypair()
        now_ts = int(clock.now().timestamp())
        forged_token = jwt.encode(
            {
                "iss": AGENT_TOKEN_ISSUER,
                "aud": AGENT_TOKEN_AUDIENCE,
                "sub": "cust_001",
                "act": {"sub": "agt_billpal"},
                "mandate_id": "mnd_active",
                "mandate_hash": "sha256:fake",
                "cnf": {"jkt": agent_a.jkt},
                "iat": now_ts,
                "exp": now_ts + 300,
                "jti": "tok_forged_1",
            },
            rogue_signing_key,
            algorithm=AGENT_TOKEN_ALG,
            headers={"typ": AGENT_TOKEN_TYP},
        )
        forged_proof = _build_proof(
            private_key=priv_a,
            method="POST",
            path="/v1/payments/bills",
            body=SAMPLE_BILL_BODY,
            iat=now_ts,
            access_token=forged_token,
        )
        with pytest.raises(AuthenticationError) as unk_exc:
            await identity_verifier.authenticate(
                SAMPLE_BILL_BODY,
                {"Authorization": f"DPoP {forged_token}", "DPoP": forged_proof},
                "POST",
                "/v1/payments/bills",
            )
        assert unk_exc.value.code is ReasonCode.E_AUTH_INVALID_TOKEN
        assert unk_exc.value.http_status == 401

        # 3. Expired token -> 401 E_AUTH_EXPIRED; refreshing while mandate active succeeds!
        clock.advance(seconds=MAX_TOKEN_TTL_SECONDS + 1)
        expired_proof = _build_proof(
            private_key=priv_a,
            method="POST",
            path="/v1/payments/bills",
            body=SAMPLE_BILL_BODY,
            iat=int(clock.now().timestamp()),
            access_token=token_a,
        )
        with pytest.raises(AuthenticationError) as exp_exc:
            await identity_verifier.authenticate(
                SAMPLE_BILL_BODY,
                {"Authorization": f"DPoP {token_a}", "DPoP": expired_proof},
                "POST",
                "/v1/payments/bills",
            )
        assert exp_exc.value.code is ReasonCode.E_AUTH_EXPIRED
        assert exp_exc.value.http_status == 401

        refreshed, _ = await _issue_for(
            token_service, clock, priv_a, client_id="agt_billpal", mandate_id="mnd_active"
        )
        refreshed_proof = _build_proof(
            private_key=priv_a,
            method="POST",
            path="/v1/payments/bills",
            body=SAMPLE_BILL_BODY,
            iat=int(clock.now().timestamp()),
            access_token=refreshed,
        )
        ctx = await identity_verifier.authenticate(
            SAMPLE_BILL_BODY,
            {"Authorization": f"DPoP {refreshed}", "DPoP": refreshed_proof},
            "POST",
            "/v1/payments/bills",
        )
        assert ctx.mandate_id == "mnd_active"

    async def test_wrong_customer_wrong_agent_and_wrong_mandate_hash_bindings(
        self, token_env: TokenEnv
    ) -> None:
        (
            _,
            session_factory,
            clock,
            _,
            _,
            mandate_reader,
            _,
            identity_verifier,
            _,
            token_signing_key,
            priv_a,
            agent_a,
            _,
            _,
        ) = token_env

        now = clock.now()
        now_ts = int(now.timestamp())
        live_mandate = mandate_reader.mandates["mnd_active"]

        def _mint_persisted_token(
            *,
            customer_id: str,
            agent_id: str,
            mandate_id: str,
            m_hash: str,
        ) -> str:
            jti = f"tok_case_{secrets.token_hex(6)}"
            tok = jwt.encode(
                {
                    "iss": AGENT_TOKEN_ISSUER,
                    "aud": AGENT_TOKEN_AUDIENCE,
                    "sub": customer_id,
                    "act": {"sub": agent_id},
                    "mandate_id": mandate_id,
                    "mandate_hash": m_hash,
                    "cnf": {"jkt": agent_a.jkt},
                    "iat": now_ts,
                    "exp": now_ts + 300,
                    "jti": jti,
                },
                token_signing_key,
                algorithm=AGENT_TOKEN_ALG,
                headers={"typ": AGENT_TOKEN_TYP},
            )
            with session_factory() as session:
                with session.begin():
                    session.add(
                        IssuedTokenRecord(
                            jti=jti,
                            customer_id=customer_id,
                            agent_id=agent_id,
                            mandate_id=mandate_id,
                            mandate_hash=m_hash,
                            jkt=agent_a.jkt,
                            token_hash=sha256_hex(tok.encode("ascii")),
                            issued_at=now.isoformat().replace("+00:00", "Z"),
                            expires_at=(now + timedelta(seconds=300))
                            .isoformat()
                            .replace("+00:00", "Z"),
                            revoked_at=None,
                        )
                    )
            return tok

        cases = [
            # Wrong customer (token says cust_002, live mandate belongs to cust_001).
            _mint_persisted_token(
                customer_id="cust_002",
                agent_id="agt_billpal",
                mandate_id="mnd_active",
                m_hash=live_mandate.mandate_hash,
            ),
            # Wrong agent (token says agt_billpal, live mandate belongs to agt_paywise).
            _mint_persisted_token(
                customer_id="cust_001",
                agent_id="agt_billpal",
                mandate_id="mnd_other_agent",
                m_hash=mandate_reader.mandates["mnd_other_agent"].mandate_hash,
            ),
            # Wrong mandate hash binding.
            _mint_persisted_token(
                customer_id="cust_001",
                agent_id="agt_billpal",
                mandate_id="mnd_active",
                m_hash="sha256:" + ("f" * 64),
            ),
            # Unknown mandate ID.
            _mint_persisted_token(
                customer_id="cust_001",
                agent_id="agt_billpal",
                mandate_id="mnd_nonexistent",
                m_hash=live_mandate.mandate_hash,
            ),
        ]

        for bad_tok in cases:
            proof = _build_proof(
                private_key=priv_a,
                method="POST",
                path="/v1/payments/bills",
                body=SAMPLE_BILL_BODY,
                iat=now_ts,
                access_token=bad_tok,
            )
            with pytest.raises(AuthorizationError) as exc_info:
                await identity_verifier.authenticate(
                    SAMPLE_BILL_BODY,
                    {"Authorization": f"DPoP {bad_tok}", "DPoP": proof},
                    "POST",
                    "/v1/payments/bills",
                )
            assert exc_info.value.code is ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH
            assert exc_info.value.http_status == 403

    async def test_agent_token_cannot_access_human_endpoints_and_oauth_route_works(
        self, token_env: TokenEnv
    ) -> None:
        (
            _,
            _,
            clock,
            _,
            registry,
            _,
            token_service,
            _,
            human_auth,
            _,
            priv_a,
            _,
            _,
            _,
        ) = token_env

        app = FastAPI()
        install_identity_error_handlers(app)
        app.include_router(
            build_identity_router(
                registry=registry,
                human_auth=human_auth,
                token_service=token_service,
            )
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url=TRUSTED_ORIGIN
        ) as client:
            # 1. Valid POST /v1/oauth/token request.
            req_bytes = canonical_json({"client_id": "agt_billpal", "mandate_id": "mnd_active"})
            proof = _build_proof(
                private_key=priv_a,
                method="POST",
                path="/v1/oauth/token",
                body=req_bytes,
                iat=int(clock.now().timestamp()),
                jti="prf_http_tok_1",
            )
            resp = await client.post(
                "/v1/oauth/token",
                content=req_bytes,
                headers={"Content-Type": "application/json", "DPoP": proof},
            )
            assert resp.status_code == 200
            assert resp.headers["Cache-Control"] == "no-store"
            assert "X-Request-ID" in resp.headers
            body = resp.json()
            assert body["token_type"] == "DPoP"
            assert body["expires_in"] == MAX_TOKEN_TTL_SECONDS
            agent_token = body["access_token"]

            # Replaying the exact same proof to /v1/oauth/token -> 401 E_AUTH_REPLAY.
            replay_resp = await client.post(
                "/v1/oauth/token",
                content=req_bytes,
                headers={"Content-Type": "application/json", "DPoP": proof},
            )
            assert replay_resp.status_code == 401
            assert replay_resp.json()["error"]["code"] == ReasonCode.E_AUTH_REPLAY.value

            # Revoked mandate on /v1/oauth/token -> 403 E_MANDATE_REVOKED.
            rev_bytes = canonical_json({"client_id": "agt_billpal", "mandate_id": "mnd_revoked"})
            rev_proof = _build_proof(
                private_key=priv_a,
                method="POST",
                path="/v1/oauth/token",
                body=rev_bytes,
                iat=int(clock.now().timestamp()),
            )
            rev_resp = await client.post(
                "/v1/oauth/token",
                content=rev_bytes,
                headers={"Content-Type": "application/json", "DPoP": rev_proof},
            )
            assert rev_resp.status_code == 403
            assert rev_resp.json()["error"]["code"] == ReasonCode.E_MANDATE_REVOKED.value

            # 2. Agent token CANNOT authenticate to human_auth for ANY role.
            for role in (HumanRole.CUSTOMER, HumanRole.OPS, HumanRole.REGISTRAR):
                with pytest.raises(AuthenticationError) as role_exc:
                    await human_auth.authenticate(agent_token, role)
                assert role_exc.value.code is ReasonCode.E_AUTH_INVALID_TOKEN

            # 3. Agent token CANNOT call human HTTP endpoints (Bearer or DPoP).
            for scheme in ("Bearer", "DPoP"):
                ops_resp = await client.get(
                    "/v1/ops/agents",
                    headers={"Authorization": f"{scheme} {agent_token}"},
                )
                assert ops_resp.status_code == 401
                assert ops_resp.json()["error"]["code"] == ReasonCode.E_AUTH_INVALID_TOKEN.value

                reg_resp = await client.post(
                    "/v1/agents/register",
                    json={
                        "name": "Rogue",
                        "vendor": "Rogue",
                        "public_jwk": public_key_to_jwk(priv_a.public_key()),
                        "max_scopes": ["bill_pay"],
                    },
                    headers={"Authorization": f"{scheme} {agent_token}"},
                )
                assert reg_resp.status_code == 401
                assert reg_resp.json()["error"]["code"] == ReasonCode.E_AUTH_INVALID_TOKEN.value
