"""Member A typed dependency injection container and provider validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from contracts.audit_ports import AuditLog
from contracts.common import Clock, UnitOfWork
from contracts.core_ports import (
    ActionReader,
    BankClient,
    ExecutionPort,
    Lifecycle,
    MandateReader,
    PolicyEvaluator,
    ReservationPort,
    UsageReader,
)
from contracts.escalation_ports import EscalationService
from contracts.identity_models import AuthContext
from contracts.identity_ports import AgentRegistry, HumanAuth, IdentityVerifier, TokenService
from contracts.risk_ports import RiskEngine
from gateway.config import AgentGateConfig
from gateway.mandates.models import Catalog
from gateway.platform.clock import SystemClock


class LazyMandateReader(MandateReader):
    """Proxy mandate reader resolved once MandateService is bound."""

    def __init__(self, target: MandateReader | None = None) -> None:
        self.target = target

    async def get(self, mandate_id: Any, tx: Any = None) -> Any:
        if self.target is None:
            raise RuntimeError("MandateReader target not yet bound")
        return await self.target.get(mandate_id, tx)


def is_fake_provider(provider: Any) -> bool:
    """Detect whether a given provider is a test double or fake."""
    if provider is None:
        return False
    if getattr(provider, "__is_fake__", False):
        return True
    cls_name = provider.__class__.__name__
    cls_module = provider.__class__.__module__
    if "tests.support" in cls_module:
        return True
    fake_markers = ("Fake", "Fixture", "Memory", "Recording", "Mock")
    return any(marker in cls_name for marker in fake_markers)


@dataclass
class Dependencies:
    """Container holding typed service interfaces injected into the gateway application."""

    config: AgentGateConfig
    clock: Clock
    unit_of_work: UnitOfWork | None = None

    identity_verifier: IdentityVerifier | None = None
    human_auth: HumanAuth | None = None
    agent_registry: AgentRegistry | None = None
    token_service: TokenService | None = None
    catalog: Catalog | None = None

    audit_log: AuditLog | None = None
    risk_engine: RiskEngine | None = None
    escalation_service: EscalationService | None = None

    mandate_service: Any = None
    mandate_reader: MandateReader | None = None
    usage_reader: UsageReader | None = None
    policy_evaluator: PolicyEvaluator | None = None
    action_reader: ActionReader | None = None
    execution_port: ExecutionPort | None = None
    reservation_port: ReservationPort | None = None
    lifecycle: Lifecycle | None = None
    bank_client: BankClient | None = None
    event_bus: Any = None

    extra: dict[str, Any] = field(default_factory=dict)

    def provider_modes(self) -> dict[str, str]:
        """Summarize provider modes (real vs fake vs unconfigured) for readiness reporting."""
        services = {
            "identity": self.identity_verifier,
            "human_auth": self.human_auth,
            "audit": self.audit_log,
            "risk": self.risk_engine,
            "escalation": self.escalation_service,
            "bank": self.bank_client,
            "policy": self.policy_evaluator,
            "mandates": self.mandate_reader,
        }
        summary: dict[str, str] = {}
        for name, provider in services.items():
            if provider is None:
                summary[name] = "unconfigured"
            elif is_fake_provider(provider):
                summary[name] = "fake"
            else:
                summary[name] = "real"
        return summary

    def validate_for_profile(self) -> None:
        """Enforce strict profile security guards.
        
        Under profile 'real', no security provider may be missing or bound to a test fake.
        Under profile 'scripted', test doubles are likewise forbidden unless explicitly configured.
        """
        profile = self.config.profile
        allow_fakes = self.config.active_profile_settings.allow_fakes

        critical_providers = {
            "identity_verifier": self.identity_verifier,
            "human_auth": self.human_auth,
            "audit_log": self.audit_log,
            "policy_evaluator": self.policy_evaluator,
            "risk_engine": self.risk_engine,
            "escalation_service": self.escalation_service,
            "bank_client": self.bank_client,
        }

        if profile == "real":
            missing: list[str] = []
            fakes: list[str] = []
            for name, provider in critical_providers.items():
                if provider is None:
                    missing.append(name)
                elif is_fake_provider(provider):
                    fakes.append(f"{name} ({provider.__class__.__name__})")

            errors: list[str] = []
            if missing:
                errors.append(f"Missing required real providers: {', '.join(missing)}")
            if fakes:
                errors.append(f"Fakes not permitted in 'real' profile: {', '.join(fakes)}")

            if errors:
                raise RuntimeError(
                    f"Refusing startup in profile 'real': {'; '.join(errors)}"
                )
        elif not allow_fakes:
            # Scripted profile: forbid fakes on implemented modules, allow fallback for stubs
            implemented_real_providers = {
                "policy_evaluator": self.policy_evaluator,
                "risk_engine": self.risk_engine,
                "escalation_service": self.escalation_service,
                "bank_client": self.bank_client,
            }
            missing = [k for k, v in implemented_real_providers.items() if v is None]
            fakes = [
                f"{k} ({v.__class__.__name__})"
                for k, v in implemented_real_providers.items()
                if v is not None and is_fake_provider(v)
            ]
            errors = []
            if missing:
                errors.append(f"Missing required real providers: {', '.join(missing)}")
            if fakes:
                fakes_str = ", ".join(fakes)
                errors.append(
                    f"Fakes not permitted in '{profile}' profile for implemented services: "
                    f"{fakes_str}"
                )
            if errors:
                raise RuntimeError(
                    f"Refusing startup in profile '{profile}': {'; '.join(errors)}"
                )


def build_test_dependencies(
    config: AgentGateConfig, overrides: dict[str, Any] | None = None
) -> Dependencies:
    """Build default dependencies for the test profile using support test doubles."""
    from contracts.risk_models import RiskLevel, RiskResult
    from gateway.events.bus import EventBus
    from tests.support.a.providers import (
        FakeAgentRegistry,
        FakeBank,
        FakeCatalog,
        FakeHumanAuth,
        FakeTokenService,
    )
    from tests.support.b.providers import FixtureIdentityVerifier, MemoryAuditLog
    from tests.support.c.providers import (
        FixtureRiskEngine,
        MemoryEscalationStore,
        RecordingExecutionPort,
    )
    from tests.support.platform import FrozenClock

    default_auth = AuthContext(
        customer_id="cust_001",
        agent_id="agt_billpal",
        mandate_id="mnd_demo_1",
        mandate_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
        jkt="jkt_test_thumbprint",
        token_jti="tok_test_jti",
        proof_jti="prf_test_jti",
    )
    default_risk = RiskResult(
        score=20,
        level=RiskLevel.LOW,
        recommended="ALLOW",
        signals=(),
        breaker_recommended=False,
        breaker_reasons=(),
        model_version="rules_v1",
    )

    clock = FrozenClock(datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC))
    human_auth = FakeHumanAuth()
    agent_registry = FakeAgentRegistry()
    token_service = FakeTokenService()
    audit_log = MemoryAuditLog()
    catalog = FakeCatalog()
    event_bus = EventBus()
    escalation_service = MemoryEscalationStore()

    deps = Dependencies(
        config=config,
        clock=clock,
        human_auth=human_auth,
        agent_registry=agent_registry,
        token_service=token_service,
        catalog=catalog,
        identity_verifier=FixtureIdentityVerifier(result=default_auth),
        audit_log=audit_log,
        risk_engine=FixtureRiskEngine(result=default_risk),
        escalation_service=escalation_service,
        execution_port=RecordingExecutionPort(),
        bank_client=FakeBank(),
        event_bus=event_bus,
    )

    if overrides:
        for k, v in overrides.items():
            if hasattr(deps, k):
                setattr(deps, k, v)
            else:
                deps.extra[k] = v

    return deps


def build_dependencies(
    config: AgentGateConfig, overrides: dict[str, Any] | None = None
) -> Dependencies:
    """Construct dependency container according to active profile."""
    if config.profile == "test":
        return build_test_dependencies(config, overrides)

    from gateway.escalation.service import EscalationConfig, PersistentEscalationService
    from gateway.events.bus import EventBus
    from gateway.executor.bank_client import HttpBankClient
    from gateway.risk.history import RiskHistoryStore
    from gateway.risk.scoring import DeterministicRiskEngine
    from tests.support.platform import FrozenClock

    clock = (
        FrozenClock(datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC))
        if config.active_profile_settings.frozen_clock
        else SystemClock()
    )

    risk_engine = DeterministicRiskEngine(history_store=RiskHistoryStore())
    escalation_service = PersistentEscalationService(
        config=EscalationConfig(
            default_ttl_seconds=config.approval.default_ttl_seconds,
            scripted_ttl_seconds=config.approval.scripted_ttl_seconds,
            max_pending_per_mandate=config.approval.max_pending_per_mandate,
            profile=config.profile,
        ),
        clock=clock,
    )
    bank_client = HttpBankClient(
        base_url=config.bank_origin,
        service_key=config.bank_service_credential or "local-bank-dev-secret",
    )
    event_bus = EventBus()

    # Member B providers (check if real available, else fallback to fakes in scripted)
    human_auth = None
    agent_registry = None
    token_service = None
    audit_log = None
    catalog = None
    identity_verifier = None
    lazy_mandate_reader = None

    if config.profile == "real":
        from pathlib import Path
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from gateway.identity.crypto import generate_ed25519_keypair
        from gateway.identity.human_auth import HumanSessionKeyring
        from gateway.identity.schema import (
            create_identity_schema,
            create_identity_session_factory,
            create_sqlite_identity_engine,
        )
        from gateway.audit.schema import create_audit_schema
        from gateway.identity.wiring import build_real_member_b_providers
        from gateway.mandates.models import DefaultCatalog

        def _get_key(path_str: str | None) -> Ed25519PrivateKey:
            if path_str and Path(path_str).exists():
                loaded = serialization.load_pem_private_key(
                    Path(path_str).read_bytes(), password=None
                )
                if isinstance(loaded, Ed25519PrivateKey):
                    return loaded
            key, _ = generate_ed25519_keypair()
            if path_str:
                p = Path(path_str)
                p.parent.mkdir(parents=True, exist_ok=True)
                pem = key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption(),
                )
                p.write_bytes(pem)
            return key

        token_signing_key = _get_key(config.token_signing_key_path)
        audit_signing_key = _get_key(config.audit_signing_key_path)
        human_keyring = HumanSessionKeyring.generate()

        b_engine = create_sqlite_identity_engine(config.database.gateway_url)
        create_identity_schema(b_engine)
        create_audit_schema(b_engine)
        sf = create_identity_session_factory(b_engine)

        lazy_mandate_reader = LazyMandateReader()
        b_providers = build_real_member_b_providers(
            session_factory=sf,
            clock=clock,
            mandate_reader=lazy_mandate_reader,
            trusted_origin=config.gateway_origin,
            token_signing_key=token_signing_key,
            audit_signing_key=audit_signing_key,
            human_keyring=human_keyring,
        )

        human_auth = b_providers.human_auth
        agent_registry = b_providers.registry
        token_service = b_providers.token_service
        audit_log = b_providers.audit_log
        identity_verifier = b_providers.identity_verifier
        catalog = DefaultCatalog()

    elif config.profile == "scripted":
        # Fallback to test doubles for Member B modules not yet implemented on main
        from tests.support.a.providers import (
            FakeAgentRegistry,
            FakeCatalog,
            FakeHumanAuth,
            FakeTokenService,
        )
        from tests.support.b.providers import FixtureIdentityVerifier, MemoryAuditLog

        default_auth = AuthContext(
            customer_id="cust_001",
            agent_id="agt_billpal",
            mandate_id="mnd_demo_1",
            mandate_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
            jkt="jkt_test_thumbprint",
            token_jti="tok_test_jti",
            proof_jti="prf_test_jti",
        )
        human_auth = FakeHumanAuth()
        agent_registry = FakeAgentRegistry()
        token_service = FakeTokenService()
        audit_log = MemoryAuditLog()
        catalog = FakeCatalog()
        identity_verifier = FixtureIdentityVerifier(result=default_auth)

    deps = Dependencies(
        config=config,
        clock=clock,
        human_auth=human_auth,
        agent_registry=agent_registry,
        token_service=token_service,
        catalog=catalog,
        identity_verifier=identity_verifier,
        audit_log=audit_log,
        risk_engine=risk_engine,
        escalation_service=escalation_service,
        bank_client=bank_client,
        event_bus=event_bus,
    )
    if lazy_mandate_reader is not None:
        deps.extra["lazy_mandate_reader"] = lazy_mandate_reader

    if overrides:
        for k, v in overrides.items():
            if hasattr(deps, k):
                setattr(deps, k, v)
            else:
                deps.extra[k] = v

    return deps
