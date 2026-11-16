"""Member A owned core domain DTOs; no persistence or policy logic."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StrictInt

from .common import (
    ActionType,
    AuditRef,
    ContractModel,
    Currency,
    OpaqueId,
    Paise,
    PolicyVerdict,
    PositivePaise,
)


class MandateStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"
    SUSPENDED = "SUSPENDED"
    EXPIRED = "EXPIRED"


class ReservationState(StrEnum):
    RESERVED = "RESERVED"
    COMMITTED = "COMMITTED"
    RELEASED = "RELEASED"
    UNKNOWN = "UNKNOWN"


class BankStatus(StrEnum):
    EXECUTED = "EXECUTED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class ActionStatus(StrEnum):
    RECEIVED = "RECEIVED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    DISPATCHED = "DISPATCHED"
    EXECUTED = "EXECUTED"
    REJECTED = "REJECTED"
    EXECUTION_UNKNOWN = "EXECUTION_UNKNOWN"
    FAILED = "FAILED"


class TimeWindow(ContractModel):
    timezone: Literal["Asia/Kolkata"] = "Asia/Kolkata"
    weekdays: Annotated[tuple[Annotated[StrictInt, Field(ge=1, le=7)], ...], Field(min_length=1)]
    start_local: Annotated[str, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")]
    end_local: Annotated[str, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")]


class Intent(ContractModel):
    task: OpaqueId
    category: OpaqueId
    note: Annotated[str, Field(max_length=500)] | None = None


class ActionParams(ContractModel):
    from_account: OpaqueId
    payee_id: OpaqueId | None = None
    amount_paise: Paise = 0
    currency: Currency = "INR"
    reference: Annotated[str, Field(max_length=120)] | None = None


class CanonicalAction(ContractModel):
    request_id: OpaqueId
    customer_id: OpaqueId
    agent_id: OpaqueId
    mandate_id: OpaqueId
    mandate_hash: OpaqueId
    action_type: ActionType
    params: ActionParams
    intent: Intent
    idempotency_key: OpaqueId
    received_at: datetime


class StepUpRules(ContractModel):
    amount_above_paise: Paise | None = None
    new_payee_requires_approval: bool = True
    always_ask_actions: tuple[ActionType, ...] = ()


class Grant(ContractModel):
    grant_id: OpaqueId
    action: ActionType
    account_ids: Annotated[tuple[OpaqueId, ...], Field(min_length=1)]
    payee_ids: tuple[OpaqueId, ...] = ()
    currency: Currency = "INR"
    max_per_txn_paise: PositivePaise | None = None
    max_total_paise: PositivePaise | None = None
    max_count: Annotated[StrictInt, Field(gt=0)] | None = None
    window: Literal["mandate"] = "mandate"
    category: str | None = None
    time_window: TimeWindow | None = None


class MandateTerms(ContractModel):
    mandate_id: OpaqueId
    version: Annotated[StrictInt, Field(ge=1)]
    customer_id: OpaqueId
    agent_id: OpaqueId
    valid_from: datetime
    valid_until: datetime
    grants: Annotated[tuple[Grant, ...], Field(min_length=1)]
    step_up: StepUpRules = StepUpRules()
    risk_profile: Literal["balanced", "conservative", "permissive"] = "balanced"


class MandateSnapshot(ContractModel):
    terms: MandateTerms
    status: MandateStatus
    mandate_hash: OpaqueId
    sig: OpaqueId
    key_id: OpaqueId


class UsageSnapshot(ContractModel):
    mandate_id: OpaqueId
    grant_id: OpaqueId
    window_key: OpaqueId
    committed_paise: Paise
    reserved_paise: Paise
    committed_count: Annotated[StrictInt, Field(ge=0)]
    reserved_count: Annotated[StrictInt, Field(ge=0)]
    remaining_paise: Paise | None = None
    remaining_count: Annotated[StrictInt, Field(ge=0)] | None = None


class PolicyResult(ContractModel):
    verdict: PolicyVerdict
    reason_codes: tuple[str, ...]
    matched_grant_ids: tuple[OpaqueId, ...] = ()
    step_up_reasons: tuple[str, ...] = ()
    usage_snapshot: tuple[UsageSnapshot, ...] = ()


class Reservation(ContractModel):
    reservation_id: OpaqueId
    request_id: OpaqueId
    grant_window_keys: Annotated[tuple[OpaqueId, ...], Field(min_length=1)]
    amount_paise: Paise
    count: Annotated[StrictInt, Field(ge=0)]
    state: ReservationState


class BankResult(ContractModel):
    status: BankStatus
    bank_operation_id: str | None = None
    bank_idempotency_key: OpaqueId
    reason_code: str | None = None
    balance_paise: Paise | None = None
    executed_at: datetime | None = None


class ExecutionResult(ContractModel):
    request_id: OpaqueId
    status: ActionStatus
    bank_result: BankResult | None = None
    audit_ref: AuditRef
    escalation_id: str | None = None


class AccountCard(ContractModel):
    account_id: OpaqueId
    masked_label: OpaqueId
    customer_id: OpaqueId


class PayeeCard(ContractModel):
    payee_id: OpaqueId
    display_name: OpaqueId
    category: OpaqueId
    verified: bool
