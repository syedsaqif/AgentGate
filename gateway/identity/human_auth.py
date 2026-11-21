"""Member B customer/ops/registrar session authentication, Argon2 PIN checks, and throttling."""

from __future__ import annotations

import hmac
import math
import secrets
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from contracts.audit_models import AuditEvent
from contracts.audit_ports import AuditLog
from contracts.common import Clock, ContractModel, HumanRole, OpaqueId, Tx
from contracts.identity_models import HumanContext
from contracts.reason_codes import ReasonCode

from .crypto import (
    format_utc_timestamp,
    generate_ed25519_keypair,
    public_key_thumbprint,
    sha256_hex,
)
from .errors import (
    AuthenticationError,
    PinVerificationError,
    RateLimitedError,
    ResourceNotFoundError,
    ValidationDomainError,
)
from .models import (
    CustomerRecord,
    HumanSessionRecord,
    OperatorRecord,
    SessionFactory,
    maybe_await,
    session_scope,
)

HUMAN_SESSION_ISSUER = "agentgate"
HUMAN_SESSION_TYP = "agentgate-human-session+jwt"

ROLE_AUDIENCES: dict[HumanRole, str] = {
    HumanRole.CUSTOMER: "agentgate-customer-session",
    HumanRole.OPS: "agentgate-ops-session",
    HumanRole.REGISTRAR: "agentgate-registrar-session",
}


class HumanSessionResult(ContractModel):
    """Returned to the caller upon successful human login; never audited or logged."""

    session_token: str
    session_id: OpaqueId
    subject_id: OpaqueId
    role: HumanRole
    expires_at: datetime


@dataclass(frozen=True)
class HumanSessionKeyring:
    """Domain-separated Ed25519 signing keys for customer, ops, and registrar sessions."""

    customer_key: Ed25519PrivateKey
    ops_key: Ed25519PrivateKey
    registrar_key: Ed25519PrivateKey

    def __post_init__(self) -> None:
        thumbprints = {
            public_key_thumbprint(self.customer_key.public_key()),
            public_key_thumbprint(self.ops_key.public_key()),
            public_key_thumbprint(self.registrar_key.public_key()),
        }
        if len(thumbprints) != 3:
            raise ValueError(
                "HumanSessionKeyring requires distinct Ed25519 signing keys per human role"
            )

    @classmethod
    def generate(cls) -> HumanSessionKeyring:
        c_priv, _ = generate_ed25519_keypair()
        o_priv, _ = generate_ed25519_keypair()
        r_priv, _ = generate_ed25519_keypair()
        return cls(customer_key=c_priv, ops_key=o_priv, registrar_key=r_priv)

    def private_key_for(self, role: HumanRole) -> Ed25519PrivateKey:
        if role is HumanRole.CUSTOMER:
            return self.customer_key
        if role is HumanRole.OPS:
            return self.ops_key
        if role is HumanRole.REGISTRAR:
            return self.registrar_key
        raise ValidationDomainError(f"unsupported human role: {role!r}")

    def public_key_for(self, role: HumanRole) -> Ed25519PublicKey:
        return self.private_key_for(role).public_key()


class AuthThrottler:
    """Deterministic clock-driven failure throttler for logins and customer PIN checks."""

    def __init__(
        self,
        clock: Clock,
        *,
        max_failures: int = 5,
        window_seconds: int = 300,
        lockout_seconds: int = 300,
    ) -> None:
        if max_failures < 1 or window_seconds < 1 or lockout_seconds < 1:
            raise ValueError("throttle thresholds must be positive integers")
        self._clock = clock
        self._max_failures = max_failures
        self._window = timedelta(seconds=window_seconds)
        self._lockout = timedelta(seconds=lockout_seconds)
        self._failures: dict[tuple[str, str], list[datetime]] = defaultdict(list)
        self._locked_until: dict[tuple[str, str], datetime] = {}

    def check(self, scope: str, key: str) -> None:
        now = self._clock.now()
        bucket = (scope, key)
        locked_until = self._locked_until.get(bucket)
        if locked_until is not None:
            if now < locked_until:
                remaining = math.ceil((locked_until - now).total_seconds())
                raise RateLimitedError(
                    retry_after_seconds=max(1, remaining),
                    internal_reason=f"{scope}_rate_limited",
                )
            self._locked_until.pop(bucket, None)
            self._failures.pop(bucket, None)

    def record_failure(self, scope: str, key: str) -> None:
        now = self._clock.now()
        bucket = (scope, key)
        cutoff = now - self._window
        recent = [ts for ts in self._failures[bucket] if ts > cutoff]
        recent.append(now)
        self._failures[bucket] = recent
        if len(recent) >= self._max_failures:
            self._locked_until[bucket] = now + self._lockout

    def record_success(self, scope: str, key: str) -> None:
        bucket = (scope, key)
        self._failures.pop(bucket, None)
        self._locked_until.pop(bucket, None)


def assert_customer_owns(human: HumanContext, resource_customer_id: OpaqueId) -> None:
    """Enforce strict customer object ownership; returns 404 to prevent IDOR enumeration."""

    if human.role != HumanRole.CUSTOMER or human.subject_id != resource_customer_id:
        raise ResourceNotFoundError(
            "Requested resource was not found.",
            internal_reason="customer_ownership_mismatch",
        )


def _parse_utc_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.astimezone(UTC)


def _default_password_hasher() -> PasswordHasher:
    # Real Argon2id parameters aligned with OWASP minimum guidance.
    return PasswordHasher(
        time_cost=2,
        memory_cost=19456,
        parallelism=1,
        hash_len=32,
        salt_len=16,
        type=Type.ID,
    )


class SqlHumanAuthService:
    """SQLAlchemy-backed human session and Argon2 PIN service conforming to `HumanAuth`."""

    def __init__(
        self,
        session_factory: SessionFactory,
        clock: Clock,
        keyring: HumanSessionKeyring,
        *,
        audit_log: AuditLog | None = None,
        throttler: AuthThrottler | None = None,
        password_hasher: PasswordHasher | None = None,
        customer_session_ttl_seconds: int = 3600,
        ops_session_ttl_seconds: int = 1800,
        registrar_session_ttl_seconds: int = 900,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._keyring = keyring
        self._audit_log = audit_log
        self._throttler = throttler or AuthThrottler(clock)
        self._hasher = password_hasher or _default_password_hasher()
        self._dummy_hash = self._hasher.hash("agentgate-constant-time-dummy-secret")
        self._ttls: dict[HumanRole, int] = {
            HumanRole.CUSTOMER: customer_session_ttl_seconds,
            HumanRole.OPS: ops_session_ttl_seconds,
            HumanRole.REGISTRAR: registrar_session_ttl_seconds,
        }

    async def _audit_safe(
        self,
        *,
        event_type: str,
        request_id: str | None = None,
        customer_id: str | None = None,
        reason_codes: tuple[str, ...] = (),
        safe_payload: dict[str, Any] | None = None,
    ) -> None:
        if self._audit_log is None:
            return
        await self._audit_log.append(
            AuditEvent(
                event_type=event_type,
                request_id=request_id,
                customer_id=customer_id,
                reason_codes=reason_codes,
                safe_payload=safe_payload or {},
                occurred_at=self._clock.now(),
            )
        )

    def _verify_argon2(self, stored_hash: str, candidate: str) -> bool:
        if not isinstance(candidate, str) or not candidate:
            return False
        try:
            return bool(self._hasher.verify(stored_hash, candidate))
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    async def seed_customer(
        self,
        *,
        customer_id: OpaqueId,
        display_name: str,
        pin: str,
        status: str = "ACTIVE",
        tx: Tx | None = None,
    ) -> None:
        """Create or seed a customer record with an Argon2id PIN hash (never stores raw PIN)."""

        if not customer_id or not pin:
            raise ValidationDomainError("customer_id and pin must be non-empty")
        pin_hash = self._hasher.hash(pin)
        record = CustomerRecord(
            customer_id=customer_id,
            display_name=display_name,
            pin_hash=pin_hash,
            status=status,
            created_at=format_utc_timestamp(self._clock.now()),
        )
        async with session_scope(self._session_factory, tx, write=True) as session:
            session.add(record)
            await maybe_await(session.flush())

    async def seed_operator(
        self,
        *,
        operator_id: OpaqueId,
        display_name: str,
        role: HumanRole,
        password: str,
        status: str = "ACTIVE",
        tx: Tx | None = None,
    ) -> None:
        """Create or seed an ops or registrar account with an Argon2id secret hash."""

        if role not in (HumanRole.OPS, HumanRole.REGISTRAR):
            raise ValidationDomainError("operator role must be 'ops' or 'registrar'")
        if not operator_id or not password:
            raise ValidationDomainError("operator_id and password must be non-empty")
        secret_hash = self._hasher.hash(password)
        record = OperatorRecord(
            operator_id=operator_id,
            display_name=display_name,
            role=role.value,
            secret_hash=secret_hash,
            status=status,
            created_at=format_utc_timestamp(self._clock.now()),
        )
        async with session_scope(self._session_factory, tx, write=True) as session:
            session.add(record)
            await maybe_await(session.flush())

    async def _issue_session(
        self,
        *,
        subject_id: str,
        role: HumanRole,
        request_id: str | None = None,
    ) -> HumanSessionResult:
        now = self._clock.now()
        expires_at = now + timedelta(seconds=self._ttls[role])
        session_id = f"ses_{secrets.token_hex(12)}"

        claims = {
            "iss": HUMAN_SESSION_ISSUER,
            "aud": ROLE_AUDIENCES[role],
            "sub": subject_id,
            "jti": session_id,
            "role": role.value,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
        }
        token = jwt.encode(
            claims,
            self._keyring.private_key_for(role),
            algorithm="EdDSA",
            headers={"typ": HUMAN_SESSION_TYP},
        )
        token_hash = sha256_hex(token.encode("ascii"))

        record = HumanSessionRecord(
            session_id=session_id,
            subject_id=subject_id,
            role=role.value,
            token_hash=token_hash,
            issued_at=format_utc_timestamp(now),
            expires_at=format_utc_timestamp(expires_at),
            revoked_at=None,
        )
        async with session_scope(self._session_factory, write=True) as session:
            session.add(record)
            await maybe_await(session.flush())

        await self._audit_safe(
            event_type="human.login",
            request_id=request_id,
            customer_id=subject_id if role is HumanRole.CUSTOMER else None,
            safe_payload={
                "session_id": session_id,
                "subject_id": subject_id,
                "role": role.value,
                "expires_at": format_utc_timestamp(expires_at),
            },
        )
        return HumanSessionResult(
            session_token=token,
            session_id=session_id,
            subject_id=subject_id,
            role=role,
            expires_at=expires_at,
        )

    async def login_customer(
        self,
        *,
        customer_id: OpaqueId,
        pin: str,
        request_id: str | None = None,
    ) -> HumanSessionResult:
        """Authenticate a seeded customer with Argon2id PIN verification and throttling."""

        throttle_key = customer_id.strip() if isinstance(customer_id, str) else ""
        try:
            self._throttler.check("customer_login", throttle_key)
        except RateLimitedError:
            await self._audit_safe(
                event_type="auth.failed",
                request_id=request_id,
                reason_codes=(ReasonCode.E_REQ_RATE_LIMITED.value,),
                safe_payload={"domain": "customer_login", "reason": "rate_limited"},
            )
            raise

        async with session_scope(self._session_factory, write=False) as session:
            record = await maybe_await(session.get(CustomerRecord, throttle_key))

        if record is None or record.status != "ACTIVE":
            # Constant-time work to prevent customer-existence timing side channel.
            self._verify_argon2(self._dummy_hash, pin)
            self._throttler.record_failure("customer_login", throttle_key)
            await self._audit_safe(
                event_type="auth.failed",
                request_id=request_id,
                reason_codes=(ReasonCode.E_AUTH_INVALID_TOKEN.value,),
                safe_payload={"domain": "customer_login", "reason": "invalid_credentials"},
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_INVALID_TOKEN,
                "Invalid customer credentials.",
                internal_reason="customer_unknown_or_inactive",
            )

        if not self._verify_argon2(record.pin_hash, pin):
            self._throttler.record_failure("customer_login", throttle_key)
            await self._audit_safe(
                event_type="auth.failed",
                request_id=request_id,
                customer_id=record.customer_id,
                reason_codes=(ReasonCode.E_AUTH_INVALID_TOKEN.value,),
                safe_payload={"domain": "customer_login", "reason": "bad_pin"},
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_INVALID_TOKEN,
                "Invalid customer credentials.",
                internal_reason="customer_bad_pin",
            )

        self._throttler.record_success("customer_login", throttle_key)
        return await self._issue_session(
            subject_id=record.customer_id,
            role=HumanRole.CUSTOMER,
            request_id=request_id,
        )

    async def login_ops(
        self,
        *,
        operator_id: OpaqueId,
        password: str,
        request_id: str | None = None,
    ) -> HumanSessionResult:
        """Authenticate a seeded ops or registrar operator into its role-isolated session domain."""

        throttle_key = operator_id.strip() if isinstance(operator_id, str) else ""
        try:
            self._throttler.check("ops_login", throttle_key)
        except RateLimitedError:
            await self._audit_safe(
                event_type="auth.failed",
                request_id=request_id,
                reason_codes=(ReasonCode.E_REQ_RATE_LIMITED.value,),
                safe_payload={"domain": "ops_login", "reason": "rate_limited"},
            )
            raise

        async with session_scope(self._session_factory, write=False) as session:
            record = await maybe_await(session.get(OperatorRecord, throttle_key))

        if record is None or record.status != "ACTIVE":
            self._verify_argon2(self._dummy_hash, password)
            self._throttler.record_failure("ops_login", throttle_key)
            await self._audit_safe(
                event_type="auth.failed",
                request_id=request_id,
                reason_codes=(ReasonCode.E_AUTH_INVALID_TOKEN.value,),
                safe_payload={"domain": "ops_login", "reason": "invalid_credentials"},
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_INVALID_TOKEN,
                "Invalid operator credentials.",
                internal_reason="operator_unknown_or_inactive",
            )

        if not self._verify_argon2(record.secret_hash, password):
            self._throttler.record_failure("ops_login", throttle_key)
            await self._audit_safe(
                event_type="auth.failed",
                request_id=request_id,
                reason_codes=(ReasonCode.E_AUTH_INVALID_TOKEN.value,),
                safe_payload={"domain": "ops_login", "reason": "bad_password"},
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_INVALID_TOKEN,
                "Invalid operator credentials.",
                internal_reason="operator_bad_password",
            )

        self._throttler.record_success("ops_login", throttle_key)
        role = HumanRole(record.role)
        return await self._issue_session(
            subject_id=record.operator_id,
            role=role,
            request_id=request_id,
        )

    async def authenticate(
        self,
        session_token: str,
        required_role: HumanRole,
    ) -> HumanContext:
        """Verify a human session token strictly within `required_role`'s key/audience domain."""

        if not isinstance(session_token, str) or not session_token.strip():
            raise AuthenticationError(
                ReasonCode.E_AUTH_MISSING,
                "Authentication session token is required.",
                internal_reason="missing_human_session_token",
            )

        token = session_token.strip()
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            await self._audit_safe(
                event_type="auth.failed",
                reason_codes=(ReasonCode.E_AUTH_INVALID_TOKEN.value,),
                safe_payload={"domain": required_role.value, "reason": "malformed_session_header"},
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_INVALID_TOKEN,
                "Invalid authentication session.",
                internal_reason="malformed_session_header",
            ) from exc

        if header.get("alg") != "EdDSA" or header.get("typ") != HUMAN_SESSION_TYP:
            await self._audit_safe(
                event_type="auth.failed",
                reason_codes=(ReasonCode.E_AUTH_INVALID_TOKEN.value,),
                safe_payload={
                    "domain": required_role.value,
                    "reason": "invalid_session_alg_or_typ",
                },
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_INVALID_TOKEN,
                "Invalid authentication session.",
                internal_reason="invalid_session_alg_or_typ",
            )

        expected_aud = ROLE_AUDIENCES[required_role]
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                self._keyring.public_key_for(required_role),
                algorithms=["EdDSA"],
                issuer=HUMAN_SESSION_ISSUER,
                audience=expected_aud,
                options={
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                    "require": ["iss", "aud", "sub", "jti", "role", "iat", "exp"],
                },
            )
        except jwt.InvalidTokenError as exc:
            await self._audit_safe(
                event_type="auth.failed",
                reason_codes=(ReasonCode.E_AUTH_INVALID_TOKEN.value,),
                safe_payload={
                    "domain": required_role.value,
                    "reason": "invalid_session_signature_or_aud",
                },
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_INVALID_TOKEN,
                "Invalid authentication session.",
                internal_reason="invalid_session_signature_or_aud",
            ) from exc

        if claims.get("role") != required_role.value:
            raise AuthenticationError(
                ReasonCode.E_AUTH_INVALID_TOKEN,
                "Invalid authentication session.",
                internal_reason="session_role_mismatch",
            )

        now = self._clock.now()
        exp_ts = claims.get("exp")
        if not isinstance(exp_ts, int) or isinstance(exp_ts, bool):
            raise AuthenticationError(
                ReasonCode.E_AUTH_INVALID_TOKEN,
                "Invalid authentication session.",
                internal_reason="invalid_session_exp",
            )

        session_id = str(claims["jti"])
        subject_id = str(claims["sub"])

        async with session_scope(self._session_factory, write=False) as session:
            record = await maybe_await(session.get(HumanSessionRecord, session_id))

        if record is None or record.role != required_role.value or record.subject_id != subject_id:
            raise AuthenticationError(
                ReasonCode.E_AUTH_INVALID_TOKEN,
                "Invalid authentication session.",
                internal_reason="session_record_mismatch",
            )

        try:
            presented_hash = sha256_hex(token.encode("ascii"))
        except UnicodeEncodeError as exc:
            raise AuthenticationError(
                ReasonCode.E_AUTH_INVALID_TOKEN,
                "Invalid authentication session.",
                internal_reason="non_ascii_session_token",
            ) from exc

        if not hmac.compare_digest(record.token_hash, presented_hash):
            raise AuthenticationError(
                ReasonCode.E_AUTH_INVALID_TOKEN,
                "Invalid authentication session.",
                internal_reason="session_token_hash_mismatch",
            )

        if record.revoked_at is not None:
            await self._audit_safe(
                event_type="auth.failed",
                customer_id=subject_id if required_role is HumanRole.CUSTOMER else None,
                reason_codes=(ReasonCode.E_AUTH_TOKEN_REVOKED.value,),
                safe_payload={"domain": required_role.value, "reason": "session_revoked"},
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_TOKEN_REVOKED,
                "Authentication session has been revoked.",
                internal_reason="session_revoked",
            )

        record_expires_at = _parse_utc_iso(record.expires_at)
        if now >= record_expires_at or int(now.timestamp()) >= exp_ts:
            await self._audit_safe(
                event_type="auth.failed",
                customer_id=subject_id if required_role is HumanRole.CUSTOMER else None,
                reason_codes=(ReasonCode.E_AUTH_EXPIRED.value,),
                safe_payload={"domain": required_role.value, "reason": "session_expired"},
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_EXPIRED,
                "Authentication session has expired.",
                internal_reason="session_expired",
            )

        return HumanContext(
            session_id=record.session_id,
            subject_id=record.subject_id,
            role=required_role,
            expires_at=record_expires_at,
        )

    async def revoke_session(self, session_id: OpaqueId, tx: Tx | None = None) -> None:
        """Revoke an existing human session immediately."""

        now_iso = format_utc_timestamp(self._clock.now())
        async with session_scope(self._session_factory, tx, write=True) as session:
            record = await maybe_await(session.get(HumanSessionRecord, session_id))
            if record is not None and record.revoked_at is None:
                record.revoked_at = now_iso
                await maybe_await(session.flush())

    async def verify_pin(self, customer_id: OpaqueId, pin: str) -> None:
        """Verify a customer's PIN using Argon2id with failure throttling and safe auditing."""

        throttle_key = customer_id.strip() if isinstance(customer_id, str) else ""
        try:
            self._throttler.check("pin", throttle_key)
        except RateLimitedError:
            await self._audit_safe(
                event_type="auth.failed",
                customer_id=throttle_key or None,
                reason_codes=(ReasonCode.E_REQ_RATE_LIMITED.value,),
                safe_payload={"domain": "pin_verify", "reason": "pin_rate_limited"},
            )
            raise

        async with session_scope(self._session_factory, write=False) as session:
            record = await maybe_await(session.get(CustomerRecord, throttle_key))

        if record is None or record.status != "ACTIVE":
            self._verify_argon2(self._dummy_hash, pin)
            self._throttler.record_failure("pin", throttle_key)
            await self._audit_safe(
                event_type="auth.failed",
                reason_codes=(ReasonCode.E_ESC_BAD_PIN.value,),
                safe_payload={"domain": "pin_verify", "reason": "customer_unknown_or_inactive"},
            )
            raise PinVerificationError(internal_reason="customer_unknown_or_inactive")

        if not self._verify_argon2(record.pin_hash, pin):
            self._throttler.record_failure("pin", throttle_key)
            await self._audit_safe(
                event_type="auth.failed",
                customer_id=record.customer_id,
                reason_codes=(ReasonCode.E_ESC_BAD_PIN.value,),
                safe_payload={"domain": "pin_verify", "reason": "bad_pin"},
            )
            raise PinVerificationError(internal_reason="bad_pin")

        self._throttler.record_success("pin", throttle_key)


__all__ = [
    "HUMAN_SESSION_ISSUER",
    "HUMAN_SESSION_TYP",
    "ROLE_AUDIENCES",
    "AuthThrottler",
    "HumanSessionKeyring",
    "HumanSessionResult",
    "SqlHumanAuthService",
    "assert_customer_owns",
]
