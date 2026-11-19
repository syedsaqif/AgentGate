"""Member A customer mandate management routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from contracts.common import HumanRole
from contracts.reason_codes import ReasonCode
from gateway.deps import Dependencies
from gateway.errors import AgentGateError
from gateway.mandates.models import (
    CreateMandateRequest,
    CreateMandateResponse,
    CustomerMandateListResponse,
)

router = APIRouter(prefix="/v1/customer", tags=["mandates"])
security = HTTPBearer(auto_error=False)


def get_deps(request: Request) -> Dependencies:
    deps = getattr(request.app.state, "deps", None)
    if not deps:
        raise HTTPException(status_code=500, detail="Dependencies not initialized")
    return deps


async def get_current_customer(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
    deps: Annotated[Dependencies, Depends(get_deps)],
) -> str:
    """Extract and authenticate customer session token."""
    if not credentials or not credentials.credentials:
        raise AgentGateError(
            code=ReasonCode.E_AUTH_MISSING,
            message="Missing customer session token in Authorization header",
            status_code=401,
        )

    if not deps.human_auth:
        raise AgentGateError(
            code=ReasonCode.E_SERVICE_UNAVAILABLE,
            message="Human authentication provider not configured",
            status_code=503,
        )

    try:
        context = await deps.human_auth.authenticate(
            credentials.credentials, required_role=HumanRole.CUSTOMER
        )
        return context.subject_id
    except Exception as exc:
        raise AgentGateError(
            code=ReasonCode.E_AUTH_MISSING,
            message=f"Invalid or expired customer session: {exc}",
            status_code=401,
        ) from exc


@router.post(
    "/mandates",
    response_model=CreateMandateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_mandate_endpoint(
    request: CreateMandateRequest,
    customer_id: Annotated[str, Depends(get_current_customer)],
    deps: Annotated[Dependencies, Depends(get_deps)],
) -> CreateMandateResponse:
    """Create a signed immutable mandate for the authenticated customer."""
    mandate_service = getattr(deps, "mandate_service", None)
    if not mandate_service:
        raise AgentGateError(
            code=ReasonCode.E_SERVICE_UNAVAILABLE,
            message="Mandate service is not configured",
            status_code=503,
        )

    return await mandate_service.create_mandate(customer_id, request)


@router.get("/mandates", response_model=CustomerMandateListResponse)
async def list_mandates_endpoint(
    customer_id: Annotated[str, Depends(get_current_customer)],
    deps: Annotated[Dependencies, Depends(get_deps)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> CustomerMandateListResponse:
    """List all mandates and usage owned by the authenticated customer."""
    mandate_service = getattr(deps, "mandate_service", None)
    if not mandate_service:
        raise AgentGateError(
            code=ReasonCode.E_SERVICE_UNAVAILABLE,
            message="Mandate service is not configured",
            status_code=503,
        )

    items, next_cursor = await mandate_service.list_customer_mandates(
        customer_id=customer_id, limit=limit, cursor=cursor
    )
    return CustomerMandateListResponse(items=items, next_cursor=next_cursor)
