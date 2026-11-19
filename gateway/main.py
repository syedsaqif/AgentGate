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
from gateway.mandates.routes import router as customer_mandates_router
from gateway.mandates.service import MandateService
from gateway.platform.db import (
    Base,
    SqliteUnitOfWork,
    create_db_engine,
    create_session_factory,
)
from gateway.platform.health import router as health_router
from gateway.platform.startup import init_database


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan managing database readiness and teardown."""
    deps: Dependencies = app.state.deps
    config: AgentGateConfig = app.state.config

    # Strict check: real profile must reject missing or fake security dependencies
    deps.validate_for_profile()

    # Configure database engine & session factory if not already supplied
    if config.database.gateway_url:
        engine = getattr(app.state, "engine", None)
        if engine is None:
            engine = create_db_engine(config.database.gateway_url)
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
        deps.usage_reader = deps.mandate_service
        deps.policy_evaluator = deps.mandate_service.policy_evaluator

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

    # Validate profile on app creation
    deps.validate_for_profile()

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
        engine = create_db_engine(config.database.gateway_url)
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
        deps.usage_reader = deps.mandate_service
        deps.policy_evaluator = deps.mandate_service.policy_evaluator

    # Register X-Request-ID middleware
    app.add_middleware(RequestIdMiddleware)

    # Register uniform error envelope handlers
    register_error_handlers(app)

    # Mount platform routers
    app.include_router(health_router)
    app.include_router(customer_mandates_router)

    return app


# Default app instance for ASGI servers
app = create_app(profile=os.getenv("AGENTGATE_PROFILE", "test"))
