"""Member A mandate API schemas and protocols."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from contracts.common import AuditRef, ContractModel, OpaqueId
from contracts.core_models import (
    AccountCard,
    Grant,
    MandateSnapshot,
    PayeeCard,
    StepUpRules,
    UsageSnapshot,
)


class Catalog(Protocol):
    """Protocol for resolving trusted accounts and payees."""

    async def get_account(self, account_id: str) -> AccountCard | None: ...
    async def get_payee(self, payee_id: str) -> PayeeCard | None: ...


class CreateMandateRequest(BaseModel):
    """Customer input payload for creating a new mandate.
    
    Accepts only customer-selectable terms plus PIN.
    Server rejects client-supplied customer_id, mandate_id, status, hashes, or signatures.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: OpaqueId
    valid_from: datetime
    valid_until: datetime
    grants: Annotated[tuple[Grant, ...], Field(min_length=1)]
    step_up: StepUpRules = StepUpRules()
    risk_profile: Literal["balanced", "conservative", "permissive"] = "balanced"
    pin: Annotated[str, Field(min_length=4, max_length=16)]


class CreateMandateResponse(ContractModel):
    mandate: MandateSnapshot
    audit_ref: AuditRef


class CustomerMandateItem(ContractModel):
    mandate: MandateSnapshot
    usage: tuple[UsageSnapshot, ...]


class CustomerMandateListResponse(ContractModel):
    items: tuple[CustomerMandateItem, ...]
    next_cursor: str | None = None
