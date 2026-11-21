"""Member A typed dependency injection container and provider validation."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from contracts.audit_ports import AuditLog
from contracts.common import Clock, OpaqueId, Tx, UnitOfWork
from contracts.core_models import ExecutionResult
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
from contracts.escalation_models import Escalation, EscalationDecision
from contracts.escalation_ports import EscalationService
from contracts.identity_models import AgentPublicIdentity, AuthContext
from contracts.identity_ports import AgentRegistry, HumanAuth, IdentityVerifier, TokenService
from contracts.risk_ports import RiskEngine
from gateway.config import AgentGateConfig
from gateway.escalation.service import PersistentEscalationService
from gateway.mandates.models import Catalog
from gateway.platform.clock import SystemClock

_active_tx: ContextVar[Tx | None] = ContextVar("escalation_active_tx", default=None)


class ContextBoundAgentRegistry:
    """AgentRegistry decorator passing the active escalation transaction when omitted."""

    def __init__(self, target: Any) -> None:
        self._target = target

    async def get(self, agent_id: OpaqueId, tx: Tx | None = None) -> AgentPublicIdentity:
        effective_tx = tx or _active_tx.get()
        if effective_tx is not None:
            try:
                return await self._target.get(agent_id, tx=effective_tx)
            except TypeError:
                pass
        return await self._target.get(agent_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)


def _is_auto_tx(tx: Tx | None) -> bool:
    return bool(getattr(tx, "_auto_session", False) and hasattr(tx, "session") and tx.session)


class ConnectedEscalationService(PersistentEscalationService):
    """Escalation service adapter bridging unmanaged route transactions to session_factory."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._agent_registry: Any = None

    @property
    def agent_registry(self) -> Any:
        return self._agent_registry

    @agent_registry.setter
    def agent_registry(self, value: Any) -> None:
        if value is not None and not isinstance(value, ContextBoundAgentRegistry):
            self._agent_registry = ContextBoundAgentRegistry(value)
        else:
            self._agent_registry = value

    def _get_session(self, tx: Tx | None) -> Any:
        if tx is not None:
            if hasattr(tx, "session") and tx.session is not None:
                return tx.session
            if self.session_factory is not None:
                session = self.session_factory()
                tx.session = session
                tx._auto_session = True
                return session
        return super()._get_session(tx)

    async def decide(
        self,
        escalation_id: OpaqueId,
        decision: EscalationDecision,
        subject_id: OpaqueId,
        tx: Tx,
    ) -> Escalation:
        token = _active_tx.set(tx)
        try:
            res = await super().decide(escalation_id, decision, subject_id, tx)
            if _is_auto_tx(tx):
                await tx.session.commit()
            return res
        except Exception:
            if _is_auto_tx(tx):
                await tx.session.rollback()
                await tx.session.close()
                tx.session = None
            raise
        finally:
            _active_tx.reset(token)

    async def claim_execution(
        self,
        escalation_id: OpaqueId,
        expected_hash: str,
        tx: Tx,
    ) -> Escalation:
        token = _active_tx.set(tx)
        try:
            res = await super().claim_execution(escalation_id, expected_hash, tx)
            if _is_auto_tx(tx):
                await tx.session.commit()
            return res
        finally:
            _active_tx.reset(token)

    async def execute_approved(
        self,
        escalation_id: OpaqueId,
        tx: Tx,
        execution_port: ExecutionPort | None = None,
    ) -> ExecutionResult:
        token = _active_tx.set(tx)
        try:
            res = await super().execute_approved(escalation_id, tx, execution_port)
            if _is_auto_tx(tx):
                await tx.session.commit()
                await tx.session.close()
                tx.session = None
            return res
        except Exception:
            if _is_auto_tx(tx):
                await tx.session.rollback()
                await tx.session.close()
                tx.session = None
            raise
        finally:
            _active_tx.reset(token)

    async def cancel_for_mandate(self, mandate_id: OpaqueId, tx: Tx) -> None:
        try:
            res = await super().cancel_for_mandate(mandate_id, tx)
            if _is_auto_tx(tx):
                await tx.session.commit()
            return res
        except Exception:
            if _is_auto_tx(tx):
                await tx.session.rollback()
                await tx.session.close()
                tx.session = None
            raise

    async def cancel_for_agent(self, agent_id: OpaqueId, tx: Tx) -> None:
        try:
            res = await super().cancel_for_agent(agent_id, tx)
            if _is_auto_tx(tx):
                await tx.session.commit()
            return res
        except Exception:
            if _is_auto_tx(tx):
                await tx.session.rollback()
                await tx.session.close()
                tx.session = None
            raise


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


def _load_or_generate_key(path_str: str | None) -> Any:
    import os
    import stat
    from pathlib import Path

    from gateway.identity.crypto import (
        generate_ed25519_keypair,
        load_ed25519_private_key_pem,
        serialize_ed25519_private_key_pem,
    )

    if path_str:
        p = Path(path_str)
        if p.exists():
            return load_ed25519_private_key_pem(p.read_bytes())
        key, _ = generate_ed25519_keypair()
        p.parent.mkdir(parents=True, exist_ok=True)
        pem_bytes = serialize_ed25519_private_key_pem(key)
        p.write_bytes(pem_bytes)
        try:
            os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        return key
    key, _ = generate_ed25519_keypair()
    return key


def build_dependencies(
    config: AgentGateConfig, overrides: dict[str, Any] | None = None
) -> Dependencies:
    """Construct dependency container according to active profile."""
    if config.profile == "test":
        return build_test_dependencies(config, overrides)

    from gateway.escalation.service import EscalationConfig
    from gateway.events.bus import EventBus
    from gateway.executor.actions import ActionStore
    from gateway.executor.bank_client import HttpBankClient
    from gateway.executor.reservations import ReservationManager
    from gateway.executor.service import ExecutionService
    from gateway.mandates.catalog import TrustedCatalog
    from gateway.mandates.service import (
        DeferredMandateReader,
        MandateService,
        MandateUsageReader,
    )
    from gateway.platform.db import (
        SqliteUnitOfWork,
        create_db_engine,
        create_session_factory,
    )
    from gateway.platform.lifecycle import LifecycleCoordinator
    from gateway.risk.history import RiskHistoryStore
    from gateway.risk.scoring import DeterministicRiskEngine

    clock: Clock
    if config.active_profile_settings.frozen_clock:
        from tests.support.platform import FrozenClock

        clock = FrozenClock(datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC))
    else:
        clock = SystemClock()

    history_store = RiskHistoryStore()
    if config.profile in ("real", "scripted"):
        try:
            from simulator.history_seed import seed_synthetic_history

            seed_synthetic_history(history_store)
        except (ImportError, AttributeError):
            pass
    risk_engine = DeterministicRiskEngine(history_store=history_store)
    escalation_service = ConnectedEscalationService(
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

    session_factory = None
    engine = None
    unit_of_work = None
    if overrides and "unit_of_work" in overrides:
        unit_of_work = overrides["unit_of_work"]
    if overrides and "session_factory" in overrides:
        session_factory = overrides["session_factory"]

    gateway_url = config.gateway_database_url

    if session_factory is None and gateway_url:
        engine = create_db_engine(gateway_url)
        session_factory = create_session_factory(engine)
        if unit_of_work is None:
            unit_of_work = SqliteUnitOfWork(session_factory)

    human_auth = None
    agent_registry = None
    token_service = None
    audit_log = None
    catalog = None
    identity_verifier = None
    deferred_reader = None
    mandate_service = None
    mandate_reader = None
    usage_reader = None
    policy_evaluator = None
    action_reader = None
    reservation_port = None
    execution_service = None
    execution_port = None
    lifecycle = None

    if config.profile == "real":
        from gateway.identity.human_auth import HumanSessionKeyring
        from gateway.identity.wiring import build_real_member_b_providers

        token_key = _load_or_generate_key(config.token_signing_key_path)
        audit_key = _load_or_generate_key(config.resolved_audit_signing_key_path)
        mandate_key = _load_or_generate_key(config.mandate_signing_key_path)
        human_keyring = HumanSessionKeyring.generate()
        catalog = TrustedCatalog()
        deferred_reader = DeferredMandateReader()

        if session_factory is not None and unit_of_work is not None:
            b_providers = build_real_member_b_providers(
                session_factory=session_factory,
                clock=clock,
                mandate_reader=deferred_reader,
                trusted_origin=config.gateway_origin,
                token_signing_key=token_key,
                audit_signing_key=audit_key,
                human_keyring=human_keyring,
            )
            human_auth = b_providers.human_auth
            agent_registry = b_providers.registry
            token_service = b_providers.token_service
            audit_log = b_providers.audit_log
            identity_verifier = b_providers.identity_verifier

            mandate_service = MandateService(
                human_auth=human_auth,
                agent_registry=agent_registry,
                audit_log=audit_log,
                catalog=catalog,
                clock=clock,
                unit_of_work=unit_of_work,
                session_factory=session_factory,
                signing_key=mandate_key,
            )
            deferred_reader.target = mandate_service
            mandate_reader = mandate_service
            usage_reader = MandateUsageReader(mandate_service)
            policy_evaluator = mandate_service.policy_evaluator

            action_reader = ActionStore(session_factory)
            reservation_port = ReservationManager(mandate_reader)
            exec_srv = ExecutionService(
                unit_of_work=unit_of_work,
                action_store=action_reader,
                reservation_manager=reservation_port,
                bank_client=bank_client,
                audit_log=audit_log,
                mandate_reader=mandate_reader,
                clock=clock,
                session_factory=session_factory,
                event_bus=event_bus,
            )
            execution_service = exec_srv
            execution_port = exec_srv

            lifecycle = LifecycleCoordinator(
                unit_of_work=unit_of_work,
                audit_log=audit_log,
                clock=clock,
                event_bus=event_bus,
                token_service=token_service,
                escalation_service=escalation_service,
                agent_registry=agent_registry,
            )

            escalation_service.session_factory = session_factory
            escalation_service.mandate_reader = mandate_reader
            escalation_service.usage_reader = usage_reader
            escalation_service.execution_port = execution_port
            escalation_service.audit_log = audit_log
            escalation_service.catalog = catalog
            escalation_service.agent_registry = agent_registry

    elif config.profile == "scripted":
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
        unit_of_work=unit_of_work,
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
        mandate_service=mandate_service,
        mandate_reader=mandate_reader,
        usage_reader=usage_reader,
        policy_evaluator=policy_evaluator,
        action_reader=action_reader,
        execution_port=execution_port,
        reservation_port=reservation_port,
        lifecycle=lifecycle,
    )
    if deferred_reader is not None:
        deps.extra["lazy_mandate_reader"] = deferred_reader

    if execution_service is not None:
        deps.extra["execution_service"] = execution_service
    if action_reader is not None:
        deps.extra["action_store"] = action_reader
    if reservation_port is not None:
        deps.extra["reservation_manager"] = reservation_port
    if session_factory is not None:
        deps.extra["session_factory"] = session_factory
    if engine is not None:
        deps.extra["engine"] = engine

    if overrides:
        for k, v in overrides.items():
            if hasattr(deps, k):
                setattr(deps, k, v)
            else:
                deps.extra[k] = v

    return deps
