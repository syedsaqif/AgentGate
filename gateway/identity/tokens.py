"""Member B short-lived key-bound EdDSA agent tokens, issuance, verification, and revocation."""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Mapping
from datetime import UTC, timedelta
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from sqlalchemy import update

from contracts.audit_models import AuditEvent
from contracts.audit_ports import AuditLog
from contracts.common import Clock, OpaqueId, Tx
from contracts.core_models import MandateSnapshot, MandateStatus
from contracts.core_ports import MandateReader
from contracts.identity_models import AccessTokenResult, AgentStatus, AuthContext
from contracts.reason_codes import ReasonCode

from .crypto import format_utc_timestamp, public_key_thumbprint, sha256_hex
from .errors import AuthenticationError, AuthorizationError, ResourceNotFoundError
from .models import IssuedTokenRecord, SessionFactory, SqlAlchemyTx, maybe_await, session_scope
from .proofs import DPoPProofVerifier, extract_dpop_header

AGENT_TOKEN_ISSUER = "agentgate"
AGENT_TOKEN_AUDIENCE = "agentgate-agent-api"
AGENT_TOKEN_TYP = "at+jwt"
AGENT_TOKEN_ALG = "EdDSA"
MAX_TOKEN_TTL_SECONDS = 600


def extract_dpop_authorization_token(headers: Mapping[str, str]) -> str:
    """Extract raw access token from an `Authorization: DPoP <token>` header."""

    raw_auth: str | None = None
    for key, value in headers.items():
        if key.lower() == "authorization":
            raw_auth = value.strip()
            break

    if not raw_auth:
        raise AuthenticationError(
            ReasonCode.E_AUTH_MISSING,
            "Missing Authorization header.",
            internal_reason="missing_authorization_header",
        )

    parts = raw_auth.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "dpop" or not parts[1].strip():
        raise AuthenticationError(
            ReasonCode.E_AUTH_INVALID_TOKEN,
            "Authorization header must use the DPoP scheme.",
            internal_reason="invalid_authorization_scheme",
        )

    token = parts[1].strip()
    if any(ch.isspace() for ch in token):
        raise AuthenticationError(
            ReasonCode.E_AUTH_INVALID_TOKEN,
            "Malformed access token.",
            internal_reason="malformed_access_token_whitespace",
        )
    return token


class SqlTokenService:
    """Issues and revokes short-lived key-bound EdDSA agent access tokens.

    Conforms to `contracts.identity_ports.TokenService`. Raw access tokens are never stored
    in SQLite or emitted in audit events; only SHA-256 digests and metadata are persisted.
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        clock: Clock,
        signing_key: Ed25519PrivateKey,
        proof_verifier: DPoPProofVerifier,
        mandate_reader: MandateReader,
        audit_log: AuditLog | None = None,
        default_ttl_seconds: int = MAX_TOKEN_TTL_SECONDS,
    ) -> None:
        if default_ttl_seconds < 1 or default_ttl_seconds > MAX_TOKEN_TTL_SECONDS:
            raise ValueError(
                f"default_ttl_seconds must be between 1 and {MAX_TOKEN_TTL_SECONDS}"
            )
        self._session_factory = session_factory
        self._clock = clock
        self._signing_key = signing_key
        self._public_key: Ed25519PublicKey = signing_key.public_key()
        self._key_id = public_key_thumbprint(self._public_key)
        self._proof_verifier = proof_verifier
        self._mandate_reader = mandate_reader
        self._audit_log = audit_log
        self._default_ttl_seconds = default_ttl_seconds

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self._public_key

    @property
    def key_id(self) -> str:
        return self._key_id

    async def _audit_event(
        self,
        *,
        event_type: str,
        request_id: str | None = None,
        customer_id: str | None = None,
        agent_id: str | None = None,
        mandate_id: str | None = None,
        mandate_hash: str | None = None,
        reason_codes: tuple[str, ...] = (),
        safe_payload: dict[str, Any] | None = None,
        tx: Tx | None = None,
    ) -> None:
        if self._audit_log is None:
            return
        await self._audit_log.append(
            AuditEvent(
                event_type=event_type,
                request_id=request_id,
                customer_id=customer_id,
                agent_id=agent_id,
                mandate_id=mandate_id,
                mandate_hash=mandate_hash,
                reason_codes=reason_codes,
                safe_payload=safe_payload or {},
                occurred_at=self._clock.now(),
            ),
            tx=tx,
        )

    async def _read_mandate_safe(self, mandate_id: OpaqueId) -> MandateSnapshot | None:
        try:
            return await self._mandate_reader.get(mandate_id)
        except (KeyError, LookupError, ResourceNotFoundError):
            return None

    async def issue_token(
        self,
        *,
        client_id: OpaqueId,
        mandate_id: OpaqueId,
        proof_jwt: str | None,
        method: str,
        request_url: str,
        request_bytes: bytes,
        request_id: str | None = None,
    ) -> AccessTokenResult:
        """Issue a short-lived EdDSA key-bound token after verifying proof and live authority."""

        # 1. Verify DPoP proof (no ath on token request) and resolve agent strictly from jkt.
        verified_proof = await self._proof_verifier.verify_proof(
            proof_jwt=proof_jwt,
            method=method,
            request_url=request_url,
            request_bytes=request_bytes,
            access_token=None,
            expected_jkt=None,
            request_id=request_id,
        )
        agent = verified_proof.agent

        # 2. Ensure requested client_id matches the cryptographically proven agent identity.
        if client_id != agent.agent_id:
            await self._audit_event(
                event_type="auth.failed",
                request_id=request_id,
                agent_id=agent.agent_id,
                mandate_id=mandate_id,
                reason_codes=(ReasonCode.E_AUTH_KEY_MISMATCH.value,),
                safe_payload={"domain": "token_issue", "reason": "client_id_agent_mismatch"},
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_KEY_MISMATCH,
                "Client identifier does not match the proof key.",
                internal_reason="client_id_agent_mismatch",
            )

        # 3. Check live agent status.
        if agent.status != AgentStatus.ACTIVE:
            await self._audit_event(
                event_type="auth.failed",
                request_id=request_id,
                agent_id=agent.agent_id,
                mandate_id=mandate_id,
                reason_codes=(ReasonCode.E_AUTH_AGENT_SUSPENDED.value,),
                safe_payload={
                    "domain": "token_issue",
                    "reason": f"agent_status_{str(agent.status).lower()}",
                },
            )
            raise AuthorizationError(
                ReasonCode.E_AUTH_AGENT_SUSPENDED,
                "Agent is suspended or disabled.",
                internal_reason=f"agent_status_{str(agent.status).lower()}",
            )

        # 4. Read live mandate and validate binding, status, and validity window.
        mandate = await self._read_mandate_safe(mandate_id)
        if mandate is None:
            await self._audit_event(
                event_type="auth.failed",
                request_id=request_id,
                agent_id=agent.agent_id,
                mandate_id=mandate_id,
                reason_codes=(ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH.value,),
                safe_payload={"domain": "token_issue", "reason": "mandate_not_found"},
            )
            raise AuthorizationError(
                ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH,
                "Mandate is not valid for this agent.",
                internal_reason="mandate_not_found",
            )

        if mandate.terms.agent_id != agent.agent_id:
            await self._audit_event(
                event_type="auth.failed",
                request_id=request_id,
                customer_id=mandate.terms.customer_id,
                agent_id=agent.agent_id,
                mandate_id=mandate.terms.mandate_id,
                mandate_hash=mandate.mandate_hash,
                reason_codes=(ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH.value,),
                safe_payload={"domain": "token_issue", "reason": "agent_mandate_mismatch"},
            )
            raise AuthorizationError(
                ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH,
                "Agent is not bound to the requested mandate.",
                internal_reason="agent_mandate_mismatch",
            )

        if mandate.status in (MandateStatus.REVOKED, MandateStatus.SUSPENDED):
            await self._audit_event(
                event_type="auth.failed",
                request_id=request_id,
                customer_id=mandate.terms.customer_id,
                agent_id=agent.agent_id,
                mandate_id=mandate.terms.mandate_id,
                mandate_hash=mandate.mandate_hash,
                reason_codes=(ReasonCode.E_MANDATE_REVOKED.value,),
                safe_payload={
                    "domain": "token_issue",
                    "reason": f"mandate_status_{str(mandate.status).lower()}",
                },
            )
            raise AuthorizationError(
                ReasonCode.E_MANDATE_REVOKED,
                "Mandate has been revoked or suspended.",
                internal_reason=f"mandate_status_{str(mandate.status).lower()}",
            )

        now = self._clock.now()
        valid_from = mandate.terms.valid_from.astimezone(UTC)
        valid_until = mandate.terms.valid_until.astimezone(UTC)

        if now < valid_from:
            await self._audit_event(
                event_type="auth.failed",
                request_id=request_id,
                customer_id=mandate.terms.customer_id,
                agent_id=agent.agent_id,
                mandate_id=mandate.terms.mandate_id,
                mandate_hash=mandate.mandate_hash,
                reason_codes=(ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH.value,),
                safe_payload={"domain": "token_issue", "reason": "mandate_not_yet_valid"},
            )
            raise AuthorizationError(
                ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH,
                "Mandate is not yet active.",
                internal_reason="mandate_not_yet_valid",
            )

        remaining_seconds = int((valid_until - now).total_seconds())
        if mandate.status == MandateStatus.EXPIRED or remaining_seconds <= 0:
            await self._audit_event(
                event_type="auth.failed",
                request_id=request_id,
                customer_id=mandate.terms.customer_id,
                agent_id=agent.agent_id,
                mandate_id=mandate.terms.mandate_id,
                mandate_hash=mandate.mandate_hash,
                reason_codes=(ReasonCode.E_AUTH_EXPIRED.value,),
                safe_payload={"domain": "token_issue", "reason": "mandate_expired"},
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_EXPIRED,
                "Mandate has expired.",
                internal_reason="mandate_expired",
            )

        # 5. Bound TTL to at most 600s and never beyond mandate expiry.
        ttl_seconds = min(self._default_ttl_seconds, MAX_TOKEN_TTL_SECONDS, remaining_seconds)
        expires_at = now + timedelta(seconds=ttl_seconds)
        token_jti = f"tok_{secrets.token_hex(12)}"

        claims: dict[str, Any] = {
            "iss": AGENT_TOKEN_ISSUER,
            "aud": AGENT_TOKEN_AUDIENCE,
            "sub": mandate.terms.customer_id,
            "act": {"sub": agent.agent_id},
            "mandate_id": mandate.terms.mandate_id,
            "mandate_hash": mandate.mandate_hash,
            "cnf": {"jkt": agent.jkt},
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
            "jti": token_jti,
        }
        access_token = jwt.encode(
            claims,
            self._signing_key,
            algorithm=AGENT_TOKEN_ALG,
            headers={"typ": AGENT_TOKEN_TYP, "kid": self._key_id},
        )
        token_hash = sha256_hex(access_token.encode("ascii"))

        record = IssuedTokenRecord(
            jti=token_jti,
            customer_id=mandate.terms.customer_id,
            agent_id=agent.agent_id,
            mandate_id=mandate.terms.mandate_id,
            mandate_hash=mandate.mandate_hash,
            jkt=agent.jkt,
            token_hash=token_hash,
            issued_at=format_utc_timestamp(now),
            expires_at=format_utc_timestamp(expires_at),
            revoked_at=None,
        )

        async with session_scope(self._session_factory, write=True) as session:
            session.add(record)
            await maybe_await(session.flush())
            local_tx = SqlAlchemyTx(id=f"tx_{secrets.token_hex(6)}", session=session)
            await self._audit_event(
                event_type="token.issued",
                request_id=request_id,
                customer_id=mandate.terms.customer_id,
                agent_id=agent.agent_id,
                mandate_id=mandate.terms.mandate_id,
                mandate_hash=mandate.mandate_hash,
                safe_payload={
                    "token_jti": token_jti,
                    "jkt": agent.jkt,
                    "expires_in": ttl_seconds,
                    "proof_jti": verified_proof.proof_jti,
                },
                tx=local_tx,
            )

        return AccessTokenResult(
            access_token=access_token,
            token_type="DPoP",
            expires_in=ttl_seconds,
        )

    async def revoke_for_mandate(self, mandate_id: OpaqueId, tx: Tx) -> None:
        """Revoke all active tokens bound to `mandate_id` inside caller's `tx`."""

        now_iso = format_utc_timestamp(self._clock.now())
        async with session_scope(self._session_factory, tx, write=True) as session:
            stmt = (
                update(IssuedTokenRecord)
                .where(
                    IssuedTokenRecord.mandate_id == mandate_id,
                    IssuedTokenRecord.revoked_at.is_(None),
                )
                .values(revoked_at=now_iso)
            )
            result = await maybe_await(session.execute(stmt))
            await maybe_await(session.flush())
            revoked_count = int(result.rowcount or 0)
            await self._audit_event(
                event_type="token.revoked",
                mandate_id=mandate_id,
                safe_payload={"scope": "mandate", "revoked_count": revoked_count},
                tx=tx,
            )

    async def revoke_for_agent(self, agent_id: OpaqueId, tx: Tx) -> None:
        """Revoke all active tokens bound to `agent_id` inside caller's `tx`."""

        now_iso = format_utc_timestamp(self._clock.now())
        async with session_scope(self._session_factory, tx, write=True) as session:
            stmt = (
                update(IssuedTokenRecord)
                .where(
                    IssuedTokenRecord.agent_id == agent_id,
                    IssuedTokenRecord.revoked_at.is_(None),
                )
                .values(revoked_at=now_iso)
            )
            result = await maybe_await(session.execute(stmt))
            await maybe_await(session.flush())
            revoked_count = int(result.rowcount or 0)
            await self._audit_event(
                event_type="token.revoked",
                agent_id=agent_id,
                safe_payload={"scope": "agent", "revoked_count": revoked_count},
                tx=tx,
            )

    async def revoke_token(self, token_jti: OpaqueId, tx: Tx | None = None) -> None:
        """Revoke a single issued token by its `jti`."""

        now_iso = format_utc_timestamp(self._clock.now())
        async with session_scope(self._session_factory, tx, write=True) as session:
            record = await maybe_await(session.get(IssuedTokenRecord, token_jti))
            if record is not None and record.revoked_at is None:
                record.revoked_at = now_iso
                await maybe_await(session.flush())
                active_tx: Tx = (
                    tx
                    if tx is not None
                    else SqlAlchemyTx(id=f"tx_{secrets.token_hex(6)}", session=session)
                )
                await self._audit_event(
                    event_type="token.revoked",
                    customer_id=record.customer_id,
                    agent_id=record.agent_id,
                    mandate_id=record.mandate_id,
                    mandate_hash=record.mandate_hash,
                    safe_payload={"scope": "token", "token_jti": token_jti},
                    tx=active_tx,
                )


class SqlIdentityVerifier:
    """Full agent resource-request verifier conforming to `IdentityVerifier`.

    Validates:
    1. `Authorization: DPoP <access_token>` and `DPoP: <proof_jwt>` headers.
    2. EdDSA access token signature, `typ=at+jwt`, `iss`, `aud`, required claims, and TTL bounds.
    3. Token expiration against the injected `Clock`.
    4. B03 DPoP proof verification bound to `cnf.jkt`, `ath`, `bsh`, `htm`, `htu`, and replay.
    5. Live mandate binding & status (`E_MANDATE_REVOKED`, `E_AUTH_AGENT_MANDATE_MISMATCH`,
       `E_AUTH_EXPIRED`), live agent status (`E_AUTH_AGENT_SUSPENDED`), and persisted token
       revocation (`E_AUTH_TOKEN_REVOKED`) in the Section 6.4 precedence order.
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        clock: Clock,
        token_public_key: Ed25519PublicKey,
        proof_verifier: DPoPProofVerifier,
        mandate_reader: MandateReader,
        audit_log: AuditLog | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._token_public_key = token_public_key
        self._proof_verifier = proof_verifier
        self._mandate_reader = mandate_reader
        self._audit_log = audit_log

    async def _audit_failure(
        self,
        *,
        code: ReasonCode,
        internal_reason: str,
        customer_id: str | None = None,
        agent_id: str | None = None,
        mandate_id: str | None = None,
        mandate_hash: str | None = None,
    ) -> None:
        if self._audit_log is None:
            return
        await self._audit_log.append(
            AuditEvent(
                event_type="auth.failed",
                customer_id=customer_id,
                agent_id=agent_id,
                mandate_id=mandate_id,
                mandate_hash=mandate_hash,
                reason_codes=(code.value,),
                safe_payload={"domain": "agent_resource", "reason": internal_reason},
                occurred_at=self._clock.now(),
            )
        )

    async def _read_mandate_safe(self, mandate_id: OpaqueId) -> MandateSnapshot | None:
        try:
            return await self._mandate_reader.get(mandate_id)
        except (KeyError, LookupError, ResourceNotFoundError):
            return None

    async def authenticate(
        self,
        request_bytes: bytes,
        headers: Mapping[str, str],
        method: str,
        url: str,
    ) -> AuthContext:
        """Authenticate an agent API request and return a trusted `AuthContext`."""

        # 1. Extract Authorization DPoP token and DPoP proof header.
        try:
            raw_token = extract_dpop_authorization_token(headers)
        except AuthenticationError as exc:
            await self._audit_failure(code=exc.code, internal_reason=exc.internal_reason)
            raise

        proof_jwt = extract_dpop_header(headers)
        if proof_jwt is None:
            await self._audit_failure(
                code=ReasonCode.E_AUTH_MISSING, internal_reason="missing_dpop_proof"
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_MISSING,
                "Missing DPoP proof header.",
                internal_reason="missing_dpop_proof",
            )

        # 2. Verify access token header, EdDSA signature, issuer, audience, and required claims.
        try:
            header = jwt.get_unverified_header(raw_token)
        except jwt.InvalidTokenError as exc:
            await self._audit_failure(
                code=ReasonCode.E_AUTH_INVALID_TOKEN,
                internal_reason="malformed_access_token_header",
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_INVALID_TOKEN,
                "Invalid access token.",
                internal_reason="malformed_access_token_header",
            ) from exc

        if header.get("alg") != AGENT_TOKEN_ALG or header.get("typ") != AGENT_TOKEN_TYP:
            await self._audit_failure(
                code=ReasonCode.E_AUTH_INVALID_TOKEN,
                internal_reason="invalid_access_token_alg_or_typ",
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_INVALID_TOKEN,
                "Invalid access token.",
                internal_reason="invalid_access_token_alg_or_typ",
            )

        try:
            claims: dict[str, Any] = jwt.decode(
                raw_token,
                self._token_public_key,
                algorithms=[AGENT_TOKEN_ALG],
                issuer=AGENT_TOKEN_ISSUER,
                audience=AGENT_TOKEN_AUDIENCE,
                options={
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                    "require": [
                        "iss",
                        "aud",
                        "sub",
                        "act",
                        "mandate_id",
                        "mandate_hash",
                        "cnf",
                        "iat",
                        "exp",
                        "jti",
                    ],
                },
            )
        except jwt.InvalidTokenError as exc:
            await self._audit_failure(
                code=ReasonCode.E_AUTH_INVALID_TOKEN,
                internal_reason="invalid_access_token_signature_or_claims",
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_INVALID_TOKEN,
                "Invalid access token.",
                internal_reason="invalid_access_token_signature_or_claims",
            ) from exc

        customer_id = claims.get("sub")
        act = claims.get("act")
        cnf = claims.get("cnf")
        mandate_id = claims.get("mandate_id")
        mandate_hash = claims.get("mandate_hash")
        token_jti = claims.get("jti")
        iat = claims.get("iat")
        exp = claims.get("exp")

        agent_id = act.get("sub") if isinstance(act, dict) else None
        bound_jkt = cnf.get("jkt") if isinstance(cnf, dict) else None

        if (
            not isinstance(customer_id, str)
            or not customer_id
            or not isinstance(agent_id, str)
            or not agent_id
            or not isinstance(bound_jkt, str)
            or not bound_jkt
            or not isinstance(mandate_id, str)
            or not mandate_id
            or not isinstance(mandate_hash, str)
            or not mandate_hash
            or not isinstance(token_jti, str)
            or not token_jti
            or not isinstance(iat, int)
            or isinstance(iat, bool)
            or not isinstance(exp, int)
            or isinstance(exp, bool)
            or exp <= iat
            or (exp - iat) > MAX_TOKEN_TTL_SECONDS
        ):
            await self._audit_failure(
                code=ReasonCode.E_AUTH_INVALID_TOKEN,
                internal_reason="invalid_access_token_claim_structure",
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_INVALID_TOKEN,
                "Invalid access token.",
                internal_reason="invalid_access_token_claim_structure",
            )

        # 3. Check token expiry against injected clock.
        now = self._clock.now()
        if int(now.timestamp()) >= exp:
            await self._audit_failure(
                code=ReasonCode.E_AUTH_EXPIRED,
                internal_reason="token_expired",
                customer_id=customer_id,
                agent_id=agent_id,
                mandate_id=mandate_id,
                mandate_hash=mandate_hash,
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_EXPIRED,
                "Access token has expired.",
                internal_reason="token_expired",
            )

        # 4. Verify DPoP proof bound to token's cnf.jkt and ath (plus htm/htu/bsh/iat/replay).
        verified_proof = await self._proof_verifier.verify_proof(
            proof_jwt=proof_jwt,
            method=method,
            request_url=url,
            request_bytes=request_bytes,
            access_token=raw_token,
            expected_jkt=bound_jkt,
        )
        if verified_proof.agent.agent_id != agent_id:
            await self._audit_failure(
                code=ReasonCode.E_AUTH_KEY_MISMATCH,
                internal_reason="token_agent_key_mismatch",
                customer_id=customer_id,
                agent_id=verified_proof.agent.agent_id,
                mandate_id=mandate_id,
                mandate_hash=mandate_hash,
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_KEY_MISMATCH,
                "Token agent binding does not match registered proof key.",
                internal_reason="token_agent_key_mismatch",
            )

        # 5. Check live mandate binding & status FIRST (Section 6.4 precedence).
        mandate = await self._read_mandate_safe(mandate_id)
        if mandate is None:
            await self._audit_failure(
                code=ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH,
                internal_reason="mandate_not_found",
                customer_id=customer_id,
                agent_id=agent_id,
                mandate_id=mandate_id,
                mandate_hash=mandate_hash,
            )
            raise AuthorizationError(
                ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH,
                "Mandate is not valid for this token.",
                internal_reason="mandate_not_found",
            )

        if (
            mandate.terms.agent_id != agent_id
            or mandate.terms.customer_id != customer_id
            or mandate.mandate_hash != mandate_hash
        ):
            await self._audit_failure(
                code=ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH,
                internal_reason="token_mandate_binding_mismatch",
                customer_id=customer_id,
                agent_id=agent_id,
                mandate_id=mandate_id,
                mandate_hash=mandate_hash,
            )
            raise AuthorizationError(
                ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH,
                "Token does not match the bound mandate authority.",
                internal_reason="token_mandate_binding_mismatch",
            )

        if mandate.status in (MandateStatus.REVOKED, MandateStatus.SUSPENDED):
            await self._audit_failure(
                code=ReasonCode.E_MANDATE_REVOKED,
                internal_reason=f"mandate_status_{str(mandate.status).lower()}",
                customer_id=customer_id,
                agent_id=agent_id,
                mandate_id=mandate_id,
                mandate_hash=mandate_hash,
            )
            raise AuthorizationError(
                ReasonCode.E_MANDATE_REVOKED,
                "Mandate has been revoked or suspended.",
                internal_reason=f"mandate_status_{str(mandate.status).lower()}",
            )

        valid_from = mandate.terms.valid_from.astimezone(UTC)
        valid_until = mandate.terms.valid_until.astimezone(UTC)
        if now < valid_from:
            await self._audit_failure(
                code=ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH,
                internal_reason="mandate_not_yet_valid",
                customer_id=customer_id,
                agent_id=agent_id,
                mandate_id=mandate_id,
                mandate_hash=mandate_hash,
            )
            raise AuthorizationError(
                ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH,
                "Mandate is not yet active.",
                internal_reason="mandate_not_yet_valid",
            )

        if mandate.status == MandateStatus.EXPIRED or now >= valid_until:
            await self._audit_failure(
                code=ReasonCode.E_AUTH_EXPIRED,
                internal_reason="mandate_expired",
                customer_id=customer_id,
                agent_id=agent_id,
                mandate_id=mandate_id,
                mandate_hash=mandate_hash,
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_EXPIRED,
                "Mandate has expired.",
                internal_reason="mandate_expired",
            )

        # 6. Check live agent status SECOND (Section 6.4 precedence).
        if verified_proof.agent.status != AgentStatus.ACTIVE:
            await self._audit_failure(
                code=ReasonCode.E_AUTH_AGENT_SUSPENDED,
                internal_reason=f"agent_status_{str(verified_proof.agent.status).lower()}",
                customer_id=customer_id,
                agent_id=agent_id,
                mandate_id=mandate_id,
                mandate_hash=mandate_hash,
            )
            raise AuthorizationError(
                ReasonCode.E_AUTH_AGENT_SUSPENDED,
                "Agent is suspended or disabled.",
                internal_reason=f"agent_status_{str(verified_proof.agent.status).lower()}",
            )

        # 7. Check persisted issued_tokens record and revocation THIRD (Section 6.4 precedence).
        async with session_scope(self._session_factory, write=False) as session:
            record = await maybe_await(session.get(IssuedTokenRecord, token_jti))

        presented_hash = sha256_hex(raw_token.encode("ascii"))
        if (
            record is None
            or not hmac.compare_digest(record.token_hash, presented_hash)
            or record.customer_id != customer_id
            or record.agent_id != agent_id
            or record.mandate_id != mandate_id
            or record.mandate_hash != mandate_hash
            or record.jkt != bound_jkt
        ):
            await self._audit_failure(
                code=ReasonCode.E_AUTH_INVALID_TOKEN,
                internal_reason="unissued_or_mismatched_token_record",
                customer_id=customer_id,
                agent_id=agent_id,
                mandate_id=mandate_id,
                mandate_hash=mandate_hash,
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_INVALID_TOKEN,
                "Invalid access token.",
                internal_reason="unissued_or_mismatched_token_record",
            )

        if record.revoked_at is not None:
            await self._audit_failure(
                code=ReasonCode.E_AUTH_TOKEN_REVOKED,
                internal_reason="token_revoked",
                customer_id=customer_id,
                agent_id=agent_id,
                mandate_id=mandate_id,
                mandate_hash=mandate_hash,
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_TOKEN_REVOKED,
                "Access token has been revoked.",
                internal_reason="token_revoked",
            )

        return AuthContext(
            customer_id=mandate.terms.customer_id,
            agent_id=verified_proof.agent.agent_id,
            mandate_id=mandate.terms.mandate_id,
            mandate_hash=mandate.mandate_hash,
            jkt=verified_proof.jkt,
            token_jti=token_jti,
            proof_jti=verified_proof.proof_jti,
        )


__all__ = [
    "AGENT_TOKEN_ALG",
    "AGENT_TOKEN_AUDIENCE",
    "AGENT_TOKEN_ISSUER",
    "AGENT_TOKEN_TYP",
    "MAX_TOKEN_TTL_SECONDS",
    "SqlIdentityVerifier",
    "SqlTokenService",
    "extract_dpop_authorization_token",
]
