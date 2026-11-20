"""Member B ops-authenticated FastAPI audit routes (`/v1/audit/*`)."""

from __future__ import annotations

import secrets
from asyncio import to_thread
from pathlib import Path

from fastapi import APIRouter, FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse

from contracts.audit_models import EvidenceBundle, VerifyReport
from contracts.common import ErrorDetail, ErrorEnvelope, HumanRole, Page
from contracts.identity_ports import HumanAuth
from contracts.reason_codes import ReasonCode
from gateway.identity.crypto import canonical_json
from gateway.identity.errors import AuthenticationError

from .append import AuditWriteError, SqlAuditLog
from .dispute import PrefixSeal, ProofError, Trust, load_checkpoint


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
    dispute_trust_path: str | None = None,
    dispute_checkpoint_path: str | None = None,
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

    @router.get("/v1/audit/proof/{request_id}", include_in_schema=False)
    async def get_dispute_proof(
        request_id: str, request: Request, authorization: str | None = Header(default=None)
    ) -> Response:
        await human_auth.authenticate(_extract_bearer_token(authorization), HumanRole.OPS)
        req_id = _request_id_for(request)

        def load_trust() -> tuple[Trust, PrefixSeal | None]:
            if dispute_trust_path is None:
                raise ProofError("Configure the independent dispute trust file before exporting.")
            trust = Trust.model_validate_json(Path(dispute_trust_path).read_bytes())
            checkpoint = (
                load_checkpoint(Path(dispute_checkpoint_path), trust)
                if dispute_checkpoint_path is not None else None
            )
            return trust, checkpoint

        try:
            trust, checkpoint = await to_thread(load_trust)
            proof = await audit_log.dispute_proof(request_id, trust, checkpoint)
            return Response(
                canonical_json(proof), media_type="application/json",
                headers={"Cache-Control": "no-store", "X-Request-ID": req_id},
            )
        except (OSError, ValueError) as exc:
            message = str(exc) if isinstance(exc, ProofError) else (
                "The dispute trust file, checkpoint or audit data could not be validated."
            )
            error = ErrorEnvelope(error=ErrorDetail(
                code=ReasonCode.E_REQ_INVALID.value, message=message, request_id=req_id,
            ))
            return JSONResponse(
                content=error.model_dump(mode="json"), status_code=409,
                headers={"Cache-Control": "no-store", "X-Request-ID": req_id},
            )

    return router


__all__ = [
    "build_audit_router",
    "install_audit_error_handlers",
]
