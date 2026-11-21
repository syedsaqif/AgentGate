"""Member B immutable snapshot evidence bundle assembly from recorded `audit_log` rows."""

from __future__ import annotations

import json
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import select

from contracts.audit_models import EvidenceBundle, SignedRequestEvidence
from contracts.common import AuditRef, OpaqueId
from contracts.core_models import BankResult, CanonicalAction, MandateSnapshot
from contracts.identity_models import AgentPublicIdentity
from gateway.identity.errors import ResourceNotFoundError
from gateway.identity.models import SessionFactory, maybe_await, session_scope

from .checkpoints import CheckpointStore
from .models import AuditLogRecord
from .verify import verify_audit_chain

_DECISION_EVENT_TYPES: frozenset[str] = frozenset(
    {"policy.decision", "risk.scored", "decision.final"}
)
_APPROVAL_EVENT_PREFIX = "escalation."
_BANK_EVENT_TYPES: frozenset[str] = frozenset(
    {"bank.executed", "bank.failed", "bank.unknown", "bank.rejected"}
)


def build_request_evidence_payload(
    *,
    mandate: MandateSnapshot,
    agent: AgentPublicIdentity,
    action: CanonicalAction,
    signed_request: SignedRequestEvidence,
) -> dict[str, Any]:
    """Build the standardized immutable snapshot dict for `AuditEvent.safe_payload`.

    Intended for Member A's request pipeline to attach on `request.received` or `policy.decision`
    so Member B can assemble `EvidenceBundle` strictly from `audit_log` without reading A/C tables.
    """

    return {
        "mandate_snapshot": mandate.model_dump(mode="json"),
        "agent_identity": agent.model_dump(mode="json"),
        "action": action.model_dump(mode="json"),
        "signed_request": signed_request.model_dump(mode="json"),
    }


def _row_summary(row: AuditLogRecord) -> dict[str, Any]:
    return {
        "seq": row.seq,
        "event_type": row.event_type,
        "decision": row.decision,
        "reason_codes": json.loads(row.reason_codes_json),
        "risk": json.loads(row.risk_json) if row.risk_json is not None else None,
        "safe_payload": json.loads(row.safe_payload_json),
        "occurred_at": row.occurred_at,
        "entry_hash": row.entry_hash,
    }


async def assemble_evidence_bundle(
    *,
    session_factory: SessionFactory,
    request_id: OpaqueId,
    public_key: Ed25519PublicKey,
    checkpoint_store: CheckpointStore | None = None,
    anchor: AuditRef | None = None,
) -> EvidenceBundle:
    """Assemble a verifiable `EvidenceBundle` strictly from recorded `audit_log` entries."""

    async with session_scope(session_factory, write=False) as session:
        stmt = (
            select(AuditLogRecord)
            .where(AuditLogRecord.request_id == request_id)
            .order_by(AuditLogRecord.seq.asc())
        )
        result = await maybe_await(session.execute(stmt))
        rows: list[AuditLogRecord] = list(result.scalars().all())

    if not rows:
        raise ResourceNotFoundError(
            "No audit evidence found for the requested action.",
            internal_reason="evidence_request_not_found",
        )

    mandate: MandateSnapshot | None = None
    agent: AgentPublicIdentity | None = None
    action: CanonicalAction | None = None
    signed_request: SignedRequestEvidence | None = None
    bank_outcome: BankResult | None = None
    decisions: list[dict[str, Any]] = []
    approval_history: list[dict[str, Any]] = []
    audit_refs: list[AuditRef] = []

    for row in rows:
        audit_refs.append(AuditRef(seq=row.seq, entry_hash=row.entry_hash))
        payload: dict[str, Any] = json.loads(row.safe_payload_json)

        if mandate is None and isinstance(payload.get("mandate_snapshot"), dict):
            mandate = MandateSnapshot.model_validate(payload["mandate_snapshot"])
        if agent is None and isinstance(payload.get("agent_identity"), dict):
            agent = AgentPublicIdentity.model_validate(payload["agent_identity"])
        if action is None and isinstance(payload.get("action"), dict):
            action = CanonicalAction.model_validate(payload["action"])
        if signed_request is None and isinstance(payload.get("signed_request"), dict):
            signed_request = SignedRequestEvidence.model_validate(payload["signed_request"])

        if row.event_type in _DECISION_EVENT_TYPES or row.decision is not None:
            decisions.append(_row_summary(row))

        if row.event_type.startswith(_APPROVAL_EVENT_PREFIX):
            approval_history.append(_row_summary(row))

        if row.event_type in _BANK_EVENT_TYPES and isinstance(payload.get("bank_result"), dict):
            bank_outcome = BankResult.model_validate(payload["bank_result"])

    if mandate is None or agent is None or action is None or signed_request is None:
        raise ResourceNotFoundError(
            "Incomplete transaction evidence for the requested action.",
            internal_reason="evidence_snapshots_missing",
        )

    verification = await verify_audit_chain(
        session_factory=session_factory,
        public_key=public_key,
        checkpoint_store=checkpoint_store,
        anchor=anchor,
    )

    return EvidenceBundle(
        request_id=request_id,
        mandate=mandate,
        agent=agent,
        action=action,
        signed_request=signed_request,
        decisions=tuple(decisions),
        approval_history=tuple(approval_history),
        bank_outcome=bank_outcome,
        audit_references=tuple(audit_refs),
        verification=verification,
    )


__all__ = [
    "assemble_evidence_bundle",
    "build_request_evidence_payload",
]
