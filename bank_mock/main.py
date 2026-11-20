"""Member A separate FastAPI mock bank application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from bank_mock.routes import router as bank_router
from bank_mock.schema import (
    BankBase,
    create_bank_db_engine,
    create_bank_session_factory,
)
from bank_mock.service import seed_bank_data
from contracts import CONTRACT_VERSION
from gateway.config import AgentGateConfig


@asynccontextmanager
async def bank_lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = app.state.engine
    session_factory = app.state.session_factory

    # Initialize tables and seed baseline demo data
    async with engine.begin() as conn:
        await conn.run_sync(BankBase.metadata.create_all)

    async with session_factory() as session:
        await seed_bank_data(session)

    yield

    await engine.dispose()


def create_bank_app(
    db_url: str | None = None,
    service_key: str | None = None,
) -> FastAPI:
    """Create independent mock bank FastAPI application."""
    config = AgentGateConfig.load()
    db_url = db_url or config.bank_database_url

    engine = create_bank_db_engine(db_url)
    session_factory = create_bank_session_factory(engine)

    app = FastAPI(
        title="AgentGate Mock Bank",
        description="Private loopback ledger simulation for AgentGate",
        version=CONTRACT_VERSION,
        lifespan=bank_lifespan,
    )

    app.state.db_url = db_url
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.service_key = (
        service_key
        or config.bank_service_credential
        or "local-bank-dev-secret"
    )

    @app.exception_handler(StarletteHTTPException)
    async def bank_http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict):
            code = detail.get("code", "E_REQ_INVALID")
            message = detail.get("message", "Bank error")
        else:
            code = "E_REQ_INVALID"
            message = str(detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": code, "message": message}},
        )

    app.include_router(bank_router)
    return app


app = create_bank_app()
