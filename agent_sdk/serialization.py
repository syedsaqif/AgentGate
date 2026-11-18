"""Member B serialize-once request body and strict money-action payload helpers."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from contracts.canonical import canonical_json, sha256_base64url
from contracts.common import ContractModel, Currency, OpaqueId, PositivePaise
from contracts.core_models import Intent


class MoneyActionPayload(ContractModel):
    """Strict money-out request body matching AgentGate's frozen OpenAPI schema."""

    from_account: OpaqueId
    payee_id: OpaqueId
    amount_paise: PositivePaise
    currency: Currency = "INR"
    reference: str | None = Field(default=None, max_length=120)
    intent: Intent


@dataclass(frozen=True)
class SerializedRequestBody:
    """Immutable wire-body bytes and their precomputed DPoP `bsh` digest.

    Because the body is serialized exactly once into `raw_bytes`, both proof signing
    and HTTP transmission (including across retries) operate on byte-identical data.
    """

    raw_bytes: bytes
    bsh: str
    content_type: str | None


def serialize_json_body(
    payload: Mapping[str, Any] | BaseModel | None,
) -> SerializedRequestBody:
    """Serialize a request body once into deterministic RFC 8785 UTF-8 bytes and compute `bsh`."""

    if payload is None:
        return SerializedRequestBody(
            raw_bytes=b"",
            bsh=sha256_base64url(b""),
            content_type=None,
        )

    if isinstance(payload, BaseModel):
        normalized: Any = payload.model_dump(mode="json", exclude_none=True)
    else:
        normalized = dict(payload)

    raw_bytes = canonical_json(normalized)
    return SerializedRequestBody(
        raw_bytes=raw_bytes,
        bsh=sha256_base64url(raw_bytes),
        content_type="application/json",
    )


def new_idempotency_key(prefix: str = "idem") -> str:
    """Generate a unique opaque business idempotency key for a money operation."""

    clean_prefix = prefix.strip()
    if not clean_prefix or any(ch.isspace() for ch in clean_prefix):
        raise ValueError("idempotency key prefix must be a non-empty token without whitespace")
    return f"{clean_prefix}-{secrets.token_hex(12)}"


__all__ = [
    "MoneyActionPayload",
    "SerializedRequestBody",
    "new_idempotency_key",
    "serialize_json_body",
]
