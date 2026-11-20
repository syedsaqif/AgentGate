"""Member A atomic idempotency claiming and fingerprinting."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.canonical import canonical_json, sha256_prefixed
from contracts.reason_codes import ReasonCode
from gateway.errors import AgentGateError
from gateway.executor.schema import IdempotencyRecordTable


def compute_request_fingerprint(
    method: str,
    path: str,
    body: dict[str, Any] | Any,
) -> str:
    """Compute RFC 8785 JCS hash of validated route/method/body including note."""
    if hasattr(body, "model_dump"):
        if hasattr(body, "params") and hasattr(body, "action_type"):
            # CanonicalAction: fingerprint the semantic action payload excluding
            # ephemeral request metadata
            params_dict = (
                body.params.model_dump(mode="json", exclude_none=True)
                if hasattr(body.params, "model_dump")
                else body.params
            )
            intent_dict = (
                body.intent.model_dump(mode="json", exclude_none=True)
                if getattr(body, "intent", None) and hasattr(body.intent, "model_dump")
                else getattr(body, "intent", None)
            )
            serialized_body = {
                "action_type": str(body.action_type),
                "customer_id": getattr(body, "customer_id", None),
                "agent_id": getattr(body, "agent_id", None),
                "mandate_id": getattr(body, "mandate_id", None),
                "params": params_dict,
                "intent": intent_dict,
            }
        else:
            serialized_body = body.model_dump(mode="json", exclude_none=True)
    elif isinstance(body, dict):
        serialized_body = body
    else:
        serialized_body = str(body)

    envelope = {
        "method": method.upper(),
        "path": path,
        "body": serialized_body,
    }
    return sha256_prefixed(canonical_json(envelope))


def derive_bank_idempotency_key(
    customer_id: str,
    agent_id: str,
    mandate_id: str,
    idempotency_key: str,
) -> str:
    """Deterministically derive a stable server-side bank key from gateway idempotency scope."""
    raw = f"{customer_id}:{agent_id}:{mandate_id}:{idempotency_key}".encode()
    digest = hashlib.sha256(raw).hexdigest()[:24]
    return f"bank_idem_{digest}"


async def claim_idempotency(
    session: AsyncSession,
    customer_id: str,
    agent_id: str,
    mandate_id: str,
    idempotency_key: str,
    request_fingerprint: str,
    request_id: str,
) -> tuple[IdempotencyRecordTable, bool]:
    """Claim an idempotency record atomically inside a transaction.

    Returns:
        (record, is_new): tuple of record and whether it was newly inserted.
    Raises:
        AgentGateError: 409 E_REQ_IDEMPOTENCY_CONFLICT if key is reused with different fingerprint.
    """
    stmt = select(IdempotencyRecordTable).where(
        IdempotencyRecordTable.customer_id == customer_id,
        IdempotencyRecordTable.agent_id == agent_id,
        IdempotencyRecordTable.mandate_id == mandate_id,
        IdempotencyRecordTable.idempotency_key == idempotency_key,
    )
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()

    if existing is not None:
        if existing.request_fingerprint != request_fingerprint:
            msg = f"Idempotency key '{idempotency_key}' reused with different request payload"
            raise AgentGateError(
                code=ReasonCode.E_REQ_IDEMPOTENCY_CONFLICT,
                message=msg,
                status_code=409,
            )
        return existing, False

    record = IdempotencyRecordTable(
        idempotency_id=f"idm_{uuid.uuid4().hex[:12]}",
        customer_id=customer_id,
        agent_id=agent_id,
        mandate_id=mandate_id,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        request_id=request_id,
        cached_status="RECEIVED",
    )
    session.add(record)
    await session.flush()
    return record, True


async def update_idempotency_status(
    session: AsyncSession,
    idempotency_id: str,
    status: str,
    cached_response_json: str | None = None,
) -> None:
    """Update cached status and response on completion."""
    stmt = select(IdempotencyRecordTable).where(
        IdempotencyRecordTable.idempotency_id == idempotency_id
    )
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if row is not None:
        row.cached_status = status
        if cached_response_json is not None:
            row.cached_response_json = cached_response_json
        await session.flush()
