"""Member A developer and scripted test control endpoints.

Strict guard: Mounted ONLY in test and scripted profiles; ABSENT in real profile.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from contracts.reason_codes import ReasonCode
from gateway.config import AgentGateConfig
from gateway.deps import Dependencies
from gateway.errors import AgentGateError
from gateway.platform.db import Base

router = APIRouter(prefix="/v1/dev", tags=["dev"])

DEFAULT_DEV_KEY = "dev-control-secret"


class ClockControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    now: str | None = None
    advance_seconds: int | None = None


class ResetControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seed: bool = True


def _verify_dev_credential(
    request: Request,
    dev_key_a: str | None = None,
    dev_key_b: str | None = None,
) -> None:
    """Verify developer control credential from headers."""
    config: AgentGateConfig = request.app.state.config
    expected = config.dev_control_credential or DEFAULT_DEV_KEY

    supplied = dev_key_a or dev_key_b
    if not supplied or supplied != expected:
        raise AgentGateError(
            code=ReasonCode.E_AUTH_MISSING,
            message="Missing or invalid dev control credential",
            status_code=401,
        )


@router.post("/clock")
async def dev_clock(
    request: Request,
    body: ClockControlRequest,
    x_agentgate_dev_credential: Annotated[
        str | None, Header(alias="X-AgentGate-Dev-Credential")
    ] = None,
    x_dev_control_key: Annotated[
        str | None, Header(alias="X-Dev-Control-Key")
    ] = None,
) -> JSONResponse:
    """Advance or freeze system clock for deterministic testing."""
    _verify_dev_credential(request, x_agentgate_dev_credential, x_dev_control_key)
    deps: Dependencies = request.app.state.deps
    clock = deps.clock

    if body.now is not None:
        try:
            target_dt = datetime.fromisoformat(body.now)
            if target_dt.tzinfo is None:
                target_dt = target_dt.replace(tzinfo=UTC)
            else:
                target_dt = target_dt.astimezone(UTC)
        except Exception as exc:
            raise AgentGateError(
                code=ReasonCode.E_REQ_INVALID,
                message=f"Invalid ISO datetime string: {exc}",
                status_code=400,
            ) from exc

        if hasattr(clock, "set_time"):
            clock.set_time(target_dt)
        elif hasattr(clock, "value"):
            clock.value = target_dt
        else:
            raise AgentGateError(
                code=ReasonCode.E_REQ_INVALID,
                message="Current clock implementation does not support set_time",
                status_code=400,
            )

    if body.advance_seconds is not None:
        if hasattr(clock, "advance"):
            clock.advance(seconds=body.advance_seconds)
        else:
            raise AgentGateError(
                code=ReasonCode.E_REQ_INVALID,
                message="Current clock implementation does not support advance",
                status_code=400,
            )

    return JSONResponse(
        status_code=200,
        content={"clock": clock.now().isoformat().replace("+00:00", "Z")},
    )


@router.post("/reset")
async def dev_reset(
    request: Request,
    body: ResetControlRequest | None = None,
    x_agentgate_dev_credential: Annotated[
        str | None, Header(alias="X-AgentGate-Dev-Credential")
    ] = None,
    x_dev_control_key: Annotated[
        str | None, Header(alias="X-Dev-Control-Key")
    ] = None,
) -> JSONResponse:
    """Reset disposable demo and test database state."""
    _verify_dev_credential(request, x_agentgate_dev_credential, x_dev_control_key)
    engine = getattr(request.app.state, "engine", None)
    if engine is not None:
        async with engine.begin() as conn:
            # Recreate all tables cleanly
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

    deps: Dependencies = request.app.state.deps
    if deps.event_bus is not None and hasattr(deps.event_bus, "clear"):
        deps.event_bus.clear()

    return JSONResponse(status_code=200, content={"status": "RESET"})
