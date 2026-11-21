"""AgentGate Demo Server application factory and composition root."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from demo_server.agent_brain import AgentBrain, RunManager, global_run_manager
from demo_server.bank_client import BankClient
from demo_server.config import DemoServerConfig
from demo_server.gateway_client import GatewayClient
from demo_server.routes.agent import router as agent_router
from demo_server.routes.bank import router as bank_router
from demo_server.routes.gate import router as gate_router
from demo_server.routes.reset import router as reset_router


@asynccontextmanager
async def demo_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Demo server lifespan managing startup and shutdown state."""
    app.state.session_start_time = datetime.now(UTC)
    yield


def create_demo_app(
    config: DemoServerConfig | None = None,
    gateway_client: GatewayClient | None = None,
    bank_client: BankClient | None = None,
    run_manager: RunManager | None = None,
    agent_brain: AgentBrain | None = None,
) -> FastAPI:
    """Create the standalone FastAPI demo backend for port 8002."""
    cfg = config or DemoServerConfig.from_env()

    app = FastAPI(
        title="AgentGate Demo Server",
        description="Presenter, bank app proxy, and gate visualizer relay for AgentGate",
        version="1.0.0",
        lifespan=demo_lifespan,
    )

    app.state.config = cfg
    app.state.session_start_time = datetime.now(UTC)
    app.state.gateway_client = gateway_client or GatewayClient(
        gateway_origin=cfg.gateway_origin,
        ops_id=cfg.ops_id,
        ops_password=cfg.ops_password,
    )
    app.state.bank_client = bank_client or BankClient(
        bank_origin=cfg.bank_origin,
        service_key=cfg.bank_service_key,
    )
    app.state.run_manager = run_manager or global_run_manager
    app.state.agent_brain = agent_brain or AgentBrain(
        config=cfg,
        gateway_client=app.state.gateway_client,
        run_manager=app.state.run_manager,
    )

    # Request ID middleware
    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next: Any) -> Any:
        req_id = request.headers.get("X-Request-ID") or f"req_{secrets.token_hex(6)}"
        request.state.request_id = req_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = req_id
        return response

    # CORS configuration for Vite frontend
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    # Standardized error envelope handler
    @app.exception_handler(StarletteHTTPException)
    async def demo_http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        req_id = getattr(request.state, "request_id", f"req_{secrets.token_hex(6)}")
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            envelope = detail
            if "request_id" not in envelope["error"]:
                envelope["error"]["request_id"] = req_id
        elif isinstance(detail, dict):
            code = detail.get("code", "E_REQ_INVALID")
            message = detail.get("message", "Error")
            envelope = {"error": {"code": code, "message": message, "request_id": req_id}}
        else:
            envelope = {
                "error": {
                    "code": "E_REQ_INVALID",
                    "message": str(detail),
                    "request_id": req_id,
                }
            }
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope,
            headers={"X-Request-ID": req_id},
        )

    # Mount demo endpoints (outside the gateway openapi inventory)
    app.include_router(bank_router, prefix="/demo/bank", tags=["demo_bank"])
    app.include_router(gate_router, prefix="/demo/gate", tags=["demo_gate"])
    app.include_router(reset_router, prefix="/demo", tags=["demo_reset"])
    app.include_router(agent_router, prefix="/demo", tags=["demo_agent"])

    @app.get("/health", tags=["health"])
    async def health_check() -> dict[str, str]:
        """Readiness and health check for the demo server."""
        return {
            "status": "ok",
            "service": "demo_server",
            "version": "1.0.0",
        }

    return app


# Default app instance for uvicorn
app = create_demo_app()

if __name__ == "__main__":
    import uvicorn

    cfg = DemoServerConfig.from_env()
    uvicorn.run("demo_server.main:app", host=cfg.host, port=cfg.port, log_level="info")
