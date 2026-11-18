"""Member B DPoP-style Ed25519 proof verifier with trusted-origin HTU and atomic replay checks."""

from __future__ import annotations

import hmac
import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from contracts.audit_models import AuditEvent
from contracts.audit_ports import AuditLog
from contracts.common import Clock, ContractModel, OpaqueId
from contracts.identity_models import AgentPublicIdentity
from contracts.identity_ports import AgentRegistry
from contracts.reason_codes import ReasonCode

from .crypto import (
    CryptoValidationError,
    InvalidJWKError,
    access_token_hash_b64url,
    b64url_decode,
    body_sha256_b64url,
    ed25519_verify_b64url,
    jwk_thumbprint,
    jwk_to_public_key,
    validate_ed25519_public_jwk,
)
from .errors import AuthenticationError
from .replay import ProofReplayCache

DPOP_TYP = "dpop+jwt"
DPOP_ALG = "EdDSA"
DEFAULT_PROOF_SKEW_SECONDS = 60
_MAX_PROOF_LENGTH = 8192
_MAX_JTI_LENGTH = 128
_GENERIC_PROOF_ERROR_MESSAGE = "Invalid agent authentication proof."


class VerifiedProof(ContractModel):
    """Cryptographically verified and replay-claimed DPoP-style proof result."""

    agent: AgentPublicIdentity
    jkt: OpaqueId
    proof_jti: OpaqueId
    htm: OpaqueId
    htu: OpaqueId
    iat: int
    bsh: OpaqueId
    ath: str | None = None


def normalize_trusted_origin(trusted_origin: str) -> str:
    """Validate and normalize the configured gateway origin (`scheme://host[:port]`)."""

    parsed = urlsplit(trusted_origin.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("trusted_origin must be an http(s) origin with host")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("trusted_origin must not include a path, query, or fragment")
    return f"{parsed.scheme}://{parsed.netloc}"


def build_trusted_htu(trusted_origin: str, request_url: str) -> str:
    """Build the authoritative `htu` from the configured trusted origin and route path only.

    Ignores scheme/host/port from the incoming URL and strips query and fragment per RFC 9449.
    """

    origin = normalize_trusted_origin(trusted_origin)
    parsed = urlsplit(request_url)
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{origin}{path}"


def extract_dpop_header(headers: Mapping[str, str]) -> str | None:
    """Extract the `DPoP` header case-insensitively."""

    for key, value in headers.items():
        if key.lower() == "dpop":
            stripped = value.strip()
            return stripped or None
    return None


class DPoPProofVerifier:
    """Verifies AgentGate's Ed25519 DPoP-style per-request proofs.

    Validation order:
    1. Proof presence and compact 3-part JWS structure.
    2. Header `typ=dpop+jwt`, `alg=EdDSA`, strict Ed25519 public `jwk` (no private params).
    3. Optional `expected_jkt` binding (for resource requests bound to an access token).
    4. Registered agent lookup strictly by RFC 7638 `jkt` (never by body-supplied agent ID).
    5. Ed25519 cryptographic signature verification against the registered public key.
    6. Payload claims: `htm`, trusted-origin `htu` (no query/fragment), `bsh`, `ath`, `iat` (±60s),
       and non-empty `jti`.
    7. Atomic `(registered_jkt, proof_jti)` replay claim (only after cryptographic validation).
    """

    def __init__(
        self,
        *,
        registry: AgentRegistry,
        replay_cache: ProofReplayCache,
        clock: Clock,
        trusted_origin: str,
        audit_log: AuditLog | None = None,
        clock_skew_seconds: int = DEFAULT_PROOF_SKEW_SECONDS,
    ) -> None:
        if clock_skew_seconds < 0:
            raise ValueError("clock_skew_seconds must be non-negative")
        self._registry = registry
        self._replay_cache = replay_cache
        self._clock = clock
        self._trusted_origin = normalize_trusted_origin(trusted_origin)
        self._audit_log = audit_log
        self._clock_skew_seconds = clock_skew_seconds

    @property
    def trusted_origin(self) -> str:
        return self._trusted_origin

    async def _audit_failure(
        self,
        *,
        code: ReasonCode,
        internal_reason: str,
        request_id: str | None = None,
        verified_agent_id: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        if self._audit_log is None:
            return
        payload: dict[str, Any] = {"domain": "agent_proof", "reason": internal_reason}
        if extra:
            payload.update(extra)
        await self._audit_log.append(
            AuditEvent(
                event_type="auth.failed",
                request_id=request_id,
                agent_id=verified_agent_id,
                reason_codes=(code.value,),
                safe_payload=payload,
                occurred_at=self._clock.now(),
            )
        )

    async def _resolve_agent_by_jkt(self, jkt: str) -> AgentPublicIdentity | None:
        get_by_jkt = getattr(self._registry, "get_by_jkt", None)
        if callable(get_by_jkt):
            result: AgentPublicIdentity | None = await get_by_jkt(jkt)
            return result
        for candidate in await self._registry.list_public():
            if hmac.compare_digest(candidate.jkt, jkt):
                return candidate
        return None

    async def verify_proof(
        self,
        *,
        proof_jwt: str | None,
        method: str,
        request_url: str,
        request_bytes: bytes,
        access_token: str | None = None,
        expected_jkt: str | None = None,
        request_id: str | None = None,
    ) -> VerifiedProof:
        """Validate a DPoP-style proof and atomically claim `(jkt, jti)` on success."""

        if proof_jwt is None or not proof_jwt.strip():
            await self._audit_failure(
                code=ReasonCode.E_AUTH_MISSING,
                internal_reason="missing_dpop_proof",
                request_id=request_id,
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_MISSING,
                "Missing DPoP authentication proof.",
                internal_reason="missing_dpop_proof",
            )

        compact = proof_jwt.strip()
        if len(compact) > _MAX_PROOF_LENGTH or any(ch.isspace() for ch in compact):
            await self._audit_failure(
                code=ReasonCode.E_AUTH_PROOF_INVALID,
                internal_reason="malformed_proof_length_or_whitespace",
                request_id=request_id,
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_PROOF_INVALID,
                _GENERIC_PROOF_ERROR_MESSAGE,
                internal_reason="malformed_proof_length_or_whitespace",
            )

        parts = compact.split(".")
        if len(parts) != 3 or not all(parts):
            await self._audit_failure(
                code=ReasonCode.E_AUTH_PROOF_INVALID,
                internal_reason="malformed_proof_segments",
                request_id=request_id,
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_PROOF_INVALID,
                _GENERIC_PROOF_ERROR_MESSAGE,
                internal_reason="malformed_proof_segments",
            )

        header_b64, payload_b64, sig_b64 = parts

        # 1. Decode and strictly validate JWS header.
        try:
            header_obj = json.loads(b64url_decode(header_b64).decode("utf-8"))
        except Exception as exc:
            await self._audit_failure(
                code=ReasonCode.E_AUTH_PROOF_INVALID,
                internal_reason="invalid_proof_header_json",
                request_id=request_id,
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_PROOF_INVALID,
                _GENERIC_PROOF_ERROR_MESSAGE,
                internal_reason="invalid_proof_header_json",
            ) from exc

        if not isinstance(header_obj, dict):
            await self._audit_failure(
                code=ReasonCode.E_AUTH_PROOF_INVALID,
                internal_reason="proof_header_not_object",
                request_id=request_id,
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_PROOF_INVALID,
                _GENERIC_PROOF_ERROR_MESSAGE,
                internal_reason="proof_header_not_object",
            )

        if header_obj.get("typ") != DPOP_TYP:
            await self._audit_failure(
                code=ReasonCode.E_AUTH_PROOF_INVALID,
                internal_reason="invalid_proof_typ",
                request_id=request_id,
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_PROOF_INVALID,
                _GENERIC_PROOF_ERROR_MESSAGE,
                internal_reason="invalid_proof_typ",
            )

        if header_obj.get("alg") != DPOP_ALG:
            await self._audit_failure(
                code=ReasonCode.E_AUTH_PROOF_INVALID,
                internal_reason="unsupported_proof_alg",
                request_id=request_id,
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_PROOF_INVALID,
                _GENERIC_PROOF_ERROR_MESSAGE,
                internal_reason="unsupported_proof_alg",
            )

        raw_jwk = header_obj.get("jwk")
        if not isinstance(raw_jwk, Mapping):
            await self._audit_failure(
                code=ReasonCode.E_AUTH_PROOF_INVALID,
                internal_reason="invalid_proof_jwk",
                request_id=request_id,
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_PROOF_INVALID,
                _GENERIC_PROOF_ERROR_MESSAGE,
                internal_reason="invalid_proof_jwk",
            )
        try:
            normalized_jwk = validate_ed25519_public_jwk(raw_jwk)
            proof_jkt = jwk_thumbprint(normalized_jwk)
        except InvalidJWKError as exc:
            await self._audit_failure(
                code=ReasonCode.E_AUTH_PROOF_INVALID,
                internal_reason="invalid_proof_jwk",
                request_id=request_id,
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_PROOF_INVALID,
                _GENERIC_PROOF_ERROR_MESSAGE,
                internal_reason="invalid_proof_jwk",
            ) from exc

        # 2. Enforce token-key binding if verifying a resource request.
        if expected_jkt is not None and not hmac.compare_digest(proof_jkt, expected_jkt):
            await self._audit_failure(
                code=ReasonCode.E_AUTH_KEY_MISMATCH,
                internal_reason="proof_jkt_mismatch",
                request_id=request_id,
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_KEY_MISMATCH,
                "Proof key does not match bound token key.",
                internal_reason="proof_jkt_mismatch",
            )

        # 3. Resolve registered agent strictly from validated key thumbprint.
        agent = await self._resolve_agent_by_jkt(proof_jkt)
        if agent is None:
            await self._audit_failure(
                code=ReasonCode.E_AUTH_PROOF_INVALID,
                internal_reason="unknown_agent_key",
                request_id=request_id,
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_PROOF_INVALID,
                _GENERIC_PROOF_ERROR_MESSAGE,
                internal_reason="unknown_agent_key",
            )

        try:
            registered_public_key = jwk_to_public_key(agent.public_jwk)
        except InvalidJWKError as exc:
            await self._audit_failure(
                code=ReasonCode.E_AUTH_PROOF_INVALID,
                internal_reason="corrupt_registered_jwk",
                request_id=request_id,
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_PROOF_INVALID,
                _GENERIC_PROOF_ERROR_MESSAGE,
                internal_reason="corrupt_registered_jwk",
            ) from exc

        # 4. Verify Ed25519 signature using the registered public key BEFORE replay mutation.
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        if not ed25519_verify_b64url(registered_public_key, signing_input, sig_b64):
            await self._audit_failure(
                code=ReasonCode.E_AUTH_PROOF_INVALID,
                internal_reason="invalid_proof_signature",
                request_id=request_id,
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_PROOF_INVALID,
                _GENERIC_PROOF_ERROR_MESSAGE,
                internal_reason="invalid_proof_signature",
            )

        # 5. Decode and validate proof claims (signature is now verified against agent.agent_id).
        try:
            claims = json.loads(b64url_decode(payload_b64).decode("utf-8"))
        except Exception as exc:
            await self._audit_failure(
                code=ReasonCode.E_AUTH_PROOF_INVALID,
                internal_reason="invalid_proof_payload_json",
                request_id=request_id,
                verified_agent_id=agent.agent_id,
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_PROOF_INVALID,
                _GENERIC_PROOF_ERROR_MESSAGE,
                internal_reason="invalid_proof_payload_json",
            ) from exc

        if not isinstance(claims, dict):
            await self._audit_failure(
                code=ReasonCode.E_AUTH_PROOF_INVALID,
                internal_reason="proof_payload_not_object",
                request_id=request_id,
                verified_agent_id=agent.agent_id,
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_PROOF_INVALID,
                _GENERIC_PROOF_ERROR_MESSAGE,
                internal_reason="proof_payload_not_object",
            )

        expected_htm = method.strip().upper()
        htm = claims.get("htm")
        if not isinstance(htm, str) or htm != expected_htm:
            await self._audit_failure(
                code=ReasonCode.E_AUTH_PROOF_INVALID,
                internal_reason="proof_htm_mismatch",
                request_id=request_id,
                verified_agent_id=agent.agent_id,
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_PROOF_INVALID,
                _GENERIC_PROOF_ERROR_MESSAGE,
                internal_reason="proof_htm_mismatch",
            )

        expected_htu = build_trusted_htu(self._trusted_origin, request_url)
        htu = claims.get("htu")
        if (
            not isinstance(htu, str)
            or "?" in htu
            or "#" in htu
            or not hmac.compare_digest(htu, expected_htu)
        ):
            await self._audit_failure(
                code=ReasonCode.E_AUTH_PROOF_INVALID,
                internal_reason="proof_htu_mismatch",
                request_id=request_id,
                verified_agent_id=agent.agent_id,
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_PROOF_INVALID,
                _GENERIC_PROOF_ERROR_MESSAGE,
                internal_reason="proof_htu_mismatch",
            )

        expected_bsh = body_sha256_b64url(request_bytes)
        bsh = claims.get("bsh")
        if not isinstance(bsh, str) or not hmac.compare_digest(bsh, expected_bsh):
            await self._audit_failure(
                code=ReasonCode.E_AUTH_PROOF_INVALID,
                internal_reason="proof_bsh_mismatch",
                request_id=request_id,
                verified_agent_id=agent.agent_id,
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_PROOF_INVALID,
                _GENERIC_PROOF_ERROR_MESSAGE,
                internal_reason="proof_bsh_mismatch",
            )

        ath_claim = claims.get("ath")
        verified_ath: str | None = None
        if access_token is not None:
            try:
                expected_ath = access_token_hash_b64url(access_token)
            except CryptoValidationError as exc:
                await self._audit_failure(
                    code=ReasonCode.E_AUTH_INVALID_TOKEN,
                    internal_reason="invalid_access_token_encoding",
                    request_id=request_id,
                    verified_agent_id=agent.agent_id,
                )
                raise AuthenticationError(
                    ReasonCode.E_AUTH_INVALID_TOKEN,
                    "Invalid access token.",
                    internal_reason="invalid_access_token_encoding",
                ) from exc
            if not isinstance(ath_claim, str) or not hmac.compare_digest(ath_claim, expected_ath):
                await self._audit_failure(
                    code=ReasonCode.E_AUTH_PROOF_INVALID,
                    internal_reason="proof_ath_mismatch",
                    request_id=request_id,
                    verified_agent_id=agent.agent_id,
                )
                raise AuthenticationError(
                    ReasonCode.E_AUTH_PROOF_INVALID,
                    _GENERIC_PROOF_ERROR_MESSAGE,
                    internal_reason="proof_ath_mismatch",
                )
            verified_ath = ath_claim
        elif "ath" in claims and ath_claim is not None:
            await self._audit_failure(
                code=ReasonCode.E_AUTH_PROOF_INVALID,
                internal_reason="unexpected_ath_on_token_request",
                request_id=request_id,
                verified_agent_id=agent.agent_id,
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_PROOF_INVALID,
                _GENERIC_PROOF_ERROR_MESSAGE,
                internal_reason="unexpected_ath_on_token_request",
            )

        iat = claims.get("iat")
        if not isinstance(iat, int) or isinstance(iat, bool):
            await self._audit_failure(
                code=ReasonCode.E_AUTH_PROOF_INVALID,
                internal_reason="invalid_proof_iat",
                request_id=request_id,
                verified_agent_id=agent.agent_id,
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_PROOF_INVALID,
                _GENERIC_PROOF_ERROR_MESSAGE,
                internal_reason="invalid_proof_iat",
            )

        now_epoch = int(self._clock.now().timestamp())
        if abs(now_epoch - iat) > self._clock_skew_seconds:
            await self._audit_failure(
                code=ReasonCode.E_AUTH_CLOCK_SKEW,
                internal_reason="proof_clock_skew",
                request_id=request_id,
                verified_agent_id=agent.agent_id,
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_CLOCK_SKEW,
                "Proof timestamp is outside the allowed clock skew window.",
                internal_reason="proof_clock_skew",
            )

        jti = claims.get("jti")
        if (
            not isinstance(jti, str)
            or not jti
            or len(jti) > _MAX_JTI_LENGTH
            or any(ch.isspace() for ch in jti)
        ):
            await self._audit_failure(
                code=ReasonCode.E_AUTH_PROOF_INVALID,
                internal_reason="missing_or_invalid_proof_jti",
                request_id=request_id,
                verified_agent_id=agent.agent_id,
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_PROOF_INVALID,
                _GENERIC_PROOF_ERROR_MESSAGE,
                internal_reason="missing_or_invalid_proof_jti",
            )

        # 6. Atomically claim (registered_jkt, proof_jti) only after full cryptographic validation.
        claimed = await self._replay_cache.claim(
            jkt=agent.jkt,
            proof_jti=jti,
            iat_epoch=iat,
            tolerance_seconds=self._clock_skew_seconds,
        )
        if not claimed:
            await self._audit_failure(
                code=ReasonCode.E_AUTH_REPLAY,
                internal_reason="proof_replay",
                request_id=request_id,
                verified_agent_id=agent.agent_id,
                extra={"jkt": agent.jkt, "proof_jti": jti},
            )
            raise AuthenticationError(
                ReasonCode.E_AUTH_REPLAY,
                "Authentication proof has already been used.",
                internal_reason="proof_replay",
            )

        return VerifiedProof(
            agent=agent,
            jkt=agent.jkt,
            proof_jti=jti,
            htm=htm,
            htu=htu,
            iat=iat,
            bsh=bsh,
            ath=verified_ath,
        )


__all__ = [
    "DEFAULT_PROOF_SKEW_SECONDS",
    "DPOP_ALG",
    "DPOP_TYP",
    "DPoPProofVerifier",
    "VerifiedProof",
    "build_trusted_htu",
    "extract_dpop_header",
    "normalize_trusted_origin",
]
