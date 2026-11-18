"""B05 unit, security, mock-transport, CLI, and gateway-interop tests for the Python Agent SDK."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI, Request
from pydantic import ValidationError

from agent_sdk import (
    AgentAPIError,
    AgentGateClient,
    MoneyActionPayload,
    RetryPolicy,
    generate_agent_keypair,
    load_private_key_pem,
    save_private_key_pem,
    save_public_jwk,
)
from agent_sdk.cli import main as cli_main
from contracts.canonical import sha256_base64url
from contracts.common import ActionType
from contracts.core_models import (
    Grant,
    MandateSnapshot,
    MandateStatus,
    MandateTerms,
    StepUpRules,
)
from contracts.reason_codes import ReasonCode
from gateway.identity.crypto import (
    ed25519_verify_b64url,
    jwk_to_public_key,
    mandate_hash,
    sign_prefixed_sha256,
)
from gateway.identity.human_auth import HumanSessionKeyring, SqlHumanAuthService
from gateway.identity.proofs import DPoPProofVerifier
from gateway.identity.registry import SqlAgentRegistry
from gateway.identity.replay import SqlProofReplayCache
from gateway.identity.routes import build_identity_router, install_identity_error_handlers
from gateway.identity.schema import (
    create_identity_schema,
    create_identity_session_factory,
    create_sqlite_identity_engine,
)
from gateway.identity.tokens import SqlIdentityVerifier, SqlTokenService
from tests.support.b.providers import FixtureMandateReader, MemoryAuditLog
from tests.support.platform import FrozenClock

GATEWAY_URL = "http://127.0.0.1:8000"


def _decode_jws_parts(compact: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    header_b64, payload_b64, sig_b64 = compact.split(".")

    def _dec(part: str) -> dict[str, Any]:
        padded = part + "=" * ((4 - len(part) % 4) % 4)
        parsed: Any = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        assert isinstance(parsed, dict)
        return parsed

    return _dec(header_b64), _dec(payload_b64), sig_b64


async def _noop_sleep(_seconds: float) -> None:
    return None


class TestSDKKeysAndSerialization:
    def test_keygen_save_load_overwrite_protection_and_non_ed25519_rejection(
        self, tmp_path: Path
    ) -> None:
        kp = generate_agent_keypair()
        key_path = tmp_path / "keys" / "agent.pem"
        pub_path = tmp_path / "keys" / "agent_pub.json"

        save_private_key_pem(kp, key_path)
        assert key_path.exists()

        # Refuses overwrite unless overwrite=True.
        with pytest.raises(FileExistsError):
            save_private_key_pem(kp, key_path)

        loaded = load_private_key_pem(key_path)
        assert loaded.jkt == kp.jkt
        assert loaded.public_jwk() == kp.public_jwk()

        # Encrypted PEM round-trip.
        enc_path = tmp_path / "keys" / "agent_enc.pem"
        save_private_key_pem(kp, enc_path, password=b"local-test-pass")
        assert load_private_key_pem(enc_path, password=b"local-test-pass").jkt == kp.jkt

        # Public JWK export contains no private parameter 'd'.
        save_public_jwk(kp, pub_path, kid="k1")
        pub_json = json.loads(pub_path.read_text(encoding="utf-8"))
        assert pub_json["jkt"] == kp.jkt
        assert pub_json["public_jwk"]["kid"] == "k1"
        assert "d" not in pub_json["public_jwk"]

        # Reject non-Ed25519 PEM (e.g. ECDSA P-256).
        ec_key = ec.generate_private_key(ec.SECP256R1())
        ec_path = tmp_path / "keys" / "ec_bad.pem"
        ec_path.write_bytes(
            ec_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        with pytest.raises(ValueError, match="Only Ed25519"):
            load_private_key_pem(ec_path)

        # repr must not expose private key bytes.
        assert "PRIVATE KEY" not in repr(kp)

    def test_money_payload_enforces_strict_paise_and_inr(self) -> None:
        with pytest.raises(ValidationError):
            MoneyActionPayload(
                from_account="ACC-001",
                payee_id="BILL-ELEC",
                amount_paise=0,
                currency="INR",
                intent={"task": "pay", "category": "utilities"},  # type: ignore[arg-type]
            )
        with pytest.raises(ValidationError):
            MoneyActionPayload(
                from_account="ACC-001",
                payee_id="BILL-ELEC",
                amount_paise=100.5,  # type: ignore[arg-type]
                currency="INR",
                intent={"task": "pay", "category": "utilities"},  # type: ignore[arg-type]
            )
        with pytest.raises(ValidationError):
            MoneyActionPayload(
                from_account="ACC-001",
                payee_id="BILL-ELEC",
                amount_paise=100,
                currency="USD",  # type: ignore[arg-type]
                intent={"task": "pay", "category": "utilities"},  # type: ignore[arg-type]
            )


class TestSDKMockTransportSecurity:
    async def test_exact_transmitted_bytes_bsh_method_url_and_ath_verified(self) -> None:
        kp = generate_agent_keypair()
        clock = FrozenClock(datetime(2026, 11, 18, 15, 0, 0, tzinfo=UTC))
        issued_token = "sdk.test.access.token.v1"
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            dpop = request.headers["DPoP"]
            header, claims, sig = _decode_jws_parts(dpop)
            assert header["typ"] == "dpop+jwt"
            assert header["alg"] == "EdDSA"
            assert "d" not in header["jwk"]

            # Verify cryptographic signature over header.payload.
            h_b64, p_b64, _ = dpop.split(".")
            pub = jwk_to_public_key(header["jwk"])
            assert ed25519_verify_b64url(pub, f"{h_b64}.{p_b64}".encode("ascii"), sig)

            # Exact transmitted wire bytes MUST match proof bsh!
            assert claims["bsh"] == sha256_base64url(request.content)
            assert claims["htm"] == request.method
            assert claims["htu"] == f"{GATEWAY_URL}{request.url.path}"
            assert "?" not in claims["htu"] and "#" not in claims["htu"]

            if request.url.path == "/v1/oauth/token":
                assert "ath" not in claims
                assert "Authorization" not in request.headers
                return httpx.Response(
                    200,
                    json={
                        "access_token": issued_token,
                        "token_type": "DPoP",
                        "expires_in": 300,
                    },
                )

            assert request.headers["Authorization"] == f"DPoP {issued_token}"
            assert claims["ath"] == sha256_base64url(issued_token.encode("ascii"))

            if request.url.path == "/v1/payments/bills":
                assert request.headers["Idempotency-Key"] == "bill-nov-001"
                return httpx.Response(
                    200, json={"request_id": "req_1", "status": "EXECUTED"}
                )
            if request.url.path == "/v1/accounts/ACC-001/transactions":
                assert request.content == b""
                assert claims["bsh"] == "47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU"
                return httpx.Response(200, json={"items": [], "next_cursor": None})

            return httpx.Response(404, json={"error": {"code": "E_REQ_INVALID", "message": "x"}})

        transport = httpx.MockTransport(handler)
        async with AgentGateClient(
            gateway_url=GATEWAY_URL,
            agent_id="agt_billpal",
            mandate_id="mnd_demo_1",
            keypair=kp,
            clock=clock,
            transport=transport,
            sleep_fn=_noop_sleep,
        ) as client:
            pay_res = await client.pay_bill(
                from_account="ACC-001",
                payee_id="BILL-ELEC",
                amount_paise=480000,
                reference="NOV-2026",
                task="pay_electricity_bill",
                category="utilities.electricity",
                note="Monthly bill",
                idempotency_key="bill-nov-001",
            )
            assert pay_res["status"] == "EXECUTED"

            tx_res = await client.list_transactions("ACC-001", cursor="cur_1", limit=25)
            assert tx_res["items"] == []

        # 1 token fetch + 1 bill_pay + 1 list_transactions (token cached across resource calls).
        assert [r.url.path for r in captured] == [
            "/v1/oauth/token",
            "/v1/payments/bills",
            "/v1/accounts/ACC-001/transactions",
        ]

    async def test_retry_uses_fresh_proof_jti_preserves_idempotency_key_and_exact_body(
        self,
    ) -> None:
        kp = generate_agent_keypair()
        clock = FrozenClock(datetime(2026, 11, 18, 15, 0, 0, tzinfo=UTC))
        bill_attempts: list[tuple[str, str, bytes, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/oauth/token":
                return httpx.Response(
                    200,
                    json={"access_token": "tok.retry.1", "token_type": "DPoP", "expires_in": 300},
                )
            if request.url.path == "/v1/payments/bills":
                _, claims, _ = _decode_jws_parts(request.headers["DPoP"])
                bill_attempts.append(
                    (
                        str(claims["jti"]),
                        request.headers["Idempotency-Key"],
                        bytes(request.content),
                        str(claims["bsh"]),
                    )
                )
                if len(bill_attempts) == 1:
                    raise httpx.ConnectError("transient network drop")
                if len(bill_attempts) == 2:
                    return httpx.Response(
                        503,
                        json={
                            "error": {
                                "code": ReasonCode.E_SERVICE_UNAVAILABLE.value,
                                "message": "Temporary DB busy",
                                "request_id": "req_busy",
                            }
                        },
                    )
                return httpx.Response(200, json={"request_id": "req_ok", "status": "EXECUTED"})
            raise AssertionError(f"Unexpected path {request.url.path}")

        transport = httpx.MockTransport(handler)
        async with AgentGateClient(
            gateway_url=GATEWAY_URL,
            agent_id="agt_billpal",
            mandate_id="mnd_demo_1",
            keypair=kp,
            clock=clock,
            retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0.0),
            transport=transport,
            sleep_fn=_noop_sleep,
        ) as client:
            result = await client.pay_bill(
                from_account="ACC-001",
                payee_id="BILL-ELEC",
                amount_paise=185000,
                task="pay_electricity_bill",
                category="utilities.electricity",
                idempotency_key="bill-stable-key-99",
            )
            assert result["status"] == "EXECUTED"

        assert len(bill_attempts) == 3
        jtis = [row[0] for row in bill_attempts]
        idem_keys = [row[1] for row in bill_attempts]
        bodies = [row[2] for row in bill_attempts]
        bshs = [row[3] for row in bill_attempts]

        # Every retry MUST have a fresh unique proof jti.
        assert len(set(jtis)) == 3
        # Idempotency key MUST stay identical across retries.
        assert set(idem_keys) == {"bill-stable-key-99"}
        # Exact transmitted body bytes (and amount) MUST never silently change.
        assert len(set(bodies)) == 1
        assert json.loads(bodies[0].decode("utf-8"))["amount_paise"] == 185000
        assert set(bshs) == {sha256_base64url(bodies[0])}

    async def test_token_refresh_respects_revoked_and_expired_authority(self) -> None:
        kp = generate_agent_keypair()
        clock = FrozenClock(datetime(2026, 11, 18, 15, 0, 0, tzinfo=UTC))
        mandate_status = "ACTIVE"
        token_seq = 0
        resource_calls = 0
        seen_resource_jtis: list[str] = []
        seen_resource_idem_keys: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal token_seq, resource_calls
            if request.url.path == "/v1/oauth/token":
                if mandate_status == "REVOKED":
                    return httpx.Response(
                        403,
                        json={
                            "error": {
                                "code": ReasonCode.E_MANDATE_REVOKED.value,
                                "message": "Mandate revoked.",
                                "request_id": "req_rev",
                            }
                        },
                    )
                if mandate_status == "EXPIRED":
                    return httpx.Response(
                        401,
                        json={
                            "error": {
                                "code": ReasonCode.E_AUTH_EXPIRED.value,
                                "message": "Mandate expired.",
                                "request_id": "req_m_exp",
                            }
                        },
                    )
                token_seq += 1
                return httpx.Response(
                    200,
                    json={
                        "access_token": f"tok.v{token_seq}",
                        "token_type": "DPoP",
                        "expires_in": 120,
                    },
                )

            if request.url.path == "/v1/payments/bills":
                resource_calls += 1
                _, claims, _ = _decode_jws_parts(request.headers["DPoP"])
                seen_resource_jtis.append(str(claims["jti"]))
                seen_resource_idem_keys.append(request.headers["Idempotency-Key"])

                # Simulate server rejecting first token as expired while mandate is active.
                if request.headers["Authorization"] == "DPoP tok.v1":
                    return httpx.Response(
                        401,
                        json={
                            "error": {
                                "code": ReasonCode.E_AUTH_EXPIRED.value,
                                "message": "Token expired.",
                                "request_id": "req_t_exp",
                            }
                        },
                    )
                if mandate_status == "REVOKED":
                    return httpx.Response(
                        403,
                        json={
                            "error": {
                                "code": ReasonCode.E_MANDATE_REVOKED.value,
                                "message": "Mandate revoked.",
                                "request_id": "req_r_rev",
                            }
                        },
                    )
                return httpx.Response(200, json={"status": "EXECUTED", "request_id": "req_2"})
            raise AssertionError(request.url.path)

        transport = httpx.MockTransport(handler)
        async with AgentGateClient(
            gateway_url=GATEWAY_URL,
            agent_id="agt_billpal",
            mandate_id="mnd_demo_1",
            keypair=kp,
            clock=clock,
            retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0.0),
            transport=transport,
            sleep_fn=_noop_sleep,
        ) as client:
            # 1. Active mandate: expired token triggers single refresh + fresh proof + same key.
            res = await client.pay_bill(
                from_account="ACC-001",
                payee_id="BILL-ELEC",
                amount_paise=100000,
                task="pay",
                category="utilities",
                idempotency_key="bill-refresh-1",
            )
            assert res["status"] == "EXECUTED"
            assert token_seq == 2
            assert len(set(seen_resource_jtis)) == 2
            assert set(seen_resource_idem_keys) == {"bill-refresh-1"}

            # 1b. Refresh on E_AUTH_EXPIRED must not consume the transport retry budget
            # (with max_attempts=1, a single token refresh still succeeds!).
            clock.advance(seconds=10)
            client.token_manager.invalidate()
            res_single_attempt = await client.request_signed(
                "POST",
                "/v1/payments/bills",
                json_body={
                    "from_account": "ACC-001",
                    "payee_id": "BILL-ELEC",
                    "amount_paise": 100000,
                    "currency": "INR",
                    "intent": {"task": "pay", "category": "utilities"},
                },
                idempotency_key="bill-refresh-1b",
                retry_policy=RetryPolicy(max_attempts=1, base_delay_seconds=0.0),
            )
            assert res_single_attempt.status_code == 200

            # 2. Mandate revoked: advance clock so token expires -> refresh MUST fail 403!
            mandate_status = "REVOKED"
            clock.advance(seconds=125)
            with pytest.raises(AgentAPIError) as rev_exc:
                await client.pay_bill(
                    from_account="ACC-001",
                    payee_id="BILL-ELEC",
                    amount_paise=100000,
                    task="pay",
                    category="utilities",
                    idempotency_key="bill-refresh-2",
                )
            assert rev_exc.value.status_code == 403
            assert rev_exc.value.error_code == ReasonCode.E_MANDATE_REVOKED.value
            assert rev_exc.value.is_authority_denied is True
            assert client.token_manager.cached_token is None

            # 3. Mandate expired: refresh MUST fail 401 E_AUTH_EXPIRED!
            mandate_status = "EXPIRED"
            with pytest.raises(AgentAPIError) as exp_exc:
                await client.get_balance("ACC-001")
            assert exp_exc.value.status_code == 401
            assert exp_exc.value.error_code == ReasonCode.E_AUTH_EXPIRED.value
            assert client.token_manager.cached_token is None

    @pytest.mark.parametrize(
        "sequence,expected_status,expected_attempts,expected_timed_out",
        [
            (["PENDING", "APPROVED", "EXECUTED"], "EXECUTED", 3, False),
            (["PENDING", "REJECTED"], "REJECTED", 2, False),
            (["PENDING", "EXPIRED"], "EXPIRED", 2, False),
            (["PENDING", "PENDING", "PENDING"], "PENDING", 3, True),
        ],
    )
    async def test_polling_handles_pending_completed_rejected_expired_and_timeout(
        self,
        sequence: list[str],
        expected_status: str,
        expected_attempts: int,
        expected_timed_out: bool,
    ) -> None:
        kp = generate_agent_keypair()
        clock = FrozenClock(datetime(2026, 11, 18, 15, 0, 0, tzinfo=UTC))
        idx = {"n": 0}
        poll_jtis: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/oauth/token":
                return httpx.Response(
                    200,
                    json={"access_token": "tok.poll.1", "token_type": "DPoP", "expires_in": 300},
                )
            if request.url.path == "/v1/escalations/esc_1":
                _, claims, _ = _decode_jws_parts(request.headers["DPoP"])
                poll_jtis.append(str(claims["jti"]))
                st = sequence[min(idx["n"], len(sequence) - 1)]
                idx["n"] += 1
                return httpx.Response(200, json={"escalation_id": "esc_1", "status": st})
            raise AssertionError(request.url.path)

        transport = httpx.MockTransport(handler)
        async with AgentGateClient(
            gateway_url=GATEWAY_URL,
            agent_id="agt_billpal",
            mandate_id="mnd_demo_1",
            keypair=kp,
            clock=clock,
            transport=transport,
            sleep_fn=_noop_sleep,
        ) as client:
            result = await client.poll_escalation(
                "esc_1", interval_seconds=0.0, max_attempts=3
            )
            assert result.status == expected_status
            assert result.attempts == expected_attempts
            assert result.timed_out is expected_timed_out
            assert len(set(poll_jtis)) == expected_attempts


class TestSDKCLIAndRealGatewayInterop:
    def test_cli_keygen_inspect_balance_and_pay_bill_with_poll(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        key_file = tmp_path / "cli_agent.pem"
        pub_file = tmp_path / "cli_agent_pub.json"

        # 1. keygen
        rc = cli_main(["keygen", "--out", str(key_file), "--pub-out", str(pub_file)])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert "jkt" in out
        assert "d" not in out["public_jwk"]
        assert "PRIVATE KEY" not in json.dumps(out)

        # 2. inspect-key
        rc_inspect = cli_main(["inspect-key", "--key", str(key_file)])
        assert rc_inspect == 0
        inspected = json.loads(capsys.readouterr().out)
        assert inspected["jkt"] == out["jkt"]

        # 3. balance & pay-bill --poll via MockTransport
        esc_calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/oauth/token":
                return httpx.Response(
                    200,
                    json={"access_token": "tok.cli.1", "token_type": "DPoP", "expires_in": 300},
                )
            if request.url.path == "/v1/accounts/ACC-001/balance":
                return httpx.Response(
                    200, json={"account_id": "ACC-001", "balance_paise": 12500000}
                )
            if request.url.path == "/v1/payments/bills":
                return httpx.Response(
                    202,
                    json={
                        "request_id": "req_cli_1",
                        "status": "PENDING_APPROVAL",
                        "escalation_id": "esc_cli_1",
                    },
                )
            if request.url.path == "/v1/escalations/esc_cli_1":
                esc_calls["n"] += 1
                st = "PENDING" if esc_calls["n"] == 1 else "EXECUTED"
                return httpx.Response(200, json={"escalation_id": "esc_cli_1", "status": st})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        rc_bal = cli_main(
            [
                "balance",
                "--gateway-url",
                GATEWAY_URL,
                "--agent-id",
                "agt_billpal",
                "--mandate-id",
                "mnd_demo_1",
                "--key",
                str(key_file),
                "--account-id",
                "ACC-001",
            ],
            transport=transport,
        )
        assert rc_bal == 0
        bal_out = json.loads(capsys.readouterr().out)
        assert bal_out["balance_paise"] == 12500000

        rc_pay = cli_main(
            [
                "pay-bill",
                "--gateway-url",
                GATEWAY_URL,
                "--agent-id",
                "agt_billpal",
                "--mandate-id",
                "mnd_demo_1",
                "--key",
                str(key_file),
                "--from-account",
                "ACC-001",
                "--payee-id",
                "BILL-ELEC",
                "--amount-paise",
                "480000",
                "--task",
                "pay_electricity_bill",
                "--category",
                "utilities.electricity",
                "--poll",
                "--poll-interval",
                "0",
            ],
            transport=transport,
        )
        assert rc_pay == 0
        pay_out = json.loads(capsys.readouterr().out)
        assert pay_out["poll"]["status"] == "EXECUTED"
        assert pay_out["poll"]["attempts"] == 2

    async def test_sdk_interoperates_with_real_b02_b03_b04_gateway_verifier(
        self, tmp_path: Path
    ) -> None:
        db_file = tmp_path / "sdk_interop.db"
        engine = create_sqlite_identity_engine(f"sqlite:///{db_file.as_posix()}")
        create_identity_schema(engine)
        session_factory = create_identity_session_factory(engine)
        clock = FrozenClock(datetime(2026, 11, 18, 15, 0, 0, tzinfo=UTC))
        audit_log = MemoryAuditLog()
        registry = SqlAgentRegistry(session_factory, clock, audit_log=audit_log)
        replay_cache = SqlProofReplayCache(session_factory, clock)
        proof_verifier = DPoPProofVerifier(
            registry=registry,
            replay_cache=replay_cache,
            clock=clock,
            trusted_origin=GATEWAY_URL,
            audit_log=audit_log,
        )

        sdk_kp = generate_agent_keypair()
        await registry.register(
            name="BillPal",
            vendor="FinBot Labs",
            public_jwk=sdk_kp.public_jwk(),
            max_scopes=[ActionType.BILL_PAY.value, ActionType.BALANCE_READ.value],
            agent_id="agt_billpal",
        )

        now = clock.now()
        terms = MandateTerms(
            mandate_id="mnd_sdk_1",
            version=1,
            customer_id="cust_001",
            agent_id="agt_billpal",
            valid_from=now - timedelta(minutes=1),
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
                ),
            ),
            step_up=StepUpRules(),
        )
        m_hash = mandate_hash(terms)
        m_sig = sign_prefixed_sha256(generate_agent_keypair().private_key, m_hash)
        mandate_reader = FixtureMandateReader(
            mandates={
                "mnd_sdk_1": MandateSnapshot(
                    terms=terms,
                    status=MandateStatus.ACTIVE,
                    mandate_hash=m_hash,
                    sig=m_sig,
                    key_id="m-k1",
                )
            }
        )

        token_key = generate_agent_keypair().private_key
        token_service = SqlTokenService(
            session_factory=session_factory,
            clock=clock,
            signing_key=token_key,
            proof_verifier=proof_verifier,
            mandate_reader=mandate_reader,
            audit_log=audit_log,
        )
        identity_verifier = SqlIdentityVerifier(
            session_factory=session_factory,
            clock=clock,
            token_public_key=token_key.public_key(),
            proof_verifier=proof_verifier,
            mandate_reader=mandate_reader,
            audit_log=audit_log,
        )
        human_auth = SqlHumanAuthService(
            session_factory, clock, HumanSessionKeyring.generate(), audit_log=audit_log
        )

        app = FastAPI()
        install_identity_error_handlers(app)
        app.include_router(
            build_identity_router(
                registry=registry, human_auth=human_auth, token_service=token_service
            )
        )

        @app.post("/v1/payments/bills")
        async def _verified_bill_endpoint(request: Request) -> dict[str, Any]:
            raw = await request.body()
            auth_ctx = await identity_verifier.authenticate(
                raw, dict(request.headers), request.method, str(request.url)
            )
            return {
                "status": "EXECUTED",
                "customer_id": auth_ctx.customer_id,
                "agent_id": auth_ctx.agent_id,
                "mandate_id": auth_ctx.mandate_id,
                "jkt": auth_ctx.jkt,
            }

        try:
            transport = httpx.ASGITransport(app=app)
            async with AgentGateClient(
                gateway_url=GATEWAY_URL,
                agent_id="agt_billpal",
                mandate_id="mnd_sdk_1",
                keypair=sdk_kp,
                clock=clock,
                transport=transport,
                sleep_fn=_noop_sleep,
            ) as client:
                res = await client.pay_bill(
                    from_account="ACC-001",
                    payee_id="BILL-ELEC",
                    amount_paise=185000,
                    task="pay_electricity_bill",
                    category="utilities.electricity",
                    idempotency_key="bill-interop-1",
                )
                assert res["status"] == "EXECUTED"
                assert res["customer_id"] == "cust_001"
                assert res["agent_id"] == "agt_billpal"
                assert res["mandate_id"] == "mnd_sdk_1"
                assert res["jkt"] == sdk_kp.jkt
        finally:
            engine.dispose()
