"""Member B SDK token acquisition, caching, and authority-respecting refresh."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from contracts.common import Clock
from contracts.identity_models import AccessTokenResult
from contracts.reason_codes import ReasonCode

from .keys import AgentKeyPair
from .proofs import build_dpop_proof, normalize_gateway_origin
from .serialization import serialize_json_body

TOKEN_ENDPOINT_PATH = "/v1/oauth/token"

_NON_REFRESHABLE_CODES: frozenset[str] = frozenset(
    {
        ReasonCode.E_MANDATE_REVOKED.value,
        ReasonCode.E_AUTH_AGENT_SUSPENDED.value,
        ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH.value,
        ReasonCode.E_AUTH_TOKEN_REVOKED.value,
        ReasonCode.E_AUTH_KEY_MISMATCH.value,
        ReasonCode.E_AUTH_PROOF_INVALID.value,
        ReasonCode.E_AUTH_REPLAY.value,
    }
)


class _SystemUtcClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class AgentSDKError(Exception):
    """Base error raised by the AgentGate Python SDK."""


class AgentAPIError(AgentSDKError):
    """Structured error returned by the AgentGate gateway API."""

    def __init__(
        self,
        *,
        status_code: int,
        error_code: str | None,
        message: str,
        request_id: str | None = None,
        response_body: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"HTTP {status_code} ({error_code or 'UNKNOWN'}): {message}")
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.request_id = request_id
        self.response_body = response_body

    @property
    def is_authority_denied(self) -> bool:
        return self.status_code == 403 or (self.error_code in _NON_REFRESHABLE_CODES)


def parse_api_error(response: httpx.Response) -> AgentAPIError:
    """Parse AgentGate's frozen `{error: {code, message, request_id}}` response safely."""

    error_code: str | None = None
    message = f"Gateway request failed with HTTP {response.status_code}"
    request_id = response.headers.get("X-Request-ID")
    parsed_body: dict[str, Any] | None = None
    try:
        raw = response.json()
        if isinstance(raw, dict):
            parsed_body = raw
            err = raw.get("error")
            if isinstance(err, dict):
                if isinstance(err.get("code"), str):
                    error_code = err["code"]
                if isinstance(err.get("message"), str):
                    message = err["message"]
                if isinstance(err.get("request_id"), str):
                    request_id = err["request_id"]
    except Exception:
        pass
    return AgentAPIError(
        status_code=response.status_code,
        error_code=error_code,
        message=message,
        request_id=request_id,
        response_body=parsed_body,
    )


@dataclass(frozen=True)
class CachedAccessToken:
    """Short-lived DPoP access token cached in memory; raw token excluded from `repr`."""

    access_token: str = field(repr=False)
    token_type: str
    expires_in: int
    acquired_at: datetime
    expires_at: datetime

    def is_expired(self, now: datetime, *, leeway_seconds: int = 15) -> bool:
        effective_leeway = min(leeway_seconds, max(0, self.expires_in // 2))
        return now.astimezone(UTC) >= (self.expires_at - timedelta(seconds=effective_leeway))


class TokenManager:
    """Fetches and refreshes key-bound DPoP access tokens from `POST /v1/oauth/token`.

    Token refresh always queries the gateway with a fresh proof; if the mandate or agent
    has been revoked, suspended, or expired, the gateway rejects the refresh and any cached
    token is cleared immediately.
    """

    def __init__(
        self,
        *,
        gateway_origin: str,
        agent_id: str,
        mandate_id: str,
        keypair: AgentKeyPair,
        clock: Clock | None = None,
        refresh_leeway_seconds: int = 15,
    ) -> None:
        if not agent_id or not mandate_id:
            raise ValueError("agent_id and mandate_id must be non-empty")
        self._gateway_origin = normalize_gateway_origin(gateway_origin)
        self._agent_id = agent_id
        self._mandate_id = mandate_id
        self._keypair = keypair
        self._clock: Clock = clock or _SystemUtcClock()
        self._refresh_leeway_seconds = max(0, refresh_leeway_seconds)
        self._cached: CachedAccessToken | None = None

    @property
    def cached_token(self) -> CachedAccessToken | None:
        return self._cached

    def invalidate(self) -> None:
        """Clear any cached access token."""

        self._cached = None

    async def fetch_token(self, http_client: httpx.AsyncClient) -> CachedAccessToken:
        """Acquire a fresh DPoP-bound access token using a newly signed proof (no `ath`)."""

        serialized = serialize_json_body(
            {"client_id": self._agent_id, "mandate_id": self._mandate_id}
        )
        now = self._clock.now().astimezone(UTC)
        proof = build_dpop_proof(
            keypair=self._keypair,
            method="POST",
            gateway_origin=self._gateway_origin,
            path_or_url=TOKEN_ENDPOINT_PATH,
            body_bytes=serialized.raw_bytes,
            access_token=None,
            now=now,
        )

        response = await http_client.post(
            f"{self._gateway_origin}{TOKEN_ENDPOINT_PATH}",
            content=serialized.raw_bytes,
            headers={
                "Content-Type": "application/json",
                "DPoP": proof.jwt,
            },
        )
        if response.status_code != 200:
            self._cached = None
            raise parse_api_error(response)

        parsed = AccessTokenResult.model_validate(response.json())
        if parsed.token_type != "DPoP" or parsed.expires_in < 1:
            self._cached = None
            raise AgentSDKError("Gateway returned an invalid DPoP token response")

        cached = CachedAccessToken(
            access_token=parsed.access_token,
            token_type=parsed.token_type,
            expires_in=parsed.expires_in,
            acquired_at=now,
            expires_at=now + timedelta(seconds=parsed.expires_in),
        )
        self._cached = cached
        return cached

    async def get_valid_token(
        self,
        http_client: httpx.AsyncClient,
        *,
        force_refresh: bool = False,
    ) -> CachedAccessToken:
        """Return a non-expired cached token or fetch a fresh token from the gateway."""

        now = self._clock.now().astimezone(UTC)
        if (
            not force_refresh
            and self._cached is not None
            and not self._cached.is_expired(now, leeway_seconds=self._refresh_leeway_seconds)
        ):
            return self._cached
        return await self.fetch_token(http_client)


__all__ = [
    "TOKEN_ENDPOINT_PATH",
    "AgentAPIError",
    "AgentSDKError",
    "CachedAccessToken",
    "TokenManager",
    "parse_api_error",
]
