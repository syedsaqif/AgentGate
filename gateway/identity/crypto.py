"""Member B shared cryptographic utilities, canonical projections, and Ed25519 helpers.

All cryptographic operations delegate to reviewed libraries (`cryptography` and `rfc8785`).
Re-exports frozen canonical primitives from `contracts.canonical` without duplicating them.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from contracts.canonical import (
    action_hash,
    action_projection,
    canonical_json,
    sha256_base64url,
    sha256_prefixed,
)
from contracts.core_models import MandateTerms

GENESIS_PREV_HASH: str = "0" * 64

_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_DISALLOWED_PRIVATE_JWK_FIELDS = frozenset({"d", "p", "q", "dp", "dq", "qi", "oth", "k"})
_ALLOWED_PUBLIC_OKP_JWK_FIELDS = frozenset({"kty", "crv", "x", "alg", "use", "kid"})
_ED25519_PUBLIC_KEY_BYTES = 32
_ED25519_SEED_BYTES = 32
_ED25519_SIGNATURE_BYTES = 64


class CryptoValidationError(ValueError):
    """Raised when cryptographic material, encoding, or canonical input is invalid."""


class InvalidJWKError(CryptoValidationError):
    """Raised when an Ed25519 public JWK violates AgentGate's strict key policy."""


def b64url_encode(data: bytes) -> str:
    """Encode raw bytes as unpadded base64url (RFC 4648 section 5)."""

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    """Decode strict unpadded base64url, rejecting padding, whitespace, and non-canonical bits."""

    if not isinstance(value, str):
        raise CryptoValidationError("base64url value must be a string")
    if value == "":
        return b""
    if "=" in value:
        raise CryptoValidationError("base64url value must be unpadded")
    if len(value) % 4 == 1 or not _B64URL_RE.fullmatch(value):
        raise CryptoValidationError("invalid base64url encoding")

    padding = "=" * ((4 - len(value) % 4) % 4)
    try:
        decoded = base64.urlsafe_b64decode(value + padding)
    except Exception as exc:
        raise CryptoValidationError("invalid base64url encoding") from exc

    if b64url_encode(decoded) != value:
        raise CryptoValidationError("non-canonical base64url trailing bits")
    return decoded


def sha256_hex(value: bytes) -> str:
    """Return 64-character lowercase hexadecimal SHA-256 digest."""

    return hashlib.sha256(value).hexdigest()


def body_sha256_b64url(raw_body: bytes) -> str:
    """Compute AgentGate DPoP `bsh` digest over the exact transmitted UTF-8 request bytes."""

    return sha256_base64url(raw_body)


def access_token_hash_b64url(access_token: str) -> str:
    """Compute RFC 9449 `ath` digest over the ASCII representation of the raw access token."""

    if not isinstance(access_token, str) or not access_token:
        raise CryptoValidationError("access token must be a non-empty ASCII string")
    try:
        token_bytes = access_token.encode("ascii")
    except UnicodeEncodeError as exc:
        raise CryptoValidationError("access token must contain only ASCII characters") from exc
    return sha256_base64url(token_bytes)


def format_utc_timestamp(value: datetime) -> str:
    """Format an aware datetime as canonical UTC ISO-8601 ending in `Z`."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise CryptoValidationError("timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def mandate_terms_projection(terms: MandateTerms) -> dict[str, Any]:
    """Build the canonical JCS projection for immutable `MandateTerms`.

    Explicit schema defaults are preserved (never mixed between omitted and null),
    and timestamps are normalized to UTC ISO-8601 ending in `Z`.
    """

    projected = terms.model_dump(mode="json", exclude_none=False, exclude_defaults=False)
    projected["valid_from"] = format_utc_timestamp(terms.valid_from)
    projected["valid_until"] = format_utc_timestamp(terms.valid_until)
    return projected


def mandate_hash(terms: MandateTerms) -> str:
    """Compute `sha256:<hex>` digest over RFC 8785 canonical `MandateTerms`."""

    return sha256_prefixed(canonical_json(mandate_terms_projection(terms)))


def request_fingerprint(
    method: str,
    path: str,
    body: Mapping[str, Any] | None = None,
) -> str:
    """Compute idempotency request fingerprint over validated method, path, and full body.

    Unlike `action_hash`, this includes `intent.note` so reusing an idempotency key with
    any modified request body is detected as a conflict.
    """

    if not method or not method.strip():
        raise CryptoValidationError("HTTP method must be non-empty")
    if not path or not path.startswith("/"):
        raise CryptoValidationError("HTTP path must start with '/'")

    projection: dict[str, Any] = {
        "method": method.strip().upper(),
        "path": path,
        "body": dict(body) if body is not None else None,
    }
    return sha256_prefixed(canonical_json(projection))


def validate_hex_sha256(digest: str, *, field_name: str = "digest") -> str:
    """Validate a 64-character lowercase hexadecimal SHA-256 string."""

    if not isinstance(digest, str) or not _HEX_64_RE.fullmatch(digest):
        raise CryptoValidationError(f"{field_name} must be 64 lowercase hexadecimal characters")
    return digest


def parse_prefixed_sha256(prefixed_digest: str) -> bytes:
    """Parse `sha256:<64-lowercase-hex>` into its raw 32-byte digest."""

    if not isinstance(prefixed_digest, str) or not prefixed_digest.startswith("sha256:"):
        raise CryptoValidationError("prefixed digest must start with 'sha256:'")
    hex_part = validate_hex_sha256(prefixed_digest.removeprefix("sha256:"), field_name="digest")
    return bytes.fromhex(hex_part)


def audit_entry_hash(prev_hash: str, entry_without_hash_and_sig: Mapping[str, Any]) -> str:
    """Compute chained audit entry hash per Section 7.6.

    `entry_hash = hex(SHA256(bytes.fromhex(prev_hash) || JCS(entry_without_hash_and_signature)))`
    The canonical entry must include `prev_hash` matching `prev_hash` and must not contain
    `entry_hash` or `sig`.
    """

    validate_hex_sha256(prev_hash, field_name="prev_hash")
    if "entry_hash" in entry_without_hash_and_sig or "sig" in entry_without_hash_and_sig:
        raise CryptoValidationError("audit entry projection must exclude 'entry_hash' and 'sig'")
    if entry_without_hash_and_sig.get("prev_hash") != prev_hash:
        raise CryptoValidationError("audit entry must include 'prev_hash' matching chain link")

    payload = bytes.fromhex(prev_hash) + canonical_json(dict(entry_without_hash_and_sig))
    return sha256_hex(payload)


def validate_ed25519_public_jwk(jwk: Mapping[str, Any]) -> dict[str, str]:
    """Validate and normalize an Ed25519 public JWK.

    Enforces AgentGate's Ed25519/EdDSA-only policy:
    - `kty == "OKP"`, `crv == "Ed25519"`, valid 32-byte unpadded base64url `x`.
    - Rejects embedded private key material (`d`, etc.), symmetric keys, and other algorithms.
    - Returns a clean `dict[str, str]` containing only validated public fields.
    """

    if not isinstance(jwk, Mapping):
        raise InvalidJWKError("JWK must be a mapping")

    for key, value in jwk.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise InvalidJWKError("JWK keys and values must be strings")

    forbidden = _DISALLOWED_PRIVATE_JWK_FIELDS.intersection(jwk.keys())
    if forbidden:
        raise InvalidJWKError("JWK must not contain private or symmetric key material")

    unknown = set(jwk.keys()) - _ALLOWED_PUBLIC_OKP_JWK_FIELDS
    if unknown:
        raise InvalidJWKError(f"unsupported JWK fields: {', '.join(sorted(unknown))}")

    if jwk.get("kty") != "OKP":
        raise InvalidJWKError("only OKP public keys are supported")
    if jwk.get("crv") != "Ed25519":
        raise InvalidJWKError("only Ed25519 curve is supported")

    alg = jwk.get("alg")
    if alg is not None and alg != "EdDSA":
        raise InvalidJWKError("only EdDSA algorithm is supported")

    use = jwk.get("use")
    if use is not None and use != "sig":
        raise InvalidJWKError("JWK use must be 'sig'")

    kid = jwk.get("kid")
    if kid is not None and not kid.strip():
        raise InvalidJWKError("JWK kid must be non-empty when provided")

    x_val = jwk.get("x")
    if not x_val:
        raise InvalidJWKError("JWK is missing required public parameter 'x'")

    try:
        raw_public = b64url_decode(x_val)
    except CryptoValidationError as exc:
        raise InvalidJWKError(f"invalid JWK 'x' encoding: {exc}") from exc

    if len(raw_public) != _ED25519_PUBLIC_KEY_BYTES:
        raise InvalidJWKError("Ed25519 public key must be exactly 32 bytes")

    try:
        Ed25519PublicKey.from_public_bytes(raw_public)
    except Exception as exc:
        raise InvalidJWKError("invalid Ed25519 public key bytes") from exc

    normalized: dict[str, str] = {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": x_val,
    }
    for optional_field in ("alg", "use", "kid"):
        if optional_field in jwk:
            normalized[optional_field] = jwk[optional_field]
    return normalized


def jwk_thumbprint(jwk: Mapping[str, Any]) -> str:
    """Compute RFC 7638 / RFC 8037 SHA-256 JWK Thumbprint (`jkt`) for a validated Ed25519 JWK."""

    normalized = validate_ed25519_public_jwk(jwk)
    required_members = {
        "crv": normalized["crv"],
        "kty": normalized["kty"],
        "x": normalized["x"],
    }
    return sha256_base64url(canonical_json(required_members))


def generate_ed25519_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    """Generate a fresh Ed25519 private/public key pair."""

    private_key = Ed25519PrivateKey.generate()
    return private_key, private_key.public_key()


def private_key_from_seed(seed: bytes) -> Ed25519PrivateKey:
    """Construct an Ed25519 private key from a 32-byte RFC 8032 seed (test vectors)."""

    if not isinstance(seed, bytes) or len(seed) != _ED25519_SEED_BYTES:
        raise CryptoValidationError("Ed25519 seed must be exactly 32 bytes")
    return Ed25519PrivateKey.from_private_bytes(seed)


def public_key_to_jwk(public_key: Ed25519PublicKey, *, kid: str | None = None) -> dict[str, str]:
    """Serialize an Ed25519 public key to a minimal OKP JWK."""

    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    jwk: dict[str, str] = {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": b64url_encode(raw),
    }
    if kid is not None:
        if not kid:
            raise InvalidJWKError("kid must be non-empty when provided")
        jwk["kid"] = kid
    return jwk


def jwk_to_public_key(jwk: Mapping[str, Any]) -> Ed25519PublicKey:
    """Validate an Ed25519 public JWK and construct an `Ed25519PublicKey`."""

    normalized = validate_ed25519_public_jwk(jwk)
    return Ed25519PublicKey.from_public_bytes(b64url_decode(normalized["x"]))


def public_key_thumbprint(public_key: Ed25519PublicKey) -> str:
    """Compute RFC 7638 JWK Thumbprint (`jkt`) directly from an `Ed25519PublicKey`."""

    return jwk_thumbprint(public_key_to_jwk(public_key))


def ed25519_sign(private_key: Ed25519PrivateKey, message: bytes) -> bytes:
    """Sign raw message bytes using Ed25519."""

    return private_key.sign(message)


def ed25519_sign_b64url(private_key: Ed25519PrivateKey, message: bytes) -> str:
    """Sign raw message bytes using Ed25519 and return unpadded base64url signature."""

    return b64url_encode(ed25519_sign(private_key, message))


def ed25519_verify(public_key: Ed25519PublicKey, message: bytes, signature: bytes) -> bool:
    """Verify a raw 64-byte Ed25519 signature over `message`."""

    if len(signature) != _ED25519_SIGNATURE_BYTES:
        return False
    try:
        public_key.verify(signature, message)
        return True
    except InvalidSignature:
        return False


def ed25519_verify_b64url(
    public_key: Ed25519PublicKey, message: bytes, signature_b64url: str
) -> bool:
    """Verify an unpadded base64url Ed25519 signature over `message`."""

    try:
        signature = b64url_decode(signature_b64url)
    except CryptoValidationError:
        return False
    return ed25519_verify(public_key, message, signature)


def sign_audit_entry_hash(private_key: Ed25519PrivateKey, entry_hash: str) -> str:
    """Sign a 64-hex audit `entry_hash` as `base64url(Ed25519_sign(key, bytes.fromhex(hash)))`."""

    validate_hex_sha256(entry_hash, field_name="entry_hash")
    return ed25519_sign_b64url(private_key, bytes.fromhex(entry_hash))


def verify_audit_entry_signature(
    public_key: Ed25519PublicKey, entry_hash: str, sig_b64url: str
) -> bool:
    """Verify an Ed25519 audit entry signature against a 64-hex `entry_hash`."""

    try:
        validate_hex_sha256(entry_hash, field_name="entry_hash")
    except CryptoValidationError:
        return False
    return ed25519_verify_b64url(public_key, bytes.fromhex(entry_hash), sig_b64url)


def sign_prefixed_sha256(private_key: Ed25519PrivateKey, prefixed_digest: str) -> str:
    """Sign a `sha256:<hex>` digest (e.g., `mandate_hash`) over its raw 32 digest bytes."""

    return ed25519_sign_b64url(private_key, parse_prefixed_sha256(prefixed_digest))


def verify_prefixed_sha256(
    public_key: Ed25519PublicKey, prefixed_digest: str, sig_b64url: str
) -> bool:
    """Verify an Ed25519 signature over the raw 32 bytes of a `sha256:<hex>` digest."""

    try:
        digest_bytes = parse_prefixed_sha256(prefixed_digest)
    except CryptoValidationError:
        return False
    return ed25519_verify_b64url(public_key, digest_bytes, sig_b64url)


def serialize_ed25519_private_key_pem(
    private_key: Ed25519PrivateKey, *, password: bytes | None = None
) -> bytes:
    """Serialize an Ed25519 private key to PKCS8 PEM bytes."""

    encryption: serialization.KeySerializationEncryption = (
        serialization.BestAvailableEncryption(password)
        if password is not None
        else serialization.NoEncryption()
    )
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=encryption,
    )


def load_ed25519_private_key_pem(
    pem_bytes: bytes, *, password: bytes | None = None
) -> Ed25519PrivateKey:
    """Load a PKCS8 PEM private key and enforce that it is strictly Ed25519."""

    try:
        loaded = serialization.load_pem_private_key(pem_bytes, password=password)
    except Exception as exc:
        raise CryptoValidationError("failed to load PEM private key") from exc
    if not isinstance(loaded, Ed25519PrivateKey):
        raise CryptoValidationError("only Ed25519 private keys are supported")
    return loaded


def serialize_ed25519_public_key_pem(public_key: Ed25519PublicKey) -> bytes:
    """Serialize an Ed25519 public key to SubjectPublicKeyInfo PEM bytes."""

    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def load_ed25519_public_key_pem(pem_bytes: bytes) -> Ed25519PublicKey:
    """Load a SubjectPublicKeyInfo PEM public key and enforce that it is strictly Ed25519."""

    try:
        loaded = serialization.load_pem_public_key(pem_bytes)
    except Exception as exc:
        raise CryptoValidationError("failed to load PEM public key") from exc
    if not isinstance(loaded, Ed25519PublicKey):
        raise CryptoValidationError("only Ed25519 public keys are supported")
    return loaded


__all__ = [
    "GENESIS_PREV_HASH",
    "CryptoValidationError",
    "InvalidJWKError",
    "access_token_hash_b64url",
    "action_hash",
    "action_projection",
    "audit_entry_hash",
    "b64url_decode",
    "b64url_encode",
    "body_sha256_b64url",
    "canonical_json",
    "ed25519_sign",
    "ed25519_sign_b64url",
    "ed25519_verify",
    "ed25519_verify_b64url",
    "format_utc_timestamp",
    "generate_ed25519_keypair",
    "jwk_thumbprint",
    "jwk_to_public_key",
    "load_ed25519_private_key_pem",
    "load_ed25519_public_key_pem",
    "mandate_hash",
    "mandate_terms_projection",
    "parse_prefixed_sha256",
    "private_key_from_seed",
    "public_key_thumbprint",
    "public_key_to_jwk",
    "request_fingerprint",
    "serialize_ed25519_private_key_pem",
    "serialize_ed25519_public_key_pem",
    "sha256_base64url",
    "sha256_hex",
    "sha256_prefixed",
    "sign_audit_entry_hash",
    "sign_prefixed_sha256",
    "validate_ed25519_public_jwk",
    "validate_hex_sha256",
    "verify_audit_entry_signature",
    "verify_prefixed_sha256",
]
