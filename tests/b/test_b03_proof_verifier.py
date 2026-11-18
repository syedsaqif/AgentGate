"""B03 unit and security tests for DPoP-style proof verification and atomic replay protection."""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
from collections.abc import AsyncIterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from contracts.common import ActionType, FinalDecision, RiskLevel, Tx
from contracts.core_models import ActionParams, CanonicalAction, Intent
from contracts.identity_models import AgentPublicIdentity
from contracts.reason_codes import ReasonCode
from contracts.risk_models import RiskInput, RiskResult
from gateway.identity.crypto import (
    access_token_hash_b64url,
    b64url_encode,
    body_sha256_b64url,
    canonical_json,
    ed25519_sign_b64url,
    generate_ed25519_keypair,
    public_key_to_jwk,
)
from gateway.identity.errors import AuthenticationError
from gateway.identity.proofs import (
    DPOP_ALG,
    DPOP_TYP,
    DPoPProofVerifier,
    build_trusted_htu,
    extract_dpop_header,
)
from gateway.identity.registry import SqlAgentRegistry
from gateway.identity.replay import SqlProofReplayCache
from gateway.identity.schema import (
    create_identity_schema,
    create_identity_session_factory,
    create_sqlite_identity_engine,
)
from tests.support.b.providers import MemoryAuditLog
from tests.support.platform import FrozenClock

TRUSTED_ORIGIN = "http://127.0.0.1:8000"
SAMPLE_ACCESS_TOKEN = "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.sample-access-token-payload.sig"
SAMPLE_BILL_BODY = canonical_json(
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


def _encode_jws(
    header: Mapping[str, Any] | dict[str, Any],
    payload: Mapping[str, Any] | dict[str, Any],
    private_key: Ed25519PrivateKey | None,
    *,
    raw_signature_b64: str | None = None,
) -> str:
    h_b64 = b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p_b64 = b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    if raw_signature_b64 is not None:
        s_b64 = raw_signature_b64
    elif private_key is not None:
        s_b64 = ed25519_sign_b64url(private_key, f"{h_b64}.{p_b64}".encode("ascii"))
    else:
        s_b64 = ""
    return f"{h_b64}.{p_b64}.{s_b64}"


def _build_valid_proof(
    *,
    private_key: Ed25519PrivateKey,
    method: str,
    htu: str,
    body: bytes,
    iat: int,
    jti: str | None = None,
    access_token: str | None = None,
    header_overrides: dict[str, Any] | None = None,
    claim_overrides: dict[str, Any] | None = None,
    omit_claims: set[str] | None = None,
) -> str:
    header: dict[str, Any] = {
        "typ": DPOP_TYP,
        "alg": DPOP_ALG,
        "jwk": public_key_to_jwk(private_key.public_key()),
    }
    if header_overrides:
        header.update(header_overrides)

    claims: dict[str, Any] = {
        "htm": method,
        "htu": htu,
        "iat": iat,
        "jti": jti if jti is not None else f"prf_{secrets.token_hex(8)}",
        "bsh": body_sha256_b64url(body),
    }
    if access_token is not None:
        claims["ath"] = access_token_hash_b64url(access_token)
    if claim_overrides:
        claims.update(claim_overrides)
    if omit_claims:
        for key in omit_claims:
            claims.pop(key, None)

    return _encode_jws(header, claims, private_key)


class _RecordingRiskEngine:
    """Test-only RiskEngine double verifying invalid proofs never pollute verified history."""

    def __init__(self) -> None:
        self.recorded_attempts: list[RiskInput] = []

    def score(self, risk_input: RiskInput) -> RiskResult:
        return RiskResult(
            score=10,
            level=RiskLevel.LOW,
            recommended=FinalDecision.ALLOW,
            signals=(),
            model_version="test-v1",
        )

    async def record_attempt(self, risk_input: RiskInput, tx: Tx) -> None:
        self.recorded_attempts.append(risk_input)

    async def record_outcome(self, outcome: object, tx: Tx) -> None:
        return None


type ProofEnv = tuple[
    Engine,
    sessionmaker[Session],
    FrozenClock,
    MemoryAuditLog,
    SqlAgentRegistry,
    SqlProofReplayCache,
    DPoPProofVerifier,
    Ed25519PrivateKey,
    AgentPublicIdentity,
]


@pytest.fixture
async def proof_env(tmp_path: Path) -> AsyncIterator[ProofEnv]:
    db_file = tmp_path / "proofs_b03.db"
    engine = create_sqlite_identity_engine(f"sqlite:///{db_file.as_posix()}")
    create_identity_schema(engine)
    session_factory = create_identity_session_factory(engine)
    clock = FrozenClock(datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC))
    audit_log = MemoryAuditLog()
    registry = SqlAgentRegistry(session_factory, clock, audit_log=audit_log)
    replay_cache = SqlProofReplayCache(session_factory, clock)
    verifier = DPoPProofVerifier(
        registry=registry,
        replay_cache=replay_cache,
        clock=clock,
        trusted_origin=TRUSTED_ORIGIN,
        audit_log=audit_log,
        clock_skew_seconds=60,
    )
    priv, pub = generate_ed25519_keypair()
    agent = await registry.register(
        name="BillPal",
        vendor="FinBot Labs",
        public_jwk=public_key_to_jwk(pub),
        max_scopes=[ActionType.BILL_PAY.value, ActionType.BALANCE_READ.value],
        agent_id="agt_billpal",
    )
    audit_log.events.clear()
    try:
        yield (
            engine,
            session_factory,
            clock,
            audit_log,
            registry,
            replay_cache,
            verifier,
            priv,
            agent,
        )
    finally:
        engine.dispose()


class TestValidProofs:
    async def test_valid_resource_post_proof_and_empty_body_get_proof(
        self, proof_env: ProofEnv
    ) -> None:
        _, _, clock, _, _, _, verifier, priv, agent = proof_env
        now_epoch = int(clock.now().timestamp())

        # Resource POST with body + ath.
        post_htu = f"{TRUSTED_ORIGIN}/v1/payments/bills"
        post_proof = _build_valid_proof(
            private_key=priv,
            method="POST",
            htu=post_htu,
            body=SAMPLE_BILL_BODY,
            iat=now_epoch,
            jti="prf_post_1",
            access_token=SAMPLE_ACCESS_TOKEN,
        )
        verified_post = await verifier.verify_proof(
            proof_jwt=post_proof,
            method="POST",
            request_url=f"{post_htu}?ignored=1#frag",
            request_bytes=SAMPLE_BILL_BODY,
            access_token=SAMPLE_ACCESS_TOKEN,
            expected_jkt=agent.jkt,
        )
        assert verified_post.agent == agent
        assert verified_post.jkt == agent.jkt
        assert verified_post.proof_jti == "prf_post_1"
        assert verified_post.htm == "POST"
        assert verified_post.htu == post_htu
        assert verified_post.ath == access_token_hash_b64url(SAMPLE_ACCESS_TOKEN)

        # Bodyless GET with empty bytes b"" and query string stripped from htu.
        get_htu = f"{TRUSTED_ORIGIN}/v1/accounts/ACC-001/transactions"
        get_proof = _build_valid_proof(
            private_key=priv,
            method="GET",
            htu=get_htu,
            body=b"",
            iat=now_epoch,
            jti="prf_get_1",
            access_token=SAMPLE_ACCESS_TOKEN,
        )
        verified_get = await verifier.verify_proof(
            proof_jwt=get_proof,
            method="GET",
            request_url="/v1/accounts/ACC-001/transactions?limit=20&cursor=abc",
            request_bytes=b"",
            access_token=SAMPLE_ACCESS_TOKEN,
            expected_jkt=agent.jkt,
        )
        assert verified_get.bsh == "47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU"
        assert verified_get.htu == get_htu

    async def test_valid_token_request_proof_has_no_ath_and_ignores_body_agent_id(
        self, proof_env: ProofEnv
    ) -> None:
        _, _, clock, _, _, _, verifier, priv, agent = proof_env
        now_epoch = int(clock.now().timestamp())
        # Body claims another agent ID; verifier MUST resolve strictly from registered key jkt.
        token_body = canonical_json({"client_id": "agt_attacker_spoof", "mandate_id": "mnd_1"})
        token_htu = f"{TRUSTED_ORIGIN}/v1/oauth/token"
        proof = _build_valid_proof(
            private_key=priv,
            method="POST",
            htu=token_htu,
            body=token_body,
            iat=now_epoch,
            jti="prf_tok_1",
            access_token=None,
        )
        verified = await verifier.verify_proof(
            proof_jwt=proof,
            method="POST",
            request_url="/v1/oauth/token",
            request_bytes=token_body,
            access_token=None,
        )
        assert verified.agent.agent_id == agent.agent_id == "agt_billpal"
        assert verified.ath is None

        # Token request proof containing unexpected ath MUST be rejected.
        bad_tok_proof = _build_valid_proof(
            private_key=priv,
            method="POST",
            htu=token_htu,
            body=token_body,
            iat=now_epoch,
            jti="prf_tok_2",
            access_token=SAMPLE_ACCESS_TOKEN,
        )
        with pytest.raises(AuthenticationError) as exc_info:
            await verifier.verify_proof(
                proof_jwt=bad_tok_proof,
                method="POST",
                request_url="/v1/oauth/token",
                request_bytes=token_body,
                access_token=None,
            )
        assert exc_info.value.code is ReasonCode.E_AUTH_PROOF_INVALID

    async def test_extract_dpop_header_and_trusted_origin_ignores_spoofed_host(
        self, proof_env: ProofEnv
    ) -> None:
        _, _, clock, _, _, _, verifier, priv, agent = proof_env
        now_epoch = int(clock.now().timestamp())

        headers = {
            "Host": "evil.attacker.example",
            "X-Forwarded-Host": "evil.attacker.example",
            "X-Forwarded-Proto": "https",
            "Forwarded": "host=evil.attacker.example;proto=https",
            "dPoP": _build_valid_proof(
                private_key=priv,
                method="POST",
                htu=f"{TRUSTED_ORIGIN}/v1/payments/bills",
                body=SAMPLE_BILL_BODY,
                iat=now_epoch,
                jti="prf_hdr_1",
                access_token=SAMPLE_ACCESS_TOKEN,
            ),
        }
        extracted = extract_dpop_header(headers)
        assert extracted is not None
        verified = await verifier.verify_proof(
            proof_jwt=extracted,
            method="POST",
            request_url="https://evil.attacker.example/v1/payments/bills?x=1",
            request_bytes=SAMPLE_BILL_BODY,
            access_token=SAMPLE_ACCESS_TOKEN,
            expected_jkt=agent.jkt,
        )
        assert verified.htu == build_trusted_htu(TRUSTED_ORIGIN, "/v1/payments/bills")


class TestProofSecurityRejections:
    async def test_missing_or_malformed_proof_rejected(self, proof_env: ProofEnv) -> None:
        _, _, _, _, _, _, verifier, _, _ = proof_env
        with pytest.raises(AuthenticationError) as missing_exc:
            await verifier.verify_proof(
                proof_jwt=None,
                method="POST",
                request_url="/v1/payments/bills",
                request_bytes=SAMPLE_BILL_BODY,
            )
        assert missing_exc.value.code is ReasonCode.E_AUTH_MISSING
        assert missing_exc.value.http_status == 401

        for malformed in ("only.two", "a.b.c.d", "not-base64.not-base64.sig"):
            with pytest.raises(AuthenticationError) as exc_info:
                await verifier.verify_proof(
                    proof_jwt=malformed,
                    method="POST",
                    request_url="/v1/payments/bills",
                    request_bytes=SAMPLE_BILL_BODY,
                )
            assert exc_info.value.code is ReasonCode.E_AUTH_PROOF_INVALID

    async def test_invalid_signature_and_unknown_key_indistinguishable_publicly(
        self, proof_env: ProofEnv
    ) -> None:
        _, _, clock, audit_log, _, replay_cache, verifier, priv, agent = proof_env
        now_epoch = int(clock.now().timestamp())
        htu = f"{TRUSTED_ORIGIN}/v1/payments/bills"

        # 1. Invalid signature (wrong signing key while advertising registered public JWK).
        attacker_priv, _ = generate_ed25519_keypair()
        bad_sig_proof = _encode_jws(
            {"typ": DPOP_TYP, "alg": DPOP_ALG, "jwk": agent.public_jwk},
            {
                "htm": "POST",
                "htu": htu,
                "iat": now_epoch,
                "jti": "prf_sig_tamper_1",
                "bsh": body_sha256_b64url(SAMPLE_BILL_BODY),
                "ath": access_token_hash_b64url(SAMPLE_ACCESS_TOKEN),
            },
            attacker_priv,
        )
        with pytest.raises(AuthenticationError) as bad_sig_exc:
            await verifier.verify_proof(
                proof_jwt=bad_sig_proof,
                method="POST",
                request_url="/v1/payments/bills",
                request_bytes=SAMPLE_BILL_BODY,
                access_token=SAMPLE_ACCESS_TOKEN,
            )

        # Replay cache MUST NOT have been polluted by the invalid signature!
        assert await replay_cache.is_claimed(agent.jkt, "prf_sig_tamper_1") is False

        # Legitimate agent can still use that exact jti afterwards.
        valid_same_jti = _build_valid_proof(
            private_key=priv,
            method="POST",
            htu=htu,
            body=SAMPLE_BILL_BODY,
            iat=now_epoch,
            jti="prf_sig_tamper_1",
            access_token=SAMPLE_ACCESS_TOKEN,
        )
        ok = await verifier.verify_proof(
            proof_jwt=valid_same_jti,
            method="POST",
            request_url="/v1/payments/bills",
            request_bytes=SAMPLE_BILL_BODY,
            access_token=SAMPLE_ACCESS_TOKEN,
        )
        assert ok.proof_jti == "prf_sig_tamper_1"

        # 2. Unknown / unregistered key.
        unknown_priv, _ = generate_ed25519_keypair()
        unknown_proof = _build_valid_proof(
            private_key=unknown_priv,
            method="POST",
            htu=htu,
            body=SAMPLE_BILL_BODY,
            iat=now_epoch,
            jti="prf_unknown_1",
            access_token=SAMPLE_ACCESS_TOKEN,
        )
        with pytest.raises(AuthenticationError) as unknown_exc:
            await verifier.verify_proof(
                proof_jwt=unknown_proof,
                method="POST",
                request_url="/v1/payments/bills",
                request_bytes=SAMPLE_BILL_BODY,
                access_token=SAMPLE_ACCESS_TOKEN,
            )

        # Public error response is indistinguishable; internal audit reasons stay unattributed.
        assert bad_sig_exc.value.code is unknown_exc.value.code is ReasonCode.E_AUTH_PROOF_INVALID
        assert bad_sig_exc.value.http_status == unknown_exc.value.http_status == 401
        assert bad_sig_exc.value.message == unknown_exc.value.message
        assert bad_sig_exc.value.internal_reason == "invalid_proof_signature"
        assert unknown_exc.value.internal_reason == "unknown_agent_key"
        assert all(e.agent_id is None for e in audit_log.events)

    async def test_wrong_typ_alg_and_embedded_private_jwk_rejected(
        self, proof_env: ProofEnv
    ) -> None:
        _, _, clock, _, _, _, verifier, priv, _ = proof_env
        now_epoch = int(clock.now().timestamp())
        htu = f"{TRUSTED_ORIGIN}/v1/payments/bills"

        # Wrong typ.
        wrong_typ = _build_valid_proof(
            private_key=priv,
            method="POST",
            htu=htu,
            body=SAMPLE_BILL_BODY,
            iat=now_epoch,
            header_overrides={"typ": "JWT"},
            access_token=SAMPLE_ACCESS_TOKEN,
        )
        with pytest.raises(AuthenticationError) as typ_exc:
            await verifier.verify_proof(
                proof_jwt=wrong_typ,
                method="POST",
                request_url="/v1/payments/bills",
                request_bytes=SAMPLE_BILL_BODY,
                access_token=SAMPLE_ACCESS_TOKEN,
            )
        assert typ_exc.value.code is ReasonCode.E_AUTH_PROOF_INVALID
        assert typ_exc.value.internal_reason == "invalid_proof_typ"

        # Wrong / disallowed alg (none, HS256, ES256, RS256).
        for bad_alg in ("none", "HS256", "ES256", "RS256"):
            bad_alg_proof = _build_valid_proof(
                private_key=priv,
                method="POST",
                htu=htu,
                body=SAMPLE_BILL_BODY,
                iat=now_epoch,
                header_overrides={"alg": bad_alg},
                access_token=SAMPLE_ACCESS_TOKEN,
            )
            with pytest.raises(AuthenticationError) as alg_exc:
                await verifier.verify_proof(
                    proof_jwt=bad_alg_proof,
                    method="POST",
                    request_url="/v1/payments/bills",
                    request_bytes=SAMPLE_BILL_BODY,
                    access_token=SAMPLE_ACCESS_TOKEN,
                )
            assert alg_exc.value.code is ReasonCode.E_AUTH_PROOF_INVALID
            assert alg_exc.value.internal_reason == "unsupported_proof_alg"

        # Embedded private JWK parameter 'd' must be rejected.
        pub_jwk = public_key_to_jwk(priv.public_key())
        private_jwk_proof = _build_valid_proof(
            private_key=priv,
            method="POST",
            htu=htu,
            body=SAMPLE_BILL_BODY,
            iat=now_epoch,
            header_overrides={
                "jwk": {**pub_jwk, "d": "nWGxne_9WmC6hEr0kuwsxERJxWl7MmkZcDusAxyuf2A"}
            },
            access_token=SAMPLE_ACCESS_TOKEN,
        )
        with pytest.raises(AuthenticationError) as jwk_exc:
            await verifier.verify_proof(
                proof_jwt=private_jwk_proof,
                method="POST",
                request_url="/v1/payments/bills",
                request_bytes=SAMPLE_BILL_BODY,
                access_token=SAMPLE_ACCESS_TOKEN,
            )
        assert jwk_exc.value.code is ReasonCode.E_AUTH_PROOF_INVALID
        assert jwk_exc.value.internal_reason == "invalid_proof_jwk"

    async def test_wrong_htm_htu_body_ath_jti_and_expected_jkt_rejected(
        self, proof_env: ProofEnv
    ) -> None:
        _, _, clock, _, _, _, verifier, priv, agent = proof_env
        now_epoch = int(clock.now().timestamp())
        htu = f"{TRUSTED_ORIGIN}/v1/payments/bills"

        # Wrong HTTP method (htm).
        wrong_htm = _build_valid_proof(
            private_key=priv,
            method="GET",
            htu=htu,
            body=SAMPLE_BILL_BODY,
            iat=now_epoch,
            access_token=SAMPLE_ACCESS_TOKEN,
        )
        with pytest.raises(AuthenticationError) as htm_exc:
            await verifier.verify_proof(
                proof_jwt=wrong_htm,
                method="POST",
                request_url="/v1/payments/bills",
                request_bytes=SAMPLE_BILL_BODY,
                access_token=SAMPLE_ACCESS_TOKEN,
            )
        assert htm_exc.value.internal_reason == "proof_htm_mismatch"

        # Wrong URL (different route, spoofed origin, or query included in htu).
        for bad_htu in (
            f"{TRUSTED_ORIGIN}/v1/transfers",
            "https://evil.example/v1/payments/bills",
            f"{htu}?amount=1",
            f"{htu}#frag",
        ):
            wrong_htu = _build_valid_proof(
                private_key=priv,
                method="POST",
                htu=bad_htu,
                body=SAMPLE_BILL_BODY,
                iat=now_epoch,
                access_token=SAMPLE_ACCESS_TOKEN,
            )
            with pytest.raises(AuthenticationError) as htu_exc:
                await verifier.verify_proof(
                    proof_jwt=wrong_htu,
                    method="POST",
                    request_url="/v1/payments/bills",
                    request_bytes=SAMPLE_BILL_BODY,
                    access_token=SAMPLE_ACCESS_TOKEN,
                )
            assert htu_exc.value.internal_reason == "proof_htu_mismatch"

        # Modified body (+1 paise tampering in transit).
        tampered_body = SAMPLE_BILL_BODY.replace(b"480000", b"480001")
        body_proof = _build_valid_proof(
            private_key=priv,
            method="POST",
            htu=htu,
            body=SAMPLE_BILL_BODY,
            iat=now_epoch,
            access_token=SAMPLE_ACCESS_TOKEN,
        )
        with pytest.raises(AuthenticationError) as bsh_exc:
            await verifier.verify_proof(
                proof_jwt=body_proof,
                method="POST",
                request_url="/v1/payments/bills",
                request_bytes=tampered_body,
                access_token=SAMPLE_ACCESS_TOKEN,
            )
        assert bsh_exc.value.internal_reason == "proof_bsh_mismatch"

        # Wrong access-token hash (ath).
        wrong_ath = _build_valid_proof(
            private_key=priv,
            method="POST",
            htu=htu,
            body=SAMPLE_BILL_BODY,
            iat=now_epoch,
            access_token="different.access.token",
        )
        with pytest.raises(AuthenticationError) as ath_exc:
            await verifier.verify_proof(
                proof_jwt=wrong_ath,
                method="POST",
                request_url="/v1/payments/bills",
                request_bytes=SAMPLE_BILL_BODY,
                access_token=SAMPLE_ACCESS_TOKEN,
            )
        assert ath_exc.value.internal_reason == "proof_ath_mismatch"

        # Missing jti.
        missing_jti = _build_valid_proof(
            private_key=priv,
            method="POST",
            htu=htu,
            body=SAMPLE_BILL_BODY,
            iat=now_epoch,
            access_token=SAMPLE_ACCESS_TOKEN,
            omit_claims={"jti"},
        )
        with pytest.raises(AuthenticationError) as jti_exc:
            await verifier.verify_proof(
                proof_jwt=missing_jti,
                method="POST",
                request_url="/v1/payments/bills",
                request_bytes=SAMPLE_BILL_BODY,
                access_token=SAMPLE_ACCESS_TOKEN,
            )
        assert jti_exc.value.internal_reason == "missing_or_invalid_proof_jti"

        # Mismatched expected_jkt (stolen token used with another registered agent's key).
        valid_proof = _build_valid_proof(
            private_key=priv,
            method="POST",
            htu=htu,
            body=SAMPLE_BILL_BODY,
            iat=now_epoch,
            access_token=SAMPLE_ACCESS_TOKEN,
        )
        with pytest.raises(AuthenticationError) as km_exc:
            await verifier.verify_proof(
                proof_jwt=valid_proof,
                method="POST",
                request_url="/v1/payments/bills",
                request_bytes=SAMPLE_BILL_BODY,
                access_token=SAMPLE_ACCESS_TOKEN,
                expected_jkt="different-bound-token-jkt",
            )
        assert km_exc.value.code is ReasonCode.E_AUTH_KEY_MISMATCH
        assert agent.jkt != "different-bound-token-jkt"

    async def test_clock_skew_boundaries_and_future_dated_replay_retention(
        self, proof_env: ProofEnv
    ) -> None:
        _, _, clock, _, _, replay_cache, verifier, priv, agent = proof_env
        now_epoch = int(clock.now().timestamp())
        htu = f"{TRUSTED_ORIGIN}/v1/payments/bills"

        # Expired (-61s) and future-dated (+61s) proofs rejected with E_AUTH_CLOCK_SKEW.
        for bad_iat in (now_epoch - 61, now_epoch + 61):
            skew_proof = _build_valid_proof(
                private_key=priv,
                method="POST",
                htu=htu,
                body=SAMPLE_BILL_BODY,
                iat=bad_iat,
                access_token=SAMPLE_ACCESS_TOKEN,
            )
            with pytest.raises(AuthenticationError) as skew_exc:
                await verifier.verify_proof(
                    proof_jwt=skew_proof,
                    method="POST",
                    request_url="/v1/payments/bills",
                    request_bytes=SAMPLE_BILL_BODY,
                    access_token=SAMPLE_ACCESS_TOKEN,
                )
            assert skew_exc.value.code is ReasonCode.E_AUTH_CLOCK_SKEW

        # Future-dated at +60s boundary is accepted once, and MUST remain blocked for full
        # acceptance window (through now + 120s, i.e., iat + 60s) even across pruning!
        future_proof = _build_valid_proof(
            private_key=priv,
            method="POST",
            htu=htu,
            body=SAMPLE_BILL_BODY,
            iat=now_epoch + 60,
            jti="prf_future_edge",
            access_token=SAMPLE_ACCESS_TOKEN,
        )
        first = await verifier.verify_proof(
            proof_jwt=future_proof,
            method="POST",
            request_url="/v1/payments/bills",
            request_bytes=SAMPLE_BILL_BODY,
            access_token=SAMPLE_ACCESS_TOKEN,
        )
        assert first.proof_jti == "prf_future_edge"

        # Advance +60s (now == iat) -> prune must retain it, replay must fail with E_AUTH_REPLAY.
        clock.advance(seconds=60)
        assert await replay_cache.prune_expired() == 0
        with pytest.raises(AuthenticationError) as rep1:
            await verifier.verify_proof(
                proof_jwt=future_proof,
                method="POST",
                request_url="/v1/payments/bills",
                request_bytes=SAMPLE_BILL_BODY,
                access_token=SAMPLE_ACCESS_TOKEN,
            )
        assert rep1.value.code is ReasonCode.E_AUTH_REPLAY

        # Advance another +60s (now == iat + 60, final valid second) -> STILL retained & replayed!
        clock.advance(seconds=60)
        assert await replay_cache.prune_expired() == 0
        assert await replay_cache.is_claimed(agent.jkt, "prf_future_edge") is True
        with pytest.raises(AuthenticationError) as rep2:
            await verifier.verify_proof(
                proof_jwt=future_proof,
                method="POST",
                request_url="/v1/payments/bills",
                request_bytes=SAMPLE_BILL_BODY,
                access_token=SAMPLE_ACCESS_TOKEN,
            )
        assert rep2.value.code is ReasonCode.E_AUTH_REPLAY

        # Advance +1s (now == iat + 61) -> proof is now outside skew window and safely prunable.
        clock.advance(seconds=1)
        assert await replay_cache.prune_expired() == 1
        with pytest.raises(AuthenticationError) as expired_after:
            await verifier.verify_proof(
                proof_jwt=future_proof,
                method="POST",
                request_url="/v1/payments/bills",
                request_bytes=SAMPLE_BILL_BODY,
                access_token=SAMPLE_ACCESS_TOKEN,
            )
        assert expired_after.value.code is ReasonCode.E_AUTH_CLOCK_SKEW


class TestReplayAtomicityAndRiskIsolation:
    async def test_replayed_proof_rejected_sequentially_and_under_concurrent_threads(
        self, proof_env: ProofEnv
    ) -> None:
        _, _, clock, audit_log, _, _, verifier, priv, _ = proof_env
        now_epoch = int(clock.now().timestamp())
        htu = f"{TRUSTED_ORIGIN}/v1/payments/bills"

        # Sequential replay check.
        seq_proof = _build_valid_proof(
            private_key=priv,
            method="POST",
            htu=htu,
            body=SAMPLE_BILL_BODY,
            iat=now_epoch,
            jti="prf_seq_replay",
            access_token=SAMPLE_ACCESS_TOKEN,
        )
        await verifier.verify_proof(
            proof_jwt=seq_proof,
            method="POST",
            request_url="/v1/payments/bills",
            request_bytes=SAMPLE_BILL_BODY,
            access_token=SAMPLE_ACCESS_TOKEN,
        )
        with pytest.raises(AuthenticationError) as replay_exc:
            await verifier.verify_proof(
                proof_jwt=seq_proof,
                method="POST",
                request_url="/v1/payments/bills",
                request_bytes=SAMPLE_BILL_BODY,
                access_token=SAMPLE_ACCESS_TOKEN,
            )
        assert replay_exc.value.code is ReasonCode.E_AUTH_REPLAY
        assert replay_exc.value.http_status == 401

        # Concurrent simultaneous requests across OS threads synchronized at a barrier:
        # EXACTLY ONE winner, all others fail with E_AUTH_REPLAY.
        workers = 8
        barrier = threading.Barrier(workers)
        race_proof = _build_valid_proof(
            private_key=priv,
            method="POST",
            htu=htu,
            body=SAMPLE_BILL_BODY,
            iat=now_epoch,
            jti="prf_concurrent_race",
            access_token=SAMPLE_ACCESS_TOKEN,
        )

        def _attempt() -> str:
            barrier.wait()
            try:
                asyncio.run(
                    verifier.verify_proof(
                        proof_jwt=race_proof,
                        method="POST",
                        request_url="/v1/payments/bills",
                        request_bytes=SAMPLE_BILL_BODY,
                        access_token=SAMPLE_ACCESS_TOKEN,
                    )
                )
                return "WIN"
            except AuthenticationError as exc:
                return exc.code.value

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_attempt) for _ in range(workers)]
            outcomes = [f.result() for f in futures]

        assert outcomes.count("WIN") == 1
        assert outcomes.count(ReasonCode.E_AUTH_REPLAY.value) == workers - 1

        # Ensure raw proof JWS and raw access token NEVER appear in audit events.
        audit_blob = json.dumps([e.model_dump(mode="json") for e in audit_log.events])
        assert seq_proof not in audit_blob
        assert race_proof not in audit_blob
        assert SAMPLE_ACCESS_TOKEN not in audit_blob

    async def test_invalid_proof_never_mutates_verified_risk_history(
        self, proof_env: ProofEnv
    ) -> None:
        _, _, clock, audit_log, _, _, verifier, priv, agent = proof_env
        risk_engine = _RecordingRiskEngine()
        now = clock.now()
        now_epoch = int(now.timestamp())
        htu = f"{TRUSTED_ORIGIN}/v1/payments/bills"

        class _DummyTx:
            id = "tx_dummy"

        async def _guarded_request(proof_jwt: str, body: bytes) -> None:
            verified = await verifier.verify_proof(
                proof_jwt=proof_jwt,
                method="POST",
                request_url="/v1/payments/bills",
                request_bytes=body,
                access_token=SAMPLE_ACCESS_TOKEN,
                expected_jkt=agent.jkt,
            )
            action = CanonicalAction(
                request_id="req_risk_1",
                customer_id="cust_001",
                agent_id=verified.agent.agent_id,
                mandate_id="mnd_demo_1",
                mandate_hash="sha256:demo",
                action_type=ActionType.BILL_PAY,
                params=ActionParams(
                    from_account="ACC-001",
                    payee_id="BILL-ELEC",
                    amount_paise=480000,
                    currency="INR",
                ),
                intent=Intent(task="pay_electricity_bill", category="utilities.electricity"),
                idempotency_key="bill-1",
                received_at=now,
            )
            # Only reached after cryptographic verification succeeds.
            await risk_engine.record_attempt(
                RiskInput.model_construct(attempt_id="att_1", action=action, now=now),
                _DummyTx(),
            )

        # Invalid proof (modified body) and unknown key MUST fail before touching risk history.
        bad_body_proof = _build_valid_proof(
            private_key=priv,
            method="POST",
            htu=htu,
            body=SAMPLE_BILL_BODY,
            iat=now_epoch,
            jti="prf_risk_bad_1",
            access_token=SAMPLE_ACCESS_TOKEN,
        )
        with pytest.raises(AuthenticationError):
            await _guarded_request(bad_body_proof, SAMPLE_BILL_BODY + b" ")

        unknown_priv, _ = generate_ed25519_keypair()
        unknown_proof = _build_valid_proof(
            private_key=unknown_priv,
            method="POST",
            htu=htu,
            body=SAMPLE_BILL_BODY,
            iat=now_epoch,
            jti="prf_risk_bad_2",
            access_token=SAMPLE_ACCESS_TOKEN,
        )
        with pytest.raises(AuthenticationError):
            await _guarded_request(unknown_proof, SAMPLE_BILL_BODY)

        assert len(risk_engine.recorded_attempts) == 0
        assert len(audit_log.events) >= 2

        # Valid proof succeeds and records exactly 1 verified attempt.
        valid_proof = _build_valid_proof(
            private_key=priv,
            method="POST",
            htu=htu,
            body=SAMPLE_BILL_BODY,
            iat=now_epoch,
            jti="prf_risk_ok_1",
            access_token=SAMPLE_ACCESS_TOKEN,
        )
        await _guarded_request(valid_proof, SAMPLE_BILL_BODY)
        assert len(risk_engine.recorded_attempts) == 1
