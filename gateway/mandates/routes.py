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
    RevokeMandateResponse,
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


@router.post("/mandates/{mandate_id}/revoke", response_model=RevokeMandateResponse)
async def revoke_mandate_endpoint(
    mandate_id: str,
    customer_id: Annotated[str, Depends(get_current_customer)],
    deps: Annotated[Dependencies, Depends(get_deps)],
) -> RevokeMandateResponse:
    """Revoke a customer-owned mandate, its tokens, and pending approvals."""
    mandate_service = getattr(deps, "mandate_service", None)
    if not mandate_service or not deps.lifecycle:
        raise AgentGateError(
            code=ReasonCode.E_SERVICE_UNAVAILABLE,
            message="Mandate or lifecycle service is not configured",
            status_code=503,
        )

    # Enforce ownership: mandate must exist and belong to the authenticated customer
    try:
        snapshot = await mandate_service.get(mandate_id)
        if snapshot.terms.customer_id != customer_id:
            raise AgentGateError(
                code=ReasonCode.E_REQ_INVALID,
                message="Mandate not found",
                status_code=404,
            )
    except AgentGateError as e:
        if e.status_code == 404:
            raise
        raise AgentGateError(
            code=ReasonCode.E_REQ_INVALID,
            message="Mandate not found",
            status_code=404,
        ) from e
    except Exception as exc:
        raise AgentGateError(
            code=ReasonCode.E_REQ_INVALID,
            message="Mandate not found",
            status_code=404,
        ) from exc

    audit_ref = await deps.lifecycle.revoke_mandate(
        mandate_id=mandate_id,
        actor=customer_id,
        reason="Customer requested revocation",
    )
    return RevokeMandateResponse(
        mandate_id=mandate_id,
        status="REVOKED",
        audit_ref=audit_ref,
    )
