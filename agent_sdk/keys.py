"""Member B Ed25519 agent key generation, safe storage/loading, JWK, and RFC 7638 thumbprint."""

from __future__ import annotations

import base64
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from contracts.canonical import canonical_json, sha256_base64url


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def public_key_to_jwk(public_key: Ed25519PublicKey, *, kid: str | None = None) -> dict[str, str]:
    """Export an Ed25519 public key as a minimal OKP public JWK."""

    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    jwk: dict[str, str] = {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": _b64url_encode(raw),
    }
    if kid is not None:
        cleaned = kid.strip()
        if not cleaned:
            raise ValueError("kid must be non-empty when provided")
        jwk["kid"] = cleaned
    return jwk


def jwk_thumbprint(jwk: Mapping[str, Any]) -> str:
    """Compute RFC 7638 / RFC 8037 SHA-256 thumbprint (`jkt`) for an Ed25519 public JWK."""

    if "d" in jwk:
        raise ValueError("public JWK must not contain private key parameter 'd'")
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
        raise ValueError("only OKP Ed25519 public JWKs are supported")
    x_val = jwk.get("x")
    if not isinstance(x_val, str) or not x_val:
        raise ValueError("public JWK must include non-empty 'x'")
    return sha256_base64url(canonical_json({"crv": "Ed25519", "kty": "OKP", "x": x_val}))


@dataclass(frozen=True)
class AgentKeyPair:
    """In-memory Ed25519 agent key pair; private key is excluded from `repr`."""

    private_key: Ed25519PrivateKey = field(repr=False)

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self.private_key.public_key()

    def public_jwk(self, *, kid: str | None = None) -> dict[str, str]:
        return public_key_to_jwk(self.public_key, kid=kid)

    @property
    def jkt(self) -> str:
        return jwk_thumbprint(self.public_jwk())

    def sign_bytes(self, message: bytes) -> bytes:
        return self.private_key.sign(message)

    def sign_b64url(self, message: bytes) -> str:
        return _b64url_encode(self.sign_bytes(message))


def generate_agent_keypair() -> AgentKeyPair:
    """Generate a new Ed25519 agent key pair."""

    return AgentKeyPair(private_key=Ed25519PrivateKey.generate())


def save_private_key_pem(
    keypair: AgentKeyPair,
    path: str | Path,
    *,
    password: bytes | None = None,
    overwrite: bool = False,
) -> Path:
    """Persist an Ed25519 private key in PKCS8 PEM format with restrictive permissions.

    Refuses to overwrite an existing key file unless `overwrite=True`.
    Never commit saved private key files to Git.
    """

    target = Path(path)
    if target.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite existing key file: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)

    encryption: serialization.KeySerializationEncryption = (
        serialization.BestAvailableEncryption(password)
        if password is not None
        else serialization.NoEncryption()
    )
    pem_bytes = keypair.private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=encryption,
    )
    target.write_bytes(pem_bytes)
    try:
        os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # Windows ACLs may ignore POSIX chmod bits; best-effort tightening.
        pass
    return target


def load_private_key_pem(
    path: str | Path,
    *,
    password: bytes | None = None,
) -> AgentKeyPair:
    """Load an Ed25519 PKCS8 PEM private key from disk, rejecting non-Ed25519 keys."""

    pem_bytes = Path(path).read_bytes()
    loaded = serialization.load_pem_private_key(pem_bytes, password=password)
    if not isinstance(loaded, Ed25519PrivateKey):
        raise ValueError("Only Ed25519 private keys are supported by AgentGate SDK")
    return AgentKeyPair(private_key=loaded)


def save_public_jwk(
    keypair: AgentKeyPair,
    path: str | Path,
    *,
    kid: str | None = None,
    overwrite: bool = True,
) -> Path:
    """Save public JWK and RFC 7638 thumbprint (strictly public material, no private key)."""

    target = Path(path)
    if target.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite existing public JWK file: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    jwk = keypair.public_jwk(kid=kid)
    payload = {"public_jwk": jwk, "jkt": keypair.jkt}
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return target


__all__ = [
    "AgentKeyPair",
    "generate_agent_keypair",
    "jwk_thumbprint",
    "load_private_key_pem",
    "public_key_to_jwk",
    "save_private_key_pem",
    "save_public_jwk",
]
