"""Member B client-side DPoP-style Ed25519 request proof construction."""

from __future__ import annotations

import base64
import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from contracts.canonical import sha256_base64url

from .keys import AgentKeyPair

DPOP_TYP = "dpop+jwt"
DPOP_ALG = "EdDSA"


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def normalize_gateway_origin(gateway_url: str) -> str:
    """Validate and normalize the gateway origin (`scheme://host[:port]`)."""

    parsed = urlsplit(gateway_url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("gateway_url must be a valid http(s) origin")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("gateway_url must not include a path, query, or fragment")
    return f"{parsed.scheme}://{parsed.netloc}"


def build_htu(gateway_origin: str, path_or_url: str) -> str:
    """Build RFC 9449 `htu` from the gateway origin and path, stripping query/fragment."""

    origin = normalize_gateway_origin(gateway_origin)
    parsed = urlsplit(path_or_url)
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{origin}{path}"


@dataclass(frozen=True)
class BuiltProof:
    """Signed DPoP-style proof; raw JWT string is excluded from `repr`."""

    jwt: str = field(repr=False)
    jti: str
    htm: str
    htu: str
    iat: int
    bsh: str
    ath: str | None = None


def build_dpop_proof(
    *,
    keypair: AgentKeyPair,
    method: str,
    gateway_origin: str,
    path_or_url: str,
    body_bytes: bytes,
    access_token: str | None = None,
    now: datetime | None = None,
    jti: str | None = None,
) -> BuiltProof:
    """Construct a freshly signed Ed25519 DPoP-style proof over exact transmitted body bytes.

    - Includes `ath` iff `access_token` is supplied (resource requests).
    - Omits `ath` on token acquisition requests (`access_token=None`).
    - Generates a fresh unique `jti` on every call unless explicitly provided for testing.
    """

    clean_method = method.strip().upper()
    if not clean_method:
        raise ValueError("HTTP method must be non-empty")

    htu = build_htu(gateway_origin, path_or_url)
    ts = (now if now is not None else datetime.now(UTC)).astimezone(UTC)
    iat = int(ts.timestamp())
    proof_jti = jti if jti is not None else f"prf_{secrets.token_hex(12)}"
    bsh = sha256_base64url(body_bytes)

    header: dict[str, Any] = {
        "typ": DPOP_TYP,
        "alg": DPOP_ALG,
        "jwk": keypair.public_jwk(),
    }
    claims: dict[str, Any] = {
        "htm": clean_method,
        "htu": htu,
        "iat": iat,
        "jti": proof_jti,
        "bsh": bsh,
    }
    ath: str | None = None
    if access_token is not None:
        if not access_token:
            raise ValueError("access_token must be non-empty when provided")
        ath = sha256_base64url(access_token.encode("ascii"))
        claims["ath"] = ath

    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url_encode(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    sig_b64 = keypair.sign_b64url(f"{header_b64}.{payload_b64}".encode("ascii"))
    return BuiltProof(
        jwt=f"{header_b64}.{payload_b64}.{sig_b64}",
        jti=proof_jti,
        htm=clean_method,
        htu=htu,
        iat=iat,
        bsh=bsh,
        ath=ath,
    )


__all__ = [
    "DPOP_ALG",
    "DPOP_TYP",
    "BuiltProof",
    "build_dpop_proof",
    "build_htu",
    "normalize_gateway_origin",
]
