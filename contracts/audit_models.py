"""Member B owned audit and evidence boundary DTOs."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import Field, StrictInt

from .common import AuditRef, ContractModel, OpaqueId
from .core_models import BankResult, CanonicalAction, MandateSnapshot
from .identity_models import AgentPublicIdentity


class AuditEvent(ContractModel):
    event_type: OpaqueId
    request_id: str | None = None
    correlation_id: str | None = None
    customer_id: str | None = None
    agent_id: str | None = None
    mandate_id: str | None = None
    mandate_hash: str | None = None
    decision: str | None = None
    reason_codes: tuple[str, ...] = ()
    risk: dict[str, Any] | None = None
    safe_payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime


class VerifyFailure(ContractModel):
    seq: Annotated[StrictInt, Field(ge=1)]
    reason: str


class VerifyReport(ContractModel):
    valid: bool
    entries_checked: Annotated[StrictInt, Field(ge=0)]
    head_seq: Annotated[StrictInt, Field(ge=0)]
    head_hash: str
    anchored_through_seq: Annotated[StrictInt, Field(ge=0)]
    first_failure: VerifyFailure | None = None


class SignedRequestEvidence(ContractModel):
    proof_jws: str
    proof_jti: OpaqueId
    body_sha256: OpaqueId
    action_hash: OpaqueId


class EvidenceBundle(ContractModel):
    request_id: OpaqueId
    mandate: MandateSnapshot
    agent: AgentPublicIdentity
    action: CanonicalAction
    signed_request: SignedRequestEvidence
    decisions: tuple[dict[str, Any], ...]
    approval_history: tuple[dict[str, Any], ...]
    bank_outcome: BankResult | None
    audit_references: tuple[AuditRef, ...]
    verification: VerifyReport
