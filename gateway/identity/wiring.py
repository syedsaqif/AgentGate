"""Member B real-provider assembly and real/scripted profile fake-rejection guard.

Designed for Member A's composition root (`gateway/deps.py` / `gateway/main.py`) to wire
all real Member B identity and audit providers into the single FastAPI gateway with one call.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI

from contracts.common import Clock
from contracts.core_ports import Lifecycle, MandateReader
from gateway.audit.append import DEFAULT_CHECKPOINT_INTERVAL, SqlAuditLog
from gateway.audit.checkpoints import CheckpointStore
from gateway.audit.routes import build_audit_router, install_audit_error_handlers

from .crypto import public_key_thumbprint
from .human_auth import AuthThrottler, HumanSessionKeyring, SqlHumanAuthService
from .models import SessionFactory
from .proofs import DEFAULT_PROOF_SKEW_SECONDS, DPoPProofVerifier
from .registry import SqlAgentRegistry
from .replay import SqlProofReplayCache
from .routes import build_identity_router, install_identity_error_handlers
from .tokens import MAX_TOKEN_TTL_SECONDS, SqlIdentityVerifier, SqlTokenService


@dataclass(frozen=True)
class MemberBProviders:
    """Complete bundle of real Member B identity and audit services."""

    registry: SqlAgentRegistry
    human_auth: SqlHumanAuthService
    replay_cache: SqlProofReplayCache
    proof_verifier: DPoPProofVerifier
    token_service: SqlTokenService
    identity_verifier: SqlIdentityVerifier
    audit_log: SqlAuditLog
    checkpoint_store: CheckpointStore | None


def assert_distinct_signing_keys(
    *,
    token_signing_key: Ed25519PrivateKey,
    audit_signing_key: Ed25519PrivateKey,
    human_keyring: HumanSessionKeyring,
) -> None:
    """Ensure token, audit, customer, ops, and registrar Ed25519 signing keys are all distinct."""

    thumbprints = {
        public_key_thumbprint(token_signing_key.public_key()),
        public_key_thumbprint(audit_signing_key.public_key()),
        public_key_thumbprint(human_keyring.customer_key.public_key()),
        public_key_thumbprint(human_keyring.ops_key.public_key()),
        public_key_thumbprint(human_keyring.registrar_key.public_key()),
    }
    if len(thumbprints) != 5:
        raise ValueError(
            "Member B requires five distinct Ed25519 signing keys "
            "(token, audit, customer, ops, registrar)"
        )


def build_real_member_b_providers(
    *,
    session_factory: SessionFactory,
    clock: Clock,
    mandate_reader: MandateReader,
    trusted_origin: str,
    token_signing_key: Ed25519PrivateKey,
    audit_signing_key: Ed25519PrivateKey,
    human_keyring: HumanSessionKeyring,
    checkpoint_path: str | Path | None = None,
    checkpoint_interval: int = DEFAULT_CHECKPOINT_INTERVAL,
    proof_skew_seconds: int = DEFAULT_PROOF_SKEW_SECONDS,
    token_ttl_seconds: int = MAX_TOKEN_TTL_SECONDS,
    throttler: AuthThrottler | None = None,
) -> MemberBProviders:
    """Construct all real Member B identity & audit providers sharing the gateway SQLite DB."""

    assert_distinct_signing_keys(
        token_signing_key=token_signing_key,
        audit_signing_key=audit_signing_key,
        human_keyring=human_keyring,
    )

    checkpoint_store = CheckpointStore(checkpoint_path) if checkpoint_path is not None else None
    audit_log = SqlAuditLog(
        session_factory=session_factory,
        signing_key=audit_signing_key,
        clock=clock,
        checkpoint_store=checkpoint_store,
        checkpoint_interval=checkpoint_interval,
    )
    registry = SqlAgentRegistry(session_factory, clock, audit_log=audit_log)
    human_auth = SqlHumanAuthService(
        session_factory,
        clock,
        human_keyring,
        audit_log=audit_log,
        throttler=throttler,
    )
    replay_cache = SqlProofReplayCache(session_factory, clock)
    proof_verifier = DPoPProofVerifier(
        registry=registry,
        replay_cache=replay_cache,
        clock=clock,
        trusted_origin=trusted_origin,
        audit_log=audit_log,
        clock_skew_seconds=proof_skew_seconds,
    )
    token_service = SqlTokenService(
        session_factory=session_factory,
        clock=clock,
        signing_key=token_signing_key,
        proof_verifier=proof_verifier,
        mandate_reader=mandate_reader,
        audit_log=audit_log,
        default_ttl_seconds=token_ttl_seconds,
    )
    identity_verifier = SqlIdentityVerifier(
        session_factory=session_factory,
        clock=clock,
        token_public_key=token_signing_key.public_key(),
        proof_verifier=proof_verifier,
        mandate_reader=mandate_reader,
        audit_log=audit_log,
    )
    return MemberBProviders(
        registry=registry,
        human_auth=human_auth,
        replay_cache=replay_cache,
        proof_verifier=proof_verifier,
        token_service=token_service,
        identity_verifier=identity_verifier,
        audit_log=audit_log,
        checkpoint_store=checkpoint_store,
    )


def assert_real_member_b_providers(
    *,
    profile: str,
    identity_verifier: object,
    human_auth: object,
    registry: object,
    token_service: object,
    audit_log: object,
) -> None:
    """Reject any fake or in-memory Member B provider when running in `real` or `scripted` mode."""

    if profile not in ("real", "scripted"):
        return

    checks = (
        ("identity_verifier", identity_verifier, SqlIdentityVerifier),
        ("human_auth", human_auth, SqlHumanAuthService),
        ("registry", registry, SqlAgentRegistry),
        ("token_service", token_service, SqlTokenService),
        ("audit_log", audit_log, SqlAuditLog),
    )
    for name, instance, expected_cls in checks:
        if not isinstance(instance, expected_cls):
            raise RuntimeError(
                f"Profile '{profile}' forbids fake Member B provider for '{name}': "
                f"expected {expected_cls.__name__}, got {type(instance).__name__}"
            )


def mount_member_b_routes(
    app: FastAPI,
    providers: MemberBProviders,
    *,
    lifecycle: Lifecycle | None = None,
) -> None:
    """Install Member B error handlers and mount all identity and audit HTTP routes."""

    install_identity_error_handlers(app)
    install_audit_error_handlers(app)
    app.include_router(
        build_identity_router(
            registry=providers.registry,
            human_auth=providers.human_auth,
            token_service=providers.token_service,
            lifecycle=lifecycle,
        )
    )
    app.include_router(
        build_audit_router(
            audit_log=providers.audit_log,
            human_auth=providers.human_auth,
        )
    )


__all__ = [
    "MemberBProviders",
    "assert_distinct_signing_keys",
    "assert_real_member_b_providers",
    "build_real_member_b_providers",
    "mount_member_b_routes",
]
