"""Member B identity domain exceptions mapped to frozen ReasonCode values."""

from __future__ import annotations

from contracts.reason_codes import HTTP_STATUS_BY_CODE, ReasonCode


class IdentityError(Exception):
    """Base error for Member B identity and human-authentication failures."""

    def __init__(
        self,
        code: ReasonCode,
        message: str,
        *,
        http_status: int | None = None,
        internal_reason: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status if http_status is not None else HTTP_STATUS_BY_CODE[code]
        self.internal_reason = internal_reason or code.value
        self.retry_after_seconds = retry_after_seconds


class AuthenticationError(IdentityError):
    """401 authentication failure (missing, invalid, expired, or revoked credentials)."""


class AuthorizationError(IdentityError):
    """403 authenticated-caller authorization failure (suspended, wrong PIN, forbidden state)."""


class ResourceNotFoundError(IdentityError):
    """404 unknown or unowned resource without leaking cross-customer existence."""

    def __init__(
        self,
        message: str = "Requested resource was not found.",
        *,
        internal_reason: str = "resource_not_found",
    ) -> None:
        super().__init__(
            ReasonCode.E_REQ_INVALID,
            message,
            http_status=404,
            internal_reason=internal_reason,
        )


class ValidationDomainError(IdentityError):
    """400 invalid domain input (malformed JWK, duplicate key, unsupported scope)."""

    def __init__(
        self,
        message: str,
        *,
        internal_reason: str = "validation_failed",
    ) -> None:
        super().__init__(
            ReasonCode.E_REQ_INVALID,
            message,
            http_status=400,
            internal_reason=internal_reason,
        )


class DuplicateAgentKeyError(ValidationDomainError):
    """Raised when registering an agent with an already-registered key thumbprint or ID."""

    def __init__(
        self,
        message: str = "Agent key thumbprint or identifier is already registered.",
        *,
        internal_reason: str = "duplicate_agent_key",
    ) -> None:
        super().__init__(message, internal_reason=internal_reason)


class PinVerificationError(AuthorizationError):
    """403 customer PIN verification failed."""

    def __init__(
        self,
        message: str = "Invalid customer PIN.",
        *,
        internal_reason: str = "bad_pin",
    ) -> None:
        super().__init__(
            ReasonCode.E_ESC_BAD_PIN,
            message,
            http_status=403,
            internal_reason=internal_reason,
        )


class RateLimitedError(IdentityError):
    """429 login or PIN failure throttle triggered."""

    def __init__(
        self,
        retry_after_seconds: int,
        message: str = "Too many failed authentication attempts. Retry later.",
        *,
        internal_reason: str = "auth_rate_limited",
    ) -> None:
        super().__init__(
            ReasonCode.E_REQ_RATE_LIMITED,
            message,
            http_status=429,
            internal_reason=internal_reason,
            retry_after_seconds=max(1, retry_after_seconds),
        )


__all__ = [
    "AuthenticationError",
    "AuthorizationError",
    "DuplicateAgentKeyError",
    "IdentityError",
    "PinVerificationError",
    "RateLimitedError",
    "ResourceNotFoundError",
    "ValidationDomainError",
]
