"""Customer session management for the demo backend.

Server-side sessions securely bind customer_id to account_id and store the underlying
gateway token in memory. Client browsers receive only a disposable session token.
PINs are strictly forbidden from session storage.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status


@dataclass(frozen=True)
class DemoSession:
    """Server-side customer demo session."""

    session_id: str
    customer_id: str
    account_id: str
    gateway_token: str
    created_at: float
    expires_at: float

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at


class SessionStore:
    """Thread-safe in-memory session repository."""

    def __init__(self) -> None:
        self._sessions: dict[str, DemoSession] = {}

    def create_session(
        self,
        customer_id: str,
        account_id: str,
        gateway_token: str,
        ttl_seconds: int = 3600,
    ) -> DemoSession:
        """Create and store a new customer session."""
        session_id = f"demo_sess_{secrets.token_urlsafe(24)}"
        now = time.time()
        session = DemoSession(
            session_id=session_id,
            customer_id=customer_id,
            account_id=account_id,
            gateway_token=gateway_token,
            created_at=now,
            expires_at=now + ttl_seconds,
        )
        self._sessions[session_id] = session
        return session

    def get_session(self, session_id: str) -> DemoSession | None:
        """Retrieve active session by id; purges and returns None if expired."""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if session.is_expired:
            self._sessions.pop(session_id, None)
            return None
        return session

    def revoke_session(self, session_id: str) -> None:
        """Revoke a session explicitly."""
        self._sessions.pop(session_id, None)

    def clear(self) -> None:
        """Clear all active sessions (e.g. during reset)."""
        self._sessions.clear()


# Default global store for the demo server application
global_session_store = SessionStore()


def extract_session_token(request: Request) -> str:
    """Extract demo session token from Authorization, X-Session-Token header, or cookie."""
    auth_header = request.headers.get("Authorization")
    if auth_header:
        parts = auth_header.strip().split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        if len(parts) == 1 and not parts[0].lower().startswith("bearer"):
            return parts[0].strip()

    x_session = request.headers.get("X-Session-Token")
    if x_session and x_session.strip():
        return x_session.strip()

    cookie = request.cookies.get("demo_session")
    if cookie and cookie.strip():
        return cookie.strip()

    req_id = getattr(request.state, "request_id", f"req_{secrets.token_hex(6)}")
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "error": {
                "code": "E_AUTH_MISSING",
                "message": "Missing customer session token in Authorization header or cookie",
                "request_id": req_id,
            }
        },
    )


def get_current_demo_session(
    request: Request,
    token: Annotated[str, Depends(extract_session_token)],
) -> DemoSession:
    """FastAPI dependency to authenticate and resolve the active demo customer session."""
    session = global_session_store.get_session(token)
    req_id = getattr(request.state, "request_id", f"req_{secrets.token_hex(6)}")
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "E_AUTH_EXPIRED",
                    "message": "Customer demo session has expired or is invalid",
                    "request_id": req_id,
                }
            },
        )
    return session
