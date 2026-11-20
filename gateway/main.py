"""Member A composition root and FastAPI application factory."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from contracts import CONTRACT_VERSION
from gateway.config import AgentGateConfig
from gateway.deps import Dependencies, build_dependencies
from gateway.errors import RequestIdMiddleware, register_error_handlers
from gateway.events.bus import EventBus
from gateway.events.routes import router as events_router
from gateway.executor.actions import ActionStore
from gateway.executor.reservations import ReservationManager
from gateway.executor.service import ExecutionService
from gateway.mandates.routes import router as customer_mandates_router
from gateway.mandates.service import MandateService, MandateUsageReader
from gateway.pipeline import router as pipeline_router
from gateway.platform.db import (
    Base,
    SqliteUnitOfWork,
    create_db_engine,
    create_session_factory,
)
from gateway.platform.health import router as health_router
from gateway.platform.lifecycle import LifecycleCoordinator
from gateway.platform.startup import init_database


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan managing database readiness and teardown."""
    deps: Dependencies = app.state.deps
    config: AgentGateConfig = app.state.config

    # Configure database engine & session factory if not already supplied
    if config.database.gateway_url:
        engine = getattr(app.state, "engine", None)
        if engine is None:
            gateway_url = config.database.gateway_url
            if config.gateway_db and os.path.isabs(config.gateway_db):
                gateway_url = f"sqlite+aiosqlite:///{config.gateway_db.replace('\\', '/')}"
            engine = create_db_engine(gateway_url)
            session_factory = create_session_factory(engine)
            app.state.engine = engine
            app.state.session_factory = session_factory
            if deps.unit_of_work is None:
                deps.unit_of_work = SqliteUnitOfWork(session_factory)
        await init_database(engine, Base.metadata)

    # Initialize mandate service if dependencies are present
    if deps.mandate_service is None and deps.unit_of_work is not None:
        deps.mandate_service = MandateService(
            human_auth=deps.human_auth,
            agent_registry=deps.agent_registry,
            audit_log=deps.audit_log,
            catalog=deps.catalog,
            clock=deps.clock,
            unit_of_work=deps.unit_of_work,
            session_factory=app.state.session_factory,
        )
        deps.mandate_reader = deps.mandate_service
        deps.usage_reader = MandateUsageReader(deps.mandate_service)
        deps.policy_evaluator = deps.mandate_service.policy_evaluator
        if "lazy_mandate_reader" in deps.extra:
            deps.extra["lazy_mandate_reader"].target = deps.mandate_service
        if "lazy_mandate_reader" in deps.extra:
            deps.extra["lazy_mandate_reader"].target = deps.mandate_service

    # Initialize execution services if dependencies are present
    session_factory = getattr(app.state, "session_factory", None)
    if deps.action_reader is None and session_factory:
        deps.action_reader = ActionStore(session_factory)
        deps.extra["action_store"] = deps.action_reader

    if deps.reservation_port is None and deps.mandate_reader is not None:
        deps.reservation_port = ReservationManager(deps.mandate_reader)
        deps.extra["reservation_manager"] = deps.reservation_port

    if deps.event_bus is None:
        deps.event_bus = EventBus()

    if (
        getattr(deps, "execution_service", None) is None
        and deps.unit_of_work is not None
        and deps.action_reader is not None
        and deps.reservation_port is not None
        and deps.bank_client is not None
        and deps.audit_log is not None
        and deps.mandate_reader is not None
        and session_factory
    ):
        exec_srv = ExecutionService(
            unit_of_work=deps.unit_of_work,
            action_store=deps.action_reader,
            reservation_manager=deps.reservation_port,
            bank_client=deps.bank_client,
            audit_log=deps.audit_log,
            mandate_reader=deps.mandate_reader,
            clock=deps.clock,
            session_factory=session_factory,
            event_bus=deps.event_bus,
        )
        deps.execution_service = exec_srv
        deps.execution_port = exec_srv
        deps.extra["execution_service"] = exec_srv

    if (
        deps.lifecycle is None
        and deps.unit_of_work is not None
        and deps.audit_log is not None
    ):
        deps.lifecycle = LifecycleCoordinator(
            unit_of_work=deps.unit_of_work,
            audit_log=deps.audit_log,
            clock=deps.clock,
            event_bus=deps.event_bus,
            token_service=deps.token_service,
            escalation_service=deps.escalation_service,
            agent_registry=deps.agent_registry,
        )

    if deps.escalation_service is not None:
        if getattr(deps.escalation_service, "session_factory", None) is None:
            deps.escalation_service.session_factory = getattr(app.state, "session_factory", None)
        if getattr(deps.escalation_service, "mandate_reader", None) is None:
            deps.escalation_service.mandate_reader = deps.mandate_reader
        if getattr(deps.escalation_service, "usage_reader", None) is None:
            deps.escalation_service.usage_reader = deps.usage_reader
        if getattr(deps.escalation_service, "execution_port", None) is None:
            deps.escalation_service.execution_port = deps.execution_port
        if getattr(deps.escalation_service, "audit_log", None) is None:
            deps.escalation_service.audit_log = deps.audit_log
        if getattr(deps.escalation_service, "catalog", None) is None:
            deps.escalation_service.catalog = deps.catalog
        if getattr(deps.escalation_service, "agent_registry", None) is None:
            deps.escalation_service.agent_registry = deps.agent_registry

    # Run recover_unresolved() during gateway lifespan startup
    exec_svc = getattr(deps, "execution_service", None) or getattr(deps, "execution_port", None)
    if exec_svc is not None and hasattr(exec_svc, "recover_unresolved"):
        await exec_svc.recover_unresolved()

    # Strict check: real profile must reject missing or fake security dependencies
    deps.validate_for_profile()

    yield

    # Teardown database resources
    engine = getattr(app.state, "engine", None)
    if engine is not None:
        await engine.dispose()


def create_app(
    profile: str = "test",
    overrides: dict[str, Any] | None = None,
) -> FastAPI:
    """FastAPI application factory for the modular monolith gateway."""
    config = AgentGateConfig.load(profile=profile, overrides=overrides)
    deps = build_dependencies(config=config, overrides=overrides)

    app = FastAPI(
        title="AgentGate Gateway",
        description="Securing Banks Against Rogue AI Agents",
        version=CONTRACT_VERSION,
        lifespan=lifespan,
    )

    app.state.profile = profile
    app.state.config = config
    app.state.deps = deps
    app.state.overrides = overrides

    # If DB URL provided, set engine & session factory so unit tests without lifespan have DB
    if config.database.gateway_url:
        engine = deps.extra.get("engine")
        session_factory = deps.extra.get("session_factory")
        if engine is None:
            gateway_url = config.database.gateway_url
            if config.gateway_db and os.path.isabs(config.gateway_db):
                gateway_url = f"sqlite+aiosqlite:///{config.gateway_db.replace('\\', '/')}"
            engine = create_db_engine(gateway_url)
            session_factory = create_session_factory(engine)
        app.state.engine = engine
        app.state.session_factory = session_factory
        if deps.unit_of_work is None:
            deps.unit_of_work = SqliteUnitOfWork(session_factory)

    # Ensure MandateService is wired on deps
    session_factory = getattr(app.state, "session_factory", None)
    if deps.mandate_service is None and deps.unit_of_work is not None and session_factory:
        deps.mandate_service = MandateService(
            human_auth=deps.human_auth,
            agent_registry=deps.agent_registry,
            audit_log=deps.audit_log,
            catalog=deps.catalog,
            clock=deps.clock,
            unit_of_work=deps.unit_of_work,
            session_factory=app.state.session_factory,
        )
        deps.mandate_reader = deps.mandate_service
        deps.usage_reader = MandateUsageReader(deps.mandate_service)
        deps.policy_evaluator = deps.mandate_service.policy_evaluator
        if "lazy_mandate_reader" in deps.extra:
            deps.extra["lazy_mandate_reader"].target = deps.mandate_service

    # Initialize execution services if dependencies are present
    session_factory = getattr(app.state, "session_factory", None)
    if deps.action_reader is None and session_factory:
        deps.action_reader = ActionStore(session_factory)
        deps.extra["action_store"] = deps.action_reader

    if deps.reservation_port is None and deps.mandate_reader is not None:
        deps.reservation_port = ReservationManager(deps.mandate_reader)
        deps.extra["reservation_manager"] = deps.reservation_port

    if deps.event_bus is None:
        deps.event_bus = EventBus()

    if (
        getattr(deps, "execution_service", None) is None
        and deps.unit_of_work is not None
        and deps.action_reader is not None
        and deps.reservation_port is not None
        and deps.bank_client is not None
        and deps.audit_log is not None
        and deps.mandate_reader is not None
        and session_factory
    ):
        exec_srv = ExecutionService(
            unit_of_work=deps.unit_of_work,
            action_store=deps.action_reader,
            reservation_manager=deps.reservation_port,
            bank_client=deps.bank_client,
            audit_log=deps.audit_log,
            mandate_reader=deps.mandate_reader,
            clock=deps.clock,
            session_factory=session_factory,
            event_bus=deps.event_bus,
        )
        deps.execution_service = exec_srv
        deps.execution_port = exec_srv
        deps.extra["execution_service"] = exec_srv

    if (
        deps.lifecycle is None
        and deps.unit_of_work is not None
        and deps.audit_log is not None
    ):
        deps.lifecycle = LifecycleCoordinator(
            unit_of_work=deps.unit_of_work,
            audit_log=deps.audit_log,
            clock=deps.clock,
            event_bus=deps.event_bus,
            token_service=deps.token_service,
            escalation_service=deps.escalation_service,
            agent_registry=deps.agent_registry,
        )

    if deps.escalation_service is not None:
        if getattr(deps.escalation_service, "session_factory", None) is None:
            deps.escalation_service.session_factory = getattr(app.state, "session_factory", None)
        if getattr(deps.escalation_service, "mandate_reader", None) is None:
            deps.escalation_service.mandate_reader = deps.mandate_reader
        if getattr(deps.escalation_service, "usage_reader", None) is None:
            deps.escalation_service.usage_reader = deps.usage_reader
        if getattr(deps.escalation_service, "execution_port", None) is None:
            deps.escalation_service.execution_port = deps.execution_port
        if getattr(deps.escalation_service, "audit_log", None) is None:
            deps.escalation_service.audit_log = deps.audit_log
        if getattr(deps.escalation_service, "catalog", None) is None:
            deps.escalation_service.catalog = deps.catalog
        if getattr(deps.escalation_service, "agent_registry", None) is None:
            deps.escalation_service.agent_registry = deps.agent_registry

    # Validate profile after dependencies are composed
    deps.validate_for_profile()

    # Register X-Request-ID and CORS middlewares
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )
    app.add_middleware(RequestIdMiddleware)

    # Enable CORS for browser UI access
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    # Register uniform error envelope handlers
    register_error_handlers(app)

    # Mount platform routers
    app.include_router(health_router)
    app.include_router(customer_mandates_router)
    app.include_router(pipeline_router)
    app.include_router(events_router)

    # Mount Member B identity router if identity services are configured
    if (
        deps.agent_registry is not None
        and deps.human_auth is not None
        and deps.token_service is not None
    ):
        from gateway.identity.routes import build_identity_router, install_identity_error_handlers

        install_identity_error_handlers(app)
        identity_router = build_identity_router(
            registry=deps.agent_registry,
            human_auth=deps.human_auth,
            token_service=deps.token_service,
            lifecycle=deps.lifecycle,
        )
        app.include_router(identity_router)

    # Mount Member B audit router if audit log is configured
    if deps.audit_log is not None and deps.human_auth is not None:
        from gateway.audit.routes import build_audit_router, install_audit_error_handlers

        install_audit_error_handlers(app)
        audit_router = build_audit_router(
            audit_log=deps.audit_log,
            human_auth=deps.human_auth,
        )
        app.include_router(audit_router)

    # Mount Member C escalation router if escalation service is configured
    if deps.escalation_service is not None:
        from gateway.escalation.routes import create_escalation_router

        escalation_router = create_escalation_router(
            service=deps.escalation_service,
            human_auth=deps.human_auth,
            lifecycle=deps.lifecycle,
            execution_port=deps.execution_port,
            audit_log=deps.audit_log,
            unit_of_work=deps.unit_of_work,
        )
        app.include_router(escalation_router)

    # Conditionally mount dev router ONLY in test and scripted profiles
    if profile in ("test", "scripted") and config.active_profile_settings.allow_dev_controls:
        from gateway.platform.dev import router as dev_router

        app.include_router(dev_router)

    return app


# Default app instance for ASGI servers
app = create_app(profile=os.getenv("AGENTGATE_PROFILE", "test"))
