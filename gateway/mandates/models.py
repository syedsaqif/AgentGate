"""Member A mandate API schemas and protocols."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from contracts.common import AuditRef, ContractModel, OpaqueId
from contracts.core_models import (
    AccountCard,
    Grant,
    MandateSnapshot,
    MandateStatus,
    MandateTerms,
    PayeeCard,
    StepUpRules,
    UsageSnapshot,
)


class Catalog(Protocol):
    """Protocol for resolving trusted accounts and payees."""

    def get_account(self, account_id: str) -> AccountCard | None: ...
    def get_payee(self, payee_id: str) -> PayeeCard | None: ...
    async def list_accounts_for_customer(self, customer_id: str) -> tuple[AccountCard, ...]: ...
    async def list_payees(self) -> tuple[PayeeCard, ...]: ...


@dataclass
class DefaultCatalog(Catalog):
    """Production/default catalog provider resolving trusted accounts and payees."""

    accounts: dict[str, AccountCard] = field(default_factory=dict)
    payees: dict[str, PayeeCard] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.accounts:
            self.accounts["ACC-001"] = AccountCard(
                account_id="ACC-001",
                masked_label="Savings Account (...001)",
                customer_id="cust_001",
            )
            self.accounts["ACC-002"] = AccountCard(
                account_id="ACC-002",
                masked_label="Second Account (...002)",
                customer_id="cust_002",
            )
        if not self.payees:
            self.payees["BILL-ELEC"] = PayeeCard(
                payee_id="BILL-ELEC",
                display_name="Electricity Board",
                category="utilities.electricity",
                verified=True,
            )
            self.payees["BILL-MOBILE"] = PayeeCard(
                payee_id="BILL-MOBILE",
                display_name="Mobile Telecom",
                category="utilities.telecom",
                verified=True,
            )
            self.payees["BILL-INS"] = PayeeCard(
                payee_id="BILL-INS",
                display_name="Insurance Corp",
                category="financial.insurance",
                verified=True,
            )
            self.payees["PAYEE-X"] = PayeeCard(
                payee_id="PAYEE-X",
                display_name="Unverified Payee X",
                category="personal.transfer",
                verified=False,
            )
            self.payees["PAYEE-CRYPTO"] = PayeeCard(
                payee_id="PAYEE-CRYPTO",
                display_name="Crypto Exchange",
                category="investment.crypto",
                verified=False,
            )

    def get_account(self, account_id: str) -> AccountCard | None:
        return self.accounts.get(account_id)

    def get_payee(self, payee_id: str) -> PayeeCard | None:
        return self.payees.get(payee_id)

    async def list_accounts_for_customer(self, customer_id: str) -> tuple[AccountCard, ...]:
        return tuple(acc for acc in self.accounts.values() if acc.customer_id == customer_id)

    async def list_payees(self) -> tuple[PayeeCard, ...]:
        return tuple(self.payees.values())


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
    terms: MandateTerms
    status: MandateStatus
    mandate_hash: str
    sig: str
    key_id: str
    usage: tuple[UsageSnapshot, ...]
    mandate: MandateSnapshot | None = None


class CustomerMandateListResponse(ContractModel):
    items: tuple[CustomerMandateItem, ...]
    next_cursor: str | None = None


class RevokeMandateResponse(ContractModel):
    mandate_id: OpaqueId
    status: str = "REVOKED"
    audit_ref: AuditRef | None = None


CustomerMandateItem.model_rebuild()
CustomerMandateListResponse.model_rebuild()
