"""Member A bank mock request and response Pydantic models."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from contracts.common import Currency, OpaqueId, Paise, PositivePaise


class BankModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AccountBalanceResponse(BankModel):
    account_id: OpaqueId
    customer_id: OpaqueId
    balance_paise: Paise
    currency: Currency = "INR"


class LedgerEntryItem(BankModel):
    entry_id: OpaqueId
    account_id: OpaqueId
    entry_type: Literal["DEBIT", "CREDIT"]
    amount_paise: PositivePaise
    balance_after_paise: Paise
    reference: str | None = None
    created_at: datetime


class LedgerPageResponse(BankModel):
    items: tuple[LedgerEntryItem, ...]
    next_cursor: str | None = None


class PaymentRequest(BankModel):
    customer_id: OpaqueId
    from_account: OpaqueId
    payee_id: OpaqueId
    amount_paise: PositivePaise
    currency: Currency = "INR"
    reference: Annotated[str, Field(max_length=120)] | None = None
    bank_idempotency_key: OpaqueId


class TransferRequest(BankModel):
    customer_id: OpaqueId
    from_account: OpaqueId
    to_account: OpaqueId
    amount_paise: PositivePaise
    currency: Currency = "INR"
    reference: Annotated[str, Field(max_length=120)] | None = None
    bank_idempotency_key: OpaqueId
