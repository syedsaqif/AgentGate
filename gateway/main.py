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
from gateway.platform.db import Base, create_db_engine, create_session_factory
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
    if deps.unit_of_work is None and config.database.gateway_url:
        engine = create_db_engine(config.database.gateway_url)
        session_factory = create_session_factory(engine)
        app.state.engine = engine
        app.state.session_factory = session_factory
        await init_database(engine, Base.metadata)

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

    # Register X-Request-ID middleware
    app.add_middleware(RequestIdMiddleware)

    # Register uniform error envelope handlers
    register_error_handlers(app)

    # Mount platform health router
    app.include_router(health_router)

    return app


# Default app instance for ASGI servers
app = create_app(profile=os.getenv("AGENTGATE_PROFILE", "test"))
