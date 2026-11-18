"""Member B ops-authenticated FastAPI audit routes (`/v1/audit/*`)."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse

from contracts.audit_models import EvidenceBundle, VerifyReport
from contracts.common import ErrorDetail, ErrorEnvelope, HumanRole, Page
from contracts.identity_ports import HumanAuth
from contracts.reason_codes import ReasonCode
from gateway.identity.errors import AuthenticationError

from .append import AuditWriteError, SqlAuditLog


def _request_id_for(request: Request) -> str:
    existing = getattr(request.state, "request_id", None)
    if isinstance(existing, str) and existing:
        return existing
    generated = f"req_{secrets.token_hex(8)}"
    request.state.request_id = generated
    return generated


def _extract_bearer_token(authorization: str | None) -> str:
    if authorization is None or not authorization.strip():
        raise AuthenticationError(
            ReasonCode.E_AUTH_MISSING,
            "Missing Authorization bearer token.",
            internal_reason="missing_authorization_header",
        )
    parts = authorization.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise AuthenticationError(
            ReasonCode.E_AUTH_INVALID_TOKEN,
            "Authorization header must use the Bearer scheme.",
            internal_reason="invalid_authorization_scheme",
        )
    return parts[1].strip()


def install_audit_error_handlers(app: FastAPI) -> None:
    """Install frozen ErrorEnvelope handler for `AuditWriteError` (503 E_AUDIT_WRITE_FAILED)."""

    @app.exception_handler(AuditWriteError)
    async def _handle_audit_write_error(
        request: Request, exc: AuditWriteError
    ) -> JSONResponse:
        req_id = _request_id_for(request)
        envelope = ErrorEnvelope(
            error=ErrorDetail(code=exc.code.value, message=exc.message, request_id=req_id)
        )
        return JSONResponse(
            status_code=exc.http_status,
            content=envelope.model_dump(mode="json"),
            headers={"X-Request-ID": req_id},
        )


def build_audit_router(
    *,
    audit_log: SqlAuditLog,
    human_auth: HumanAuth,
) -> APIRouter:
    """Build Member B's ops-authenticated audit query, verify, and evidence router."""

    router = APIRouter()

    @router.get("/v1/audit/entries", response_model=Page)
    async def list_audit_entries(
        request: Request,
        response: Response,
        authorization: str | None = Header(default=None),
        cursor: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
        event_type: str | None = Query(default=None),
        request_id: str | None = Query(default=None),
        customer_id: str | None = Query(default=None),
        agent_id: str | None = Query(default=None),
    ) -> Page:
        req_id = _request_id_for(request)
        response.headers["X-Request-ID"] = req_id
        token = _extract_bearer_token(authorization)
        await human_auth.authenticate(token, HumanRole.OPS)
        return await audit_log.list_entries(
            cursor=cursor,
            limit=limit,
            event_type=event_type,
            request_id=request_id,
            customer_id=customer_id,
            agent_id=agent_id,
        )

    @router.get("/v1/audit/verify", response_model=VerifyReport)
    async def verify_audit(
        request: Request,
        response: Response,
        authorization: str | None = Header(default=None),
    ) -> VerifyReport:
        req_id = _request_id_for(request)
        response.headers["X-Request-ID"] = req_id
        token = _extract_bearer_token(authorization)
        await human_auth.authenticate(token, HumanRole.OPS)
        return await audit_log.verify()

    @router.get("/v1/audit/evidence/{request_id}", response_model=EvidenceBundle)
    async def get_audit_evidence(
        request_id: str,
        request: Request,
        response: Response,
        authorization: str | None = Header(default=None),
    ) -> EvidenceBundle:
        req_id = _request_id_for(request)
        response.headers["X-Request-ID"] = req_id
        token = _extract_bearer_token(authorization)
        await human_auth.authenticate(token, HumanRole.OPS)
        return await audit_log.evidence(request_id)

    return router


__all__ = [
    "build_audit_router",
    "install_audit_error_handlers",
]
