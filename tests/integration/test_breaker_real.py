"""Integration test for circuit breaker with real Member A and B modules."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_sdk import build_dpop_proof, generate_agent_keypair
from contracts.common import ActionType
from contracts.core_models import (
    Grant,
    MandateSnapshot,
    MandateStatus,
    MandateTerms,
    StepUpRules,
)
from contracts.identity_models import AgentStatus
from contracts.reason_codes import ReasonCode
from gateway.audit.schema import create_audit_schema
from gateway.events.bus import EventBus
from gateway.identity.crypto import (
    generate_ed25519_keypair,
    mandate_hash,
    sign_prefixed_sha256,
)
from gateway.identity.errors import AuthenticationError, AuthorizationError
from gateway.identity.human_auth import HumanSessionKeyring
from gateway.identity.schema import (
    create_identity_schema,
    create_identity_session_factory,
    create_sqlite_identity_engine,
)
from gateway.identity.wiring import build_real_member_b_providers
from gateway.platform.db import Base, SqliteUnitOfWork, create_db_engine, create_session_factory
from gateway.platform.lifecycle import LifecycleCoordinator
from tests.support.b.providers import FixtureMandateReader
from tests.support.platform import FrozenClock

pytestmark = pytest.mark.integration


def _make_mandate(
    mandate_id: str = "mnd_breaker_1",
    customer_id: str = "cust_001",
    agent_id: str = "agt_breaker_test",
) -> MandateSnapshot:
    terms = MandateTerms(
        mandate_id=mandate_id,
        version=1,
        customer_id=customer_id,
        agent_id=agent_id,
        valid_from=datetime(2026, 11, 1, 0, 0, tzinfo=UTC),
        valid_until=datetime(2026, 12, 1, 0, 0, tzinfo=UTC),
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


@pytest.mark.asyncio
async def test_circuit_breaker_real_integration(tmp_path: Path) -> None:
    """Verifies end-to-end circuit breaker containment with real A/B providers.
    
    1. Agent registers and issues an access token under real SQLite schema.
    2. Token successfully authenticates against real SqlIdentityVerifier.
    3. Circuit breaker trips and invokes LifecycleCoordinator.suspend_agent(...).
    4. LifecycleCoordinator executes atomic suspension, token revocation, and audit logging.
    5. Agent status becomes SUSPENDED; token is revoked in database.
    6. Subsequent token issuance and authentication are rejected.
    7. Hash-chained audit log records the suspension event and passes cryptographic verification.
    """
    db_file = tmp_path / "breaker_gateway.db"
    db_url = f"sqlite:///{db_file.as_posix()}"
    async_db_url = f"sqlite+aiosqlite:///{db_file.as_posix()}"

    # 1. Initialize real schema
    sync_engine = create_sqlite_identity_engine(db_url)
    create_identity_schema(sync_engine)
    create_audit_schema(sync_engine)
    sf = create_identity_session_factory(sync_engine)

    async_engine = create_db_engine(async_db_url)
    async_sf = create_session_factory(async_engine)
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    clock = FrozenClock(datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC))
    token_key, _ = generate_ed25519_keypair()
    audit_key, _ = generate_ed25519_keypair()
    human_keyring = HumanSessionKeyring.generate()

    mandate = _make_mandate()
    mandate_reader = FixtureMandateReader(mandates={mandate.terms.mandate_id: mandate})

    providers = build_real_member_b_providers(
        session_factory=sf,
        clock=clock,
        mandate_reader=mandate_reader,
        trusted_origin="http://127.0.0.1:8000",
        token_signing_key=token_key,
        audit_signing_key=audit_key,
        human_keyring=human_keyring,
    )

    uow = SqliteUnitOfWork(async_sf)
    event_bus = EventBus()
    lifecycle = LifecycleCoordinator(
        unit_of_work=uow,
        audit_log=providers.audit_log,
        clock=clock,
        event_bus=event_bus,
        token_service=providers.token_service,
        agent_registry=providers.registry,
    )

    # 2. Register agent
    agent_kp = generate_agent_keypair()
    agent_id = "agt_breaker_test"
    agent_identity = await providers.registry.register(
        agent_id=agent_id,
        name="Breaker Test Agent",
        vendor="Acme Security",
        public_jwk=agent_kp.public_jwk(),
        max_scopes=["bill_pay", "balance_read"],
    )
    assert agent_identity.status == AgentStatus.ACTIVE

    # 3. Issue token and verify authentication works initially
    token_proof = build_dpop_proof(
        keypair=agent_kp,
        method="POST",
        gateway_origin="http://127.0.0.1:8000",
        path_or_url="/v1/oauth/token",
        body_bytes=b"",
        now=clock.now(),
    ).jwt

    token_res = await providers.token_service.issue_token(
        client_id=agent_id,
        mandate_id=mandate.terms.mandate_id,
        proof_jwt=token_proof,
        method="POST",
        request_url="http://127.0.0.1:8000/v1/oauth/token",
        request_bytes=b"",
    )
    assert token_res.access_token

    req_body = b'{"test": 1}'
    req_proof = build_dpop_proof(
        keypair=agent_kp,
        method="POST",
        gateway_origin="http://127.0.0.1:8000",
        path_or_url="/v1/payments/bills",
        body_bytes=req_body,
        access_token=token_res.access_token,
        now=clock.now(),
    ).jwt

    # Authenticate initial request
    auth_ctx = await providers.identity_verifier.authenticate(
        request_bytes=req_body,
        headers={
            "Authorization": f"DPoP {token_res.access_token}",
            "DPoP": req_proof,
        },
        method="POST",
        url="http://127.0.0.1:8000/v1/payments/bills",
    )
    assert auth_ctx.agent_id == agent_id
    assert auth_ctx.mandate_id == mandate.terms.mandate_id

    # 4. Trip circuit breaker: Lifecycle coordinator suspends agent
    await lifecycle.suspend_agent(
        agent_id=agent_id,
        reason_codes=(ReasonCode.E_RISK_BREAKER_OPEN.value,),
        actor="circuit_breaker",
        reason="Tripped by 5 consecutive scope denials",
    )

    # 5. Verify agent is suspended in registry
    reloaded_agent = await providers.registry.get(agent_id)
    assert reloaded_agent.status == AgentStatus.SUSPENDED

    # 6. Verify existing token is revoked and fails authentication
    req2_body = b'{"test": 2}'
    req2_proof = build_dpop_proof(
        keypair=agent_kp,
        method="POST",
        gateway_origin="http://127.0.0.1:8000",
        path_or_url="/v1/payments/bills",
        body_bytes=req2_body,
        access_token=token_res.access_token,
        now=clock.now(),
    ).jwt

    with pytest.raises((AuthenticationError, AuthorizationError)) as exc_info:
        await providers.identity_verifier.authenticate(
            request_bytes=req2_body,
            headers={
                "Authorization": f"DPoP {token_res.access_token}",
                "DPoP": req2_proof,
            },
            method="POST",
            url="http://127.0.0.1:8000/v1/payments/bills",
        )
    assert exc_info.value.code in (
        ReasonCode.E_AUTH_TOKEN_REVOKED,
        ReasonCode.E_AUTH_AGENT_SUSPENDED,
    )

    # 7. Verify new token issuance is rejected for suspended agent
    new_token_proof = build_dpop_proof(
        keypair=agent_kp,
        method="POST",
        gateway_origin="http://127.0.0.1:8000",
        path_or_url="/v1/oauth/token",
        body_bytes=b"",
        now=clock.now(),
    ).jwt

    with pytest.raises((AuthenticationError, AuthorizationError)) as exc_info2:
        await providers.token_service.issue_token(
            client_id=agent_id,
            mandate_id=mandate.terms.mandate_id,
            proof_jwt=new_token_proof,
            method="POST",
            request_url="http://127.0.0.1:8000/v1/oauth/token",
            request_bytes=b"",
        )
    assert exc_info2.value.code == ReasonCode.E_AUTH_AGENT_SUSPENDED

    # 8. Verify audit chain records suspension and remains mathematically sound
    verify_report = await providers.audit_log.verify()
    assert verify_report.valid is True
    assert verify_report.entries_checked >= 2  # registration + suspension

    entries_page = await providers.audit_log.list_entries(agent_id=agent_id)
    event_types = [
        e["event_type"] if isinstance(e, dict) else e.event_type for e in entries_page.items
    ]
    assert "agent.suspended" in event_types

    await async_engine.dispose()
    sync_engine.dispose()
