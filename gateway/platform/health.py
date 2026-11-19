"""Member A health and readiness probes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from contracts import CONTRACT_VERSION

router = APIRouter(prefix="/health", tags=["health"])


class LiveResponse(BaseModel):
    status: str = "ok"


class ReadyResponse(BaseModel):
    status: str
    contract_version: str
    profile: str
    providers: dict[str, str]


@router.get("/live", response_model=LiveResponse)
async def health_live() -> dict[str, str]:
    """Liveness probe indicating the HTTP process is running."""
    return {"status": "ok"}


@router.get("/ready", response_model=ReadyResponse)
async def health_ready(request: Request) -> dict[str, Any]:
    """Readiness probe reporting contract version, profile, and provider modes."""
    deps = getattr(request.app.state, "deps", None)
    profile = getattr(request.app.state, "profile", "unknown")
    providers = deps.provider_modes() if deps else {}

    return {
        "status": "ready",
        "contract_version": CONTRACT_VERSION,
        "profile": profile,
        "providers": providers,
    }
