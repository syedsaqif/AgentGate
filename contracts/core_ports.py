"""Member A owned core service protocols."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Protocol

from .common import OpaqueId, Tx
from .core_models import (
    BankResult,
    CanonicalAction,
    ExecutionResult,
    MandateSnapshot,
    PolicyResult,
    Reservation,
    UsageSnapshot,
)
from .escalation_models import Escalation


class MandateReader(Protocol):
    async def get(self, mandate_id: OpaqueId, tx: Tx | None = None) -> MandateSnapshot: ...


class UsageReader(Protocol):
    async def get(
        self, mandate_id: OpaqueId, tx: Tx | None = None
    ) -> tuple[UsageSnapshot, ...]: ...


class PolicyEvaluator(Protocol):
    def evaluate(
        self,
        action: CanonicalAction,
        mandate: MandateSnapshot,
        usage: tuple[UsageSnapshot, ...],
        now: datetime,
    ) -> PolicyResult: ...


class ActionReader(Protocol):
    async def get(self, request_id: OpaqueId, tx: Tx | None = None) -> CanonicalAction: ...


class ExecutionPort(Protocol):
    async def execute_stored(
        self, request_id: OpaqueId, escalation_id: OpaqueId | None = None
    ) -> ExecutionResult: ...


class ReservationPort(Protocol):
    async def reserve(self, action: CanonicalAction, tx: Tx) -> Reservation: ...

    async def commit(self, reservation_id: OpaqueId, tx: Tx) -> None: ...

    async def release(self, reservation_id: OpaqueId, tx: Tx) -> None: ...


class Lifecycle(Protocol):
    async def revoke_mandate(self, mandate_id: OpaqueId) -> None: ...

    async def suspend_agent(self, agent_id: OpaqueId, reason_codes: tuple[str, ...]) -> None: ...

    async def suspend_customer_delegation(
        self,
        customer_id: OpaqueId,
        agent_id: OpaqueId,
        reason_codes: tuple[str, ...],
        *,
        before_suspend: Callable[[Tx | None], Awaitable[Escalation]] | None = None,
    ) -> Escalation | None: ...

    async def reinstate_agent(self, agent_id: OpaqueId) -> None: ...


class BankClient(Protocol):
    async def pay_bill(self, action: CanonicalAction, bank_idempotency_key: str) -> BankResult: ...

    async def transfer(self, action: CanonicalAction, bank_idempotency_key: str) -> BankResult: ...

    async def lookup(self, bank_idempotency_key: str) -> BankResult: ...
