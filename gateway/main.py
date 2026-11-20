"""Member A composition root and FastAPI application factory."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

# Import models to ensure they are registered in Base.metadata
import gateway.mandates.models  # noqa: F401
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
    engine_to_dispose: AsyncEngine | None = getattr(app.state, "engine", None)
    if engine_to_dispose is not None:
        await engine_to_dispose.dispose()


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

    if config.database.gateway_url:
        engine = create_db_engine(config.database.gateway_url)
        session_factory = create_session_factory(engine)
        app.state.engine = engine
        app.state.session_factory = session_factory
        db_url = config.database.gateway_url
        if "sqlite" in db_url:
            raw_path = db_url.split(":///")[-1]
            try:
                import sqlite3

                from sqlalchemy.schema import CreateTable

                s_conn = sqlite3.connect(raw_path)
                try:
                    for table in Base.metadata.sorted_tables:
                        ddl = str(CreateTable(table).compile(dialect=engine.sync_engine.dialect))
                        clean_ddl = ddl.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ")
                        s_conn.execute(clean_ddl)
                    s_conn.commit()
                finally:
                    s_conn.close()
            except Exception:
                pass

    # Register X-Request-ID middleware
    app.add_middleware(RequestIdMiddleware)

    # Register uniform error envelope handlers
    register_error_handlers(app)

    # Mount platform routers
    from gateway.mandates.routes import router as mandates_router

    app.include_router(health_router)
    app.include_router(mandates_router)

    return app


# Default app instance for ASGI servers
app = create_app(profile=os.getenv("AGENTGATE_PROFILE", "test"))
