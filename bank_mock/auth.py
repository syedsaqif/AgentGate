"""Member A mock bank service credential authentication."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from contracts.reason_codes import ReasonCode

API_KEY_HEADER = APIKeyHeader(name="X-Bank-Service-Key", auto_error=False)


def get_expected_service_key(request: Request) -> str:
    expected = getattr(request.app.state, "service_key", None)
    if not expected:
        # Fall back to environment or default placeholder for local dev
        import os
        expected = os.getenv("AGENTGATE_BANK_SERVICE_CREDENTIAL", "local-bank-dev-secret")
    return expected


async def verify_bank_service_credential(
    request: Request,
    api_key: Annotated[str | None, Security(API_KEY_HEADER)] = None,
) -> str:
    """Validate server-to-server bank credential header."""
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail={
                "code": ReasonCode.E_AUTH_MISSING.value,
                "message": "Missing X-Bank-Service-Key header",
            },
        )

    expected = get_expected_service_key(request)
    if not hmac.compare_digest(api_key, expected):
        raise HTTPException(
            status_code=403,
            detail={
                "code": ReasonCode.E_AUTH_KEY_MISMATCH.value,
                "message": "Invalid bank service credential",
            },
        )

    return api_key
