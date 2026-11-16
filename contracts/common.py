"""Member A owned common contract primitives; frozen after IC0."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictInt

CONTRACT_VERSION = "1.0.0"

type OpaqueId = Annotated[str, Field(min_length=1)]
type Paise = Annotated[StrictInt, Field(ge=0)]
type PositivePaise = Annotated[StrictInt, Field(gt=0)]
type Currency = Literal["INR"]


class ContractModel(BaseModel):
    """Base for serialized contract DTOs."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)


class ActionType(StrEnum):
    BILL_PAY = "bill_pay"
    TRANSFER = "transfer"
    BALANCE_READ = "balance_read"
    TRANSACTIONS_READ = "transactions_read"


class PolicyVerdict(StrEnum):
    ALLOW = "ALLOW"
    STEP_UP = "STEP_UP"
    DENY = "DENY"


class FinalDecision(StrEnum):
    ALLOW = "ALLOW"
    ESCALATE = "ESCALATE"
    DENY = "DENY"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class HumanRole(StrEnum):
    CUSTOMER = "customer"
    OPS = "ops"
    REGISTRAR = "registrar"


class ErrorDetail(ContractModel):
    code: str
    message: str
    request_id: OpaqueId


class ErrorEnvelope(ContractModel):
    error: ErrorDetail


class AuditRef(ContractModel):
    seq: Annotated[StrictInt, Field(ge=1)]
    entry_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Page(ContractModel):
    items: tuple[Any, ...] = ()
    next_cursor: str | None = None


class Clock(Protocol):
    def now(self) -> datetime: ...


class Tx(Protocol):
    """Opaque caller-owned unit-of-work marker; providers must not commit it."""

    @property
    def id(self) -> str: ...


class UnitOfWork(Protocol):
    async def __aenter__(self) -> Tx: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> bool | None: ...
