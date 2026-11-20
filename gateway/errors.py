"""Member A error normalization and X-Request-ID middleware."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from contracts.common import ErrorDetail, ErrorEnvelope
from contracts.reason_codes import HTTP_STATUS_BY_CODE, ReasonCode

logger = logging.getLogger("gateway.errors")


class AgentGateError(Exception):
    """Base domain exception carrying a reason code, human message, and optional status code."""

    def __init__(
        self,
        code: str | ReasonCode,
        message: str,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        else:
            try:
                reason = ReasonCode(self.code)
                self.status_code = HTTP_STATUS_BY_CODE.get(reason, 400)
            except ValueError:
                self.status_code = 400


def get_or_create_request_id(request: Request) -> str:
    """Return the server-generated request ID stored on the request state."""
    request_id = getattr(request.state, "request_id", None)
    if not request_id:
        request_id = f"req_{uuid.uuid4().hex[:16]}"
        request.state.request_id = request_id
    return str(request_id)


def build_error_response(
    status_code: int,
    code: str,
    message: str,
    request_id: str,
) -> JSONResponse:
    """Construct a standardized JSON response conforming to contracts.ErrorEnvelope."""
    envelope = ErrorEnvelope(
        error=ErrorDetail(
            code=code,
            message=message,
            request_id=request_id,
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(mode="json"),
        headers={"X-Request-ID": request_id},
    )


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Middleware ensuring every request has a server-generated X-Request-ID."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Always generate server-side request_id; never trust incoming client header as verified ID
        request_id = f"req_{uuid.uuid4().hex[:16]}"
        request.state.request_id = request_id

        try:
            response = await call_next(request)
        except AgentGateError as exc:
            return build_error_response(
                status_code=exc.status_code,
                code=exc.code,
                message=exc.message,
                request_id=request_id,
            )
        except StarletteHTTPException as exc:
            code = "E_REQ_INVALID" if exc.status_code == 400 else "E_SERVICE_UNAVAILABLE"
            for reason, st in HTTP_STATUS_BY_CODE.items():
                if st == exc.status_code:
                    code = reason.value
                    break
            return build_error_response(
                status_code=exc.status_code,
                code=code,
                message=str(exc.detail),
                request_id=request_id,
            )
        except Exception as exc:
            logger.exception("Unhandled gateway exception on request %s: %s", request_id, exc)
            return build_error_response(
                status_code=500,
                code=ReasonCode.E_SERVICE_UNAVAILABLE.value,
                message="Internal server error",
                request_id=request_id,
            )

        response.headers["X-Request-ID"] = request_id
        return response


def register_error_handlers(app: FastAPI) -> None:
    """Register exception handlers normalizing all errors to contracts.ErrorEnvelope."""

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        request_id = get_or_create_request_id(request)
        first_error = exc.errors()[0] if exc.errors() else {}
        loc = " -> ".join(str(elem) for elem in first_error.get("loc", []))
        msg = first_error.get("msg", "Invalid request body or parameters")
        detail_msg = f"{loc}: {msg}" if loc else msg
        return build_error_response(
            status_code=400,
            code=ReasonCode.E_REQ_INVALID.value,
            message=detail_msg,
            request_id=request_id,
        )

    @app.exception_handler(AgentGateError)
    async def agentgate_exception_handler(
        request: Request, exc: AgentGateError
    ) -> JSONResponse:
        request_id = get_or_create_request_id(request)
        return build_error_response(
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            request_id=request_id,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        request_id = get_or_create_request_id(request)
        code = "E_REQ_INVALID" if exc.status_code == 400 else "E_SERVICE_UNAVAILABLE"
        for reason, st in HTTP_STATUS_BY_CODE.items():
            if st == exc.status_code:
                code = reason.value
                break

        return build_error_response(
            status_code=exc.status_code,
            code=code,
            message=str(exc.detail),
            request_id=request_id,
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        request_id = get_or_create_request_id(request)
        logger.exception("Unhandled gateway exception on request %s: %s", request_id, exc)
        return build_error_response(
            status_code=500,
            code=ReasonCode.E_SERVICE_UNAVAILABLE.value,
            message="Internal server error",
            request_id=request_id,
        )
