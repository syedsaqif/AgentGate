"""Member B owned canonical encodings shared by providers and consumers."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from typing import Any

import rfc8785

from .core_models import CanonicalAction


def canonical_json(value: Any) -> bytes:
    """Return RFC 8785 JSON Canonicalization Scheme bytes."""

    return rfc8785.dumps(value)


def sha256_prefixed(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def sha256_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(value).digest()).rstrip(b"=").decode("ascii")


def action_projection(action: CanonicalAction) -> Mapping[str, Any]:
    """Frozen authority projection; intent.note and request metadata are excluded."""

    params = action.params.model_dump(mode="json", exclude_none=True)
    return {
        "customer_id": action.customer_id,
        "agent_id": action.agent_id,
        "mandate_id": action.mandate_id,
        "mandate_hash": action.mandate_hash,
        "action_type": action.action_type,
        "params": params,
        "intent": {
            "task": action.intent.task,
            "category": action.intent.category,
        },
        "idempotency_key": action.idempotency_key,
    }


def action_hash(action: CanonicalAction) -> str:
    return sha256_prefixed(canonical_json(action_projection(action)))
