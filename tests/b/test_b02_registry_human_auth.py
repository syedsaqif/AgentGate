"""B02 unit and security tests for Agent Registry and Human Authentication."""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import jwt
import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from contracts.common import ActionType, HumanRole
from contracts.identity_models import AgentStatus, HumanContext
from contracts.reason_codes import ReasonCode
from gateway.identity.crypto import (
    generate_ed25519_keypair,
    jwk_thumbprint,
    public_key_to_jwk,
)
from gateway.identity.errors import (
    AuthenticationError,
    DuplicateAgentKeyError,
    PinVerificationError,
    RateLimitedError,
    ResourceNotFoundError,
    ValidationDomainError,
)
from gateway.identity.human_auth import (
    HUMAN_SESSION_ISSUER,
    HUMAN_SESSION_TYP,
    AuthThrottler,
    HumanSessionKeyring,
    SqlHumanAuthService,
    assert_customer_owns,
)
from gateway.identity.models import SqlAlchemyTx
from gateway.identity.registry import SqlAgentRegistry
from gateway.identity.routes import build_identity_router, install_identity_error_handlers
from gateway.identity.schema import (
    create_identity_schema,
    create_identity_session_factory,
    create_sqlite_identity_engine,
)
from tests.support.b.providers import (
    FixtureAgentRegistry,
    FixtureHumanAuth,
    MemoryAuditLog,
)
from tests.support.platform import FrozenClock

DEMO_PIN = "4821"
OTHER_CUST_PIN = "9134"
OPS_PASSWORD = "ops-test-password-1"
REGISTRAR_PASSWORD = "reg-test-password-1"

type DbEnv = tuple[
    Engine,
    sessionmaker[Session],
    FrozenClock,
    MemoryAuditLog,
    SqlAgentRegistry,
    SqlHumanAuthService,
    HumanSessionKeyring,
]


class _RecordingLifecycle:
    """Test-only Lifecycle double that invokes SqlAgentRegistry inside a real SqlAlchemyTx."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        registry: SqlAgentRegistry,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []

    async def revoke_mandate(self, mandate_id: str) -> None:
        self.calls.append(("revoke_mandate", mandate_id, ()))

    async def suspend_agent(self, agent_id: str, reason_codes: tuple[str, ...]) -> None:
        self.calls.append(("suspend_agent", agent_id, reason_codes))
        with self._session_factory() as session:
            with session.begin():
                tx = SqlAlchemyTx(id=f"tx_{secrets.token_hex(4)}", session=session)
                await self._registry.set_status(agent_id, AgentStatus.SUSPENDED, tx)

    async def suspend_customer_delegation(
        self, customer_id: str, agent_id: str, reason_codes: tuple[str, ...]
    ) -> None:
        self.calls.append(
            ("suspend_customer_delegation", f"{customer_id}:{agent_id}", reason_codes)
        )

    async def reinstate_agent(self, agent_id: str) -> None:
        self.calls.append(("reinstate_agent", agent_id, ()))
        with self._session_factory() as session:
            with session.begin():
                tx = SqlAlchemyTx(id=f"tx_{secrets.token_hex(4)}", session=session)
                await self._registry.set_status(agent_id, AgentStatus.ACTIVE, tx)


@pytest.fixture
async def db_env(tmp_path: Path) -> AsyncIterator[DbEnv]:
    db_file = tmp_path / "identity_b02.db"
    engine = create_sqlite_identity_engine(f"sqlite:///{db_file.as_posix()}")
    create_identity_schema(engine)
    session_factory = create_identity_session_factory(engine)
    clock = FrozenClock(datetime(2026, 11, 18, 10, 0, 0, tzinfo=UTC))
    audit_log = MemoryAuditLog()
    keyring = HumanSessionKeyring.generate()
    throttler = AuthThrottler(clock, max_failures=3, window_seconds=300, lockout_seconds=120)
    registry = SqlAgentRegistry(session_factory, clock, audit_log=audit_log)
    human_auth = SqlHumanAuthService(
        session_factory,
        clock,
        keyring,
        audit_log=audit_log,
        throttler=throttler,
        customer_session_ttl_seconds=600,
        ops_session_ttl_seconds=300,
        registrar_session_ttl_seconds=180,
    )
    await human_auth.seed_customer(
        customer_id="cust_001", display_name="Demo Customer", pin=DEMO_PIN
    )
    await human_auth.seed_customer(
        customer_id="cust_002", display_name="Isolated Customer", pin=OTHER_CUST_PIN
    )
    await human_auth.seed_operator(
        operator_id="ops_001",
        display_name="Ops Reviewer",
        role=HumanRole.OPS,
        password=OPS_PASSWORD,
    )
    await human_auth.seed_operator(
        operator_id="reg_001",
        display_name="Agent Registrar",
        role=HumanRole.REGISTRAR,
        password=REGISTRAR_PASSWORD,
    )
    try:
        yield engine, session_factory, clock, audit_log, registry, human_auth, keyring
    finally:
        engine.dispose()


class TestAgentRegistry:
    async def test_sqlite_wal_mode_enabled(self, db_env: DbEnv) -> None:
        _, session_factory, _, _, _, _, _ = db_env
        with session_factory() as session:
            mode = str(session.execute(text("PRAGMA journal_mode")).scalar_one()).lower()
            fk = int(session.execute(text("PRAGMA foreign_keys")).scalar_one())
        assert mode == "wal"
        assert fk == 1

    async def test_register_lookup_list_and_audit(self, db_env: DbEnv) -> None:
        _, _, _, audit_log, registry, _, _ = db_env
        _, pub1 = generate_ed25519_keypair()
        jwk1 = public_key_to_jwk(pub1, kid="billpal-k1")

        agent = await registry.register(
            name="BillPal",
            vendor="FinBot Labs",
            public_jwk=jwk1,
            max_scopes=[ActionType.BILL_PAY.value, ActionType.BALANCE_READ.value],
            agent_id="agt_billpal",
            request_id="req_reg_1",
        )
        assert agent.agent_id == "agt_billpal"
        assert agent.status == AgentStatus.ACTIVE
        assert agent.jkt == jwk_thumbprint(jwk1)
        assert agent.max_scopes == ("bill_pay", "balance_read")
        assert "d" not in agent.public_jwk

        fetched = await registry.get("agt_billpal")
        assert fetched == agent
        by_jkt = await registry.get_by_jkt(agent.jkt)
        assert by_jkt == agent

        assert len(audit_log.events) == 1
        evt = audit_log.events[0]
        assert evt.event_type == "agent.registered"
        assert evt.agent_id == "agt_billpal"
        assert evt.safe_payload["jkt"] == agent.jkt

    async def test_duplicate_public_key_thumbprint_rejected_even_with_different_kid(
        self, db_env: DbEnv
    ) -> None:
        _, _, _, _, registry, _, _ = db_env
        _, pub = generate_ed25519_keypair()
        await registry.register(
            name="Agent One",
            vendor="Vendor A",
            public_jwk=public_key_to_jwk(pub, kid="k1"),
            max_scopes=["bill_pay"],
            agent_id="agt_one",
        )
        with pytest.raises(DuplicateAgentKeyError) as exc_info:
            await registry.register(
                name="Agent Two",
                vendor="Vendor B",
                public_jwk=public_key_to_jwk(pub, kid="k2"),
                max_scopes=["bill_pay"],
                agent_id="agt_two",
            )
        assert exc_info.value.code is ReasonCode.E_REQ_INVALID
        assert exc_info.value.http_status == 400

    async def test_register_rejects_private_jwk_bad_scopes_and_rolls_back_on_audit_failure(
        self, db_env: DbEnv
    ) -> None:
        _, _, _, audit_log, registry, _, _ = db_env
        _, pub = generate_ed25519_keypair()
        jwk = public_key_to_jwk(pub)

        with pytest.raises(ValidationDomainError):
            await registry.register(
                name="BadKeyAgent",
                vendor="Vendor",
                public_jwk={**jwk, "d": "nWGxne_9WmC6hEr0kuwsxERJxWl7MmkZcDusAxyuf2A"},
                max_scopes=["bill_pay"],
            )

        with pytest.raises(ValidationDomainError):
            await registry.register(
                name="BadScopeAgent",
                vendor="Vendor",
                public_jwk=jwk,
                max_scopes=["admin_root"],
            )

        audit_log.fail_append = True
        with pytest.raises(RuntimeError, match="injected audit failure"):
            await registry.register(
                name="RollbackAgent",
                vendor="Vendor",
                public_jwk=jwk,
                max_scopes=["bill_pay"],
                agent_id="agt_rollback",
            )
        audit_log.fail_append = False
        with pytest.raises(ResourceNotFoundError):
            await registry.get("agt_rollback")

    async def test_set_status_respects_caller_owned_tx_rollback_and_commit(
        self, db_env: DbEnv
    ) -> None:
        _, session_factory, _, _, registry, _, _ = db_env
        _, pub = generate_ed25519_keypair()
        await registry.register(
            name="TxAgent",
            vendor="Vendor",
            public_jwk=public_key_to_jwk(pub),
            max_scopes=["bill_pay"],
            agent_id="agt_tx",
        )

        # Rollback caller tx -> status must NOT persist (proves set_status did not commit tx).
        with session_factory() as session:
            with session.begin():
                tx = SqlAlchemyTx(id="tx_rollback", session=session)
                await registry.set_status("agt_tx", AgentStatus.SUSPENDED, tx)
                session.rollback()

        assert (await registry.get("agt_tx")).status == AgentStatus.ACTIVE

        # Commit caller tx -> status persists.
        with session_factory() as session:
            with session.begin():
                tx = SqlAlchemyTx(id="tx_commit", session=session)
                await registry.set_status("agt_tx", AgentStatus.SUSPENDED, tx)

        assert (await registry.get("agt_tx")).status == AgentStatus.SUSPENDED

    async def test_cursor_pagination(self, db_env: DbEnv) -> None:
        _, _, _, _, registry, _, _ = db_env
        for idx in range(3):
            _, pub = generate_ed25519_keypair()
            await registry.register(
                name=f"Agent {idx}",
                vendor="Vendor",
                public_jwk=public_key_to_jwk(pub),
                max_scopes=["balance_read"],
                agent_id=f"agt_{idx:02d}",
            )
        p1 = await registry.list_public_page(limit=2)
        assert len(p1.items) == 2
        assert p1.next_cursor is not None
        p2 = await registry.list_public_page(cursor=p1.next_cursor, limit=2)
        assert len(p2.items) == 1
        assert p2.next_cursor is None
        assert [a.agent_id for a in p1.items + p2.items] == ["agt_00", "agt_01", "agt_02"]


class TestHumanAuthSecurity:
    async def test_keyring_rejects_reused_signing_keys(self) -> None:
        k1, _ = generate_ed25519_keypair()
        k2, _ = generate_ed25519_keypair()
        with pytest.raises(ValueError, match="distinct Ed25519"):
            HumanSessionKeyring(customer_key=k1, ops_key=k1, registrar_key=k2)

    async def test_argon2id_hashes_stored_and_no_plaintext_secrets_in_db_or_audit(
        self, db_env: DbEnv
    ) -> None:
        _, session_factory, _, audit_log, _, human_auth, _ = db_env
        cust_session = await human_auth.login_customer(customer_id="cust_001", pin=DEMO_PIN)
        ops_session = await human_auth.login_ops(operator_id="ops_001", password=OPS_PASSWORD)

        with session_factory() as session:
            pin_hash: str = str(
                session.execute(
                    text("SELECT pin_hash FROM customers WHERE customer_id = 'cust_001'")
                ).scalar_one()
            )
            op_hash: str = str(
                session.execute(
                    text("SELECT secret_hash FROM operators WHERE operator_id = 'ops_001'")
                ).scalar_one()
            )
            raw_hashes: list[object] = list(
                session.execute(text("SELECT token_hash FROM human_sessions")).scalars().all()
            )
            token_hashes: list[str] = [str(val) for val in raw_hashes]

        assert pin_hash.startswith("$argon2id$")
        assert DEMO_PIN not in pin_hash
        assert op_hash.startswith("$argon2id$")
        assert OPS_PASSWORD not in op_hash
        assert cust_session.session_token not in token_hashes
        assert ops_session.session_token not in token_hashes

        serialized_audit = json.dumps([e.model_dump(mode="json") for e in audit_log.events])
        for secret in (
            DEMO_PIN,
            OPS_PASSWORD,
            cust_session.session_token,
            ops_session.session_token,
        ):
            assert secret not in serialized_audit

    async def test_strict_role_audience_and_algorithm_isolation(self, db_env: DbEnv) -> None:
        _, _, clock, _, _, human_auth, keyring = db_env
        cust = await human_auth.login_customer(customer_id="cust_001", pin=DEMO_PIN)
        ops = await human_auth.login_ops(operator_id="ops_001", password=OPS_PASSWORD)
        reg = await human_auth.login_ops(operator_id="reg_001", password=REGISTRAR_PASSWORD)

        c_ctx = await human_auth.authenticate(cust.session_token, HumanRole.CUSTOMER)
        o_ctx = await human_auth.authenticate(ops.session_token, HumanRole.OPS)
        r_ctx = await human_auth.authenticate(reg.session_token, HumanRole.REGISTRAR)
        assert c_ctx.subject_id == "cust_001" and c_ctx.role == HumanRole.CUSTOMER
        assert o_ctx.subject_id == "ops_001" and o_ctx.role == HumanRole.OPS
        assert r_ctx.subject_id == "reg_001" and r_ctx.role == HumanRole.REGISTRAR

        cross_pairs = [
            (cust.session_token, HumanRole.OPS),
            (cust.session_token, HumanRole.REGISTRAR),
            (ops.session_token, HumanRole.CUSTOMER),
            (ops.session_token, HumanRole.REGISTRAR),
            (reg.session_token, HumanRole.CUSTOMER),
            (reg.session_token, HumanRole.OPS),
        ]
        for token, wrong_role in cross_pairs:
            with pytest.raises(AuthenticationError) as exc_info:
                await human_auth.authenticate(token, wrong_role)
            assert exc_info.value.code is ReasonCode.E_AUTH_INVALID_TOKEN
            assert exc_info.value.http_status == 401

        now = clock.now()
        agent_like_token = jwt.encode(
            {
                "iss": HUMAN_SESSION_ISSUER,
                "aud": "agentgate-agent-api",
                "sub": "cust_001",
                "jti": cust.session_id,
                "role": "customer",
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(seconds=300)).timestamp()),
            },
            keyring.customer_key,
            algorithm="EdDSA",
            headers={"typ": HUMAN_SESSION_TYP},
        )
        with pytest.raises(AuthenticationError) as exc_info:
            await human_auth.authenticate(agent_like_token, HumanRole.CUSTOMER)
        assert exc_info.value.code is ReasonCode.E_AUTH_INVALID_TOKEN

        hs256_forged = jwt.encode(
            {"iss": HUMAN_SESSION_ISSUER, "aud": "agentgate-customer-session", "sub": "cust_001"},
            "weak-hmac-secret-for-test-only-32b",
            algorithm="HS256",
            headers={"typ": HUMAN_SESSION_TYP},
        )
        with pytest.raises(AuthenticationError):
            await human_auth.authenticate(hs256_forged, HumanRole.CUSTOMER)

        with pytest.raises(AuthenticationError):
            await human_auth.login_ops(operator_id="cust_001", password=DEMO_PIN)
        with pytest.raises(AuthenticationError):
            await human_auth.login_customer(customer_id="ops_001", pin=OPS_PASSWORD)

    async def test_session_expiry_revocation_and_idor_ownership(self, db_env: DbEnv) -> None:
        _, _, clock, _, _, human_auth, _ = db_env
        s1 = await human_auth.login_customer(customer_id="cust_001", pin=DEMO_PIN)
        s2 = await human_auth.login_customer(customer_id="cust_002", pin=OTHER_CUST_PIN)

        ctx1 = await human_auth.authenticate(s1.session_token, HumanRole.CUSTOMER)
        assert_customer_owns(ctx1, "cust_001")
        with pytest.raises(ResourceNotFoundError) as idor_exc:
            assert_customer_owns(ctx1, "cust_002")
        assert idor_exc.value.http_status == 404

        await human_auth.revoke_session(s1.session_id)
        with pytest.raises(AuthenticationError) as rev_exc:
            await human_auth.authenticate(s1.session_token, HumanRole.CUSTOMER)
        assert rev_exc.value.code is ReasonCode.E_AUTH_TOKEN_REVOKED

        clock.advance(seconds=601)
        with pytest.raises(AuthenticationError) as exp_exc:
            await human_auth.authenticate(s2.session_token, HumanRole.CUSTOMER)
        assert exp_exc.value.code is ReasonCode.E_AUTH_EXPIRED

    async def test_pin_verification_and_login_throttling(self, db_env: DbEnv) -> None:
        _, _, clock, audit_log, _, human_auth, _ = db_env

        await human_auth.verify_pin("cust_001", DEMO_PIN)

        for _ in range(3):
            with pytest.raises(PinVerificationError) as pin_exc:
                await human_auth.verify_pin("cust_001", "0000")
            assert pin_exc.value.code is ReasonCode.E_ESC_BAD_PIN
            assert pin_exc.value.http_status == 403

        with pytest.raises(RateLimitedError) as rl_exc:
            await human_auth.verify_pin("cust_001", DEMO_PIN)
        assert rl_exc.value.code is ReasonCode.E_REQ_RATE_LIMITED
        assert rl_exc.value.http_status == 429
        assert rl_exc.value.retry_after_seconds is not None and rl_exc.value.retry_after_seconds > 0

        clock.advance(seconds=121)
        await human_auth.verify_pin("cust_001", DEMO_PIN)

        for _ in range(3):
            with pytest.raises(AuthenticationError):
                await human_auth.login_customer(customer_id="cust_002", pin="9999")
        with pytest.raises(RateLimitedError):
            await human_auth.login_customer(customer_id="cust_002", pin=OTHER_CUST_PIN)
        clock.advance(seconds=121)
        session = await human_auth.login_customer(customer_id="cust_002", pin=OTHER_CUST_PIN)
        assert session.subject_id == "cust_002"

        serialized = json.dumps([e.model_dump(mode="json") for e in audit_log.events])
        assert "0000" not in serialized
        assert "9999" not in serialized
        assert DEMO_PIN not in serialized


class TestIdentityRoutesHTTP:
    async def test_end_to_end_b02_routes_and_role_guards(self, db_env: DbEnv) -> None:
        _, session_factory, _, _, registry, human_auth, _ = db_env
        lifecycle = _RecordingLifecycle(session_factory, registry)

        app = FastAPI()
        install_identity_error_handlers(app)
        app.include_router(
            build_identity_router(registry=registry, human_auth=human_auth, lifecycle=lifecycle)
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8000"
        ) as client:
            bad_req = await client.post(
                "/v1/customer/login",
                json={"customer_id": "cust_001", "pin": DEMO_PIN, "unexpected": True},
            )
            assert bad_req.status_code == 400
            assert bad_req.json()["error"]["code"] == ReasonCode.E_REQ_INVALID.value
            assert "X-Request-ID" in bad_req.headers

            c_login = await client.post(
                "/v1/customer/login", json={"customer_id": "cust_001", "pin": DEMO_PIN}
            )
            assert c_login.status_code == 200
            c_token = c_login.json()["session_token"]

            o_login = await client.post(
                "/v1/ops/login", json={"operator_id": "ops_001", "password": OPS_PASSWORD}
            )
            assert o_login.status_code == 200
            o_token = o_login.json()["session_token"]

            r_login = await client.post(
                "/v1/ops/login", json={"operator_id": "reg_001", "password": REGISTRAR_PASSWORD}
            )
            assert r_login.status_code == 200
            r_token = r_login.json()["session_token"]

            _, agent_pub = generate_ed25519_keypair()
            reg_payload = {
                "name": "BillPal",
                "vendor": "FinBot Labs",
                "public_jwk": public_key_to_jwk(agent_pub),
                "max_scopes": ["bill_pay", "balance_read"],
            }

            for bad_headers in (
                {},
                {"Authorization": f"Bearer {c_token}"},
                {"Authorization": f"Bearer {o_token}"},
                {"Authorization": f"DPoP {r_token}"},
            ):
                resp = await client.post(
                    "/v1/agents/register", json=reg_payload, headers=bad_headers
                )
                assert resp.status_code == 401

            reg_resp = await client.post(
                "/v1/agents/register",
                json=reg_payload,
                headers={"Authorization": f"Bearer {r_token}"},
            )
            assert reg_resp.status_code == 201
            agent_id = reg_resp.json()["agent_id"]
            assert reg_resp.json()["status"] == "ACTIVE"

            dup_resp = await client.post(
                "/v1/agents/register",
                json={**reg_payload, "name": "Clone"},
                headers={"Authorization": f"Bearer {r_token}"},
            )
            assert dup_resp.status_code == 400
            assert dup_resp.json()["error"]["code"] == ReasonCode.E_REQ_INVALID.value

            assert (
                await client.get("/v1/ops/agents", headers={"Authorization": f"Bearer {c_token}"})
            ).status_code == 401
            assert (
                await client.get("/v1/ops/agents", headers={"Authorization": f"Bearer {r_token}"})
            ).status_code == 401
            list_resp = await client.get(
                "/v1/ops/agents", headers={"Authorization": f"Bearer {o_token}"}
            )
            assert list_resp.status_code == 200
            assert len(list_resp.json()["items"]) == 1

            susp_resp = await client.post(
                f"/v1/ops/agents/{agent_id}/suspend",
                json={"reason": "E_RISK_BREAKER_OPEN"},
                headers={"Authorization": f"Bearer {o_token}"},
            )
            assert susp_resp.status_code == 200
            assert susp_resp.json()["status"] == "SUSPENDED"

            rein_resp = await client.post(
                f"/v1/ops/agents/{agent_id}/reinstate",
                headers={"Authorization": f"Bearer {o_token}"},
            )
            assert rein_resp.status_code == 200
            assert rein_resp.json()["status"] == "ACTIVE"
            assert [c[0] for c in lifecycle.calls] == ["suspend_agent", "reinstate_agent"]

            not_found = await client.post(
                "/v1/ops/agents/agt_missing/suspend",
                headers={"Authorization": f"Bearer {o_token}"},
            )
            assert not_found.status_code == 404

            for _ in range(3):
                fail = await client.post(
                    "/v1/ops/login", json={"operator_id": "ops_001", "password": "wrong"}
                )
                assert fail.status_code == 401
            throttled = await client.post(
                "/v1/ops/login", json={"operator_id": "ops_001", "password": OPS_PASSWORD}
            )
            assert throttled.status_code == 429
            assert throttled.json()["error"]["code"] == ReasonCode.E_REQ_RATE_LIMITED.value
            assert int(throttled.headers["Retry-After"]) > 0


class TestMemberBSubstitutes:
    async def test_fixture_human_auth_and_registry_cover_positive_and_negative_cases(self) -> None:
        exp = datetime(2026, 11, 18, 11, 0, 0, tzinfo=UTC)
        fake_auth = FixtureHumanAuth(
            sessions={
                "tok_cust": HumanContext(
                    session_id="ses_1",
                    subject_id="cust_001",
                    role=HumanRole.CUSTOMER,
                    expires_at=exp,
                )
            },
            valid_pins={"cust_001": DEMO_PIN},
        )
        ctx = await fake_auth.authenticate("tok_cust", HumanRole.CUSTOMER)
        assert ctx.subject_id == "cust_001"
        with pytest.raises(AuthenticationError):
            await fake_auth.authenticate("tok_cust", HumanRole.OPS)
        await fake_auth.verify_pin("cust_001", DEMO_PIN)
        with pytest.raises(PinVerificationError):
            await fake_auth.verify_pin("cust_001", "0000")

        fake_registry = FixtureAgentRegistry()
        with pytest.raises(ResourceNotFoundError):
            await fake_registry.get("agt_unknown")
