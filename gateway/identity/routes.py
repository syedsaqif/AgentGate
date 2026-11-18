"""Member B FastAPI routes for human authentication and agent registry management."""

from __future__ import annotations

import json
import secrets
from typing import Any

from fastapi import APIRouter, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from contracts.common import ErrorDetail, ErrorEnvelope, HumanRole, Page
from contracts.core_ports import Lifecycle
from contracts.identity_models import AccessTokenResult, AgentPublicIdentity
from contracts.reason_codes import ReasonCode

from .errors import AuthenticationError, IdentityError, ValidationDomainError
from .human_auth import HumanSessionResult, SqlHumanAuthService
from .proofs import extract_dpop_header
from .registry import SqlAgentRegistry
from .tokens import SqlTokenService


class _StrictRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CustomerLoginRequest(_StrictRequestModel):
    customer_id: str = Field(min_length=1, max_length=64)
    pin: str = Field(min_length=1, max_length=64)


class OpsLoginRequest(_StrictRequestModel):
    operator_id: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class RegisterAgentRequest(_StrictRequestModel):
    name: str = Field(min_length=1, max_length=120)
    vendor: str = Field(min_length=1, max_length=120)
    public_jwk: dict[str, Any]
    max_scopes: list[str] = Field(min_length=1)


class SuspendAgentRequest(_StrictRequestModel):
    reason: str | None = Field(default=None, max_length=200)


class TokenIssueRequest(_StrictRequestModel):
    client_id: str = Field(min_length=1, max_length=64)
    mandate_id: str = Field(min_length=1, max_length=64)


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


def install_identity_error_handlers(app: FastAPI) -> None:
    """Install frozen ErrorEnvelope handlers for IdentityError and request validation errors."""

    @app.exception_handler(IdentityError)
    async def _handle_identity_error(request: Request, exc: IdentityError) -> JSONResponse:
        req_id = _request_id_for(request)
        envelope = ErrorEnvelope(
            error=ErrorDetail(code=exc.code.value, message=exc.message, request_id=req_id)
        )
        headers = {"X-Request-ID": req_id}
        if exc.retry_after_seconds is not None:
            headers["Retry-After"] = str(exc.retry_after_seconds)
        return JSONResponse(
            status_code=exc.http_status,
            content=envelope.model_dump(mode="json"),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        req_id = _request_id_for(request)
        envelope = ErrorEnvelope(
            error=ErrorDetail(
                code=ReasonCode.E_REQ_INVALID.value,
                message="Request validation failed.",
                request_id=req_id,
            )
        )
        return JSONResponse(
            status_code=400,
            content=envelope.model_dump(mode="json"),
            headers={"X-Request-ID": req_id},
        )


def build_identity_router(
    *,
    registry: SqlAgentRegistry,
    human_auth: SqlHumanAuthService,
    token_service: SqlTokenService | None = None,
    lifecycle: Lifecycle | None = None,
) -> APIRouter:
    """Build Member B's FastAPI router with injected services (no global singletons)."""

    router = APIRouter()

    @router.post("/v1/oauth/token", response_model=AccessTokenResult)
    async def issue_agent_token(
        request: Request,
        response: Response,
    ) -> AccessTokenResult:
        req_id = _request_id_for(request)
        response.headers["X-Request-ID"] = req_id
        response.headers["Cache-Control"] = "no-store"
        if token_service is None:
            raise IdentityError(
                ReasonCode.E_SERVICE_UNAVAILABLE,
                "Token service is unavailable.",
                http_status=503,
            )
        raw_body = await request.body()
        try:
            parsed = json.loads(raw_body.decode("utf-8"))
            payload = TokenIssueRequest.model_validate(parsed)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
            raise ValidationDomainError(
                "Invalid token request payload.", internal_reason="invalid_token_request_body"
            ) from exc

        return await token_service.issue_token(
            client_id=payload.client_id,
            mandate_id=payload.mandate_id,
            proof_jwt=extract_dpop_header(request.headers),
            method="POST",
            request_url=str(request.url),
            request_bytes=raw_body,
            request_id=req_id,
        )

    @router.post("/v1/customer/login", response_model=HumanSessionResult)
    async def customer_login(
        payload: CustomerLoginRequest,
        request: Request,
        response: Response,
    ) -> HumanSessionResult:
        req_id = _request_id_for(request)
        response.headers["X-Request-ID"] = req_id
        return await human_auth.login_customer(
            customer_id=payload.customer_id,
            pin=payload.pin,
            request_id=req_id,
        )

    @router.post("/v1/ops/login", response_model=HumanSessionResult)
    async def ops_login(
        payload: OpsLoginRequest,
        request: Request,
        response: Response,
    ) -> HumanSessionResult:
        req_id = _request_id_for(request)
        response.headers["X-Request-ID"] = req_id
        return await human_auth.login_ops(
            operator_id=payload.operator_id,
            password=payload.password,
            request_id=req_id,
        )

    @router.post("/v1/agents/register", response_model=AgentPublicIdentity, status_code=201)
    async def register_agent(
        payload: RegisterAgentRequest,
        request: Request,
        response: Response,
        authorization: str | None = Header(default=None),
    ) -> AgentPublicIdentity:
        req_id = _request_id_for(request)
        response.headers["X-Request-ID"] = req_id
        token = _extract_bearer_token(authorization)
        await human_auth.authenticate(token, HumanRole.REGISTRAR)
        return await registry.register(
            name=payload.name,
            vendor=payload.vendor,
            public_jwk=payload.public_jwk,
            max_scopes=payload.max_scopes,
            request_id=req_id,
        )

    @router.get("/v1/ops/agents", response_model=Page)
    async def list_agents(
        request: Request,
        response: Response,
        authorization: str | None = Header(default=None),
        cursor: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> Page:
        req_id = _request_id_for(request)
        response.headers["X-Request-ID"] = req_id
        token = _extract_bearer_token(authorization)
        await human_auth.authenticate(token, HumanRole.OPS)
        return await registry.list_public_page(cursor=cursor, limit=limit)

    @router.post("/v1/ops/agents/{id}/suspend", response_model=AgentPublicIdentity)
    async def suspend_agent(
        id: str,
        request: Request,
        response: Response,
        payload: SuspendAgentRequest | None = None,
        authorization: str | None = Header(default=None),
    ) -> AgentPublicIdentity:
        req_id = _request_id_for(request)
        response.headers["X-Request-ID"] = req_id
        token = _extract_bearer_token(authorization)
        await human_auth.authenticate(token, HumanRole.OPS)
        if lifecycle is None:
            raise IdentityError(
                ReasonCode.E_SERVICE_UNAVAILABLE,
                "Lifecycle coordinator is unavailable.",
                http_status=503,
            )
        # Ensure agent exists (returns 404 before invoking lifecycle).
        await registry.get(id)
        reason_codes = (payload.reason,) if payload and payload.reason else ("OPS_SUSPENDED",)
        await lifecycle.suspend_agent(id, reason_codes)
        return await registry.get(id)

    @router.post("/v1/ops/agents/{id}/reinstate", response_model=AgentPublicIdentity)
    async def reinstate_agent(
        id: str,
        request: Request,
        response: Response,
        authorization: str | None = Header(default=None),
    ) -> AgentPublicIdentity:
        req_id = _request_id_for(request)
        response.headers["X-Request-ID"] = req_id
        token = _extract_bearer_token(authorization)
        await human_auth.authenticate(token, HumanRole.OPS)
        if lifecycle is None:
            raise IdentityError(
                ReasonCode.E_SERVICE_UNAVAILABLE,
                "Lifecycle coordinator is unavailable.",
                http_status=503,
            )
        await registry.get(id)
        await lifecycle.reinstate_agent(id)
        return await registry.get(id)

    return router


__all__ = [
    "CustomerLoginRequest",
    "OpsLoginRequest",
    "RegisterAgentRequest",
    "SuspendAgentRequest",
    "TokenIssueRequest",
    "build_identity_router",
    "install_identity_error_handlers",
]
