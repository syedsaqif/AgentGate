"""Member B fresh-proof, immutable-body, same-idempotency-key retry execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from contracts.common import Clock
from contracts.reason_codes import ReasonCode

from .keys import AgentKeyPair
from .proofs import build_dpop_proof, normalize_gateway_origin
from .serialization import SerializedRequestBody
from .tokens import TokenManager, parse_api_error

DEFAULT_RETRYABLE_STATUSES: frozenset[int] = frozenset({408, 429, 502, 503, 504})

type SleepFn = Callable[[float], Awaitable[None]]


class _SystemUtcClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True)
class RetryPolicy:
    """Retry configuration ensuring fresh proof `jti` and immutable body/idempotency key."""

    max_attempts: int = 3
    retryable_statuses: frozenset[int] = DEFAULT_RETRYABLE_STATUSES
    base_delay_seconds: float = 0.2
    allow_token_refresh_on_expired: bool = True

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_delay_seconds < 0:
            raise ValueError("base_delay_seconds must be >= 0")


def _retry_delay_for(response: httpx.Response, policy: RetryPolicy, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        try:
            parsed = float(retry_after)
            if parsed >= 0:
                return min(parsed, 30.0)
        except ValueError:
            pass
    return float(policy.base_delay_seconds * (2 ** (attempt - 1)))


async def execute_with_fresh_proof_retry(
    *,
    http_client: httpx.AsyncClient,
    token_manager: TokenManager,
    keypair: AgentKeyPair,
    gateway_origin: str,
    method: str,
    path: str,
    serialized_body: SerializedRequestBody,
    params: Mapping[str, Any] | None = None,
    idempotency_key: str | None = None,
    extra_headers: Mapping[str, str] | None = None,
    retry_policy: RetryPolicy | None = None,
    clock: Clock | None = None,
    sleep_fn: SleepFn = asyncio.sleep,
) -> httpx.Response:
    """Execute a signed resource request with fresh-proof retry semantics.

    Guarantees:
    - Exact `serialized_body.raw_bytes` is transmitted unchanged on every attempt.
    - `Idempotency-Key` is preserved verbatim on every attempt.
    - Every attempt generates a fresh Ed25519 DPoP proof with a brand-new unique `jti`.
    - On `401 E_AUTH_EXPIRED`, attempts at most one token refresh via `TokenManager`
      (which still enforces live mandate/agent authority on the gateway).
    - Never retries `403`, `400`, `404`, `409`, `422`, or non-expired `401` errors.
    """

    policy = retry_policy or RetryPolicy()
    active_clock: Clock = clock or _SystemUtcClock()
    origin = normalize_gateway_origin(gateway_origin)
    clean_method = method.strip().upper()
    target_path = path if path.startswith("/") else f"/{path}"
    full_url = f"{origin}{target_path}"

    refreshed_on_expired = False
    force_refresh = False
    attempt = 0

    while True:
        attempt += 1
        token = await token_manager.get_valid_token(http_client, force_refresh=force_refresh)
        force_refresh = False

        now = active_clock.now().astimezone(UTC)
        proof = build_dpop_proof(
            keypair=keypair,
            method=clean_method,
            gateway_origin=origin,
            path_or_url=target_path,
            body_bytes=serialized_body.raw_bytes,
            access_token=token.access_token,
            now=now,
        )

        headers: dict[str, str] = {
            "Authorization": f"DPoP {token.access_token}",
            "DPoP": proof.jwt,
        }
        if serialized_body.content_type is not None:
            headers["Content-Type"] = serialized_body.content_type
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        if extra_headers:
            for k, v in extra_headers.items():
                if k.lower() not in ("authorization", "dpop", "idempotency-key", "content-type"):
                    headers[k] = v

        try:
            response = await http_client.request(
                clean_method,
                full_url,
                content=serialized_body.raw_bytes if serialized_body.raw_bytes else None,
                params=params,
                headers=headers,
            )
        except httpx.TransportError:
            if attempt >= policy.max_attempts:
                raise
            delay = policy.base_delay_seconds * (2 ** (attempt - 1))
            if delay > 0:
                await sleep_fn(delay)
            continue

        if 200 <= response.status_code < 300:
            return response

        api_err = parse_api_error(response)

        # Refresh once ONLY on E_AUTH_EXPIRED (never on revoked/suspended/mismatch/replay).
        # Do not consume a transport-retry attempt on the single allowed token refresh.
        if (
            response.status_code == 401
            and api_err.error_code == ReasonCode.E_AUTH_EXPIRED.value
            and policy.allow_token_refresh_on_expired
            and not refreshed_on_expired
        ):
            refreshed_on_expired = True
            token_manager.invalidate()
            force_refresh = True
            attempt -= 1
            continue

        if response.status_code in policy.retryable_statuses and attempt < policy.max_attempts:
            delay = _retry_delay_for(response, policy, attempt)
            if delay > 0:
                await sleep_fn(delay)
            continue

        raise api_err


__all__ = [
    "DEFAULT_RETRYABLE_STATUSES",
    "RetryPolicy",
    "SleepFn",
    "execute_with_fresh_proof_retry",
]
