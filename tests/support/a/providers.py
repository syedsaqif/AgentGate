"""Member A negative-capable bank substitute conforming to BankClient protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from contracts.core_models import BankResult, BankStatus, CanonicalAction
from contracts.core_ports import BankClient
from contracts.reason_codes import ReasonCode
from gateway.errors import AgentGateError


@dataclass
class FakeBank(BankClient):
    """Configurable test double for bank operations supporting both success and fault injection."""

    outcome: BankStatus = BankStatus.EXECUTED
    fail_insufficient_funds: bool = False
    fail_idempotency_conflict: bool = False
    fail_unavailable: bool = False
    timeout: bool = False

    calls: list[str] = field(default_factory=list)
    recorded_operations: dict[str, BankResult] = field(default_factory=dict)

    __is_fake__: bool = True

    async def pay_bill(
        self, action: CanonicalAction, bank_idempotency_key: str
    ) -> BankResult:
        self.calls.append(action.request_id)

        if self.fail_idempotency_conflict:
            raise AgentGateError(
                code=ReasonCode.E_REQ_IDEMPOTENCY_CONFLICT,
                message="Simulated idempotency conflict",
                status_code=409,
            )

        if self.fail_unavailable:
            raise AgentGateError(
                code=ReasonCode.E_BANK_UNAVAILABLE,
                message="Simulated bank outage",
                status_code=502,
            )

        if self.timeout:
            result = BankResult(
                status=BankStatus.UNKNOWN,
                bank_idempotency_key=bank_idempotency_key,
                reason_code=ReasonCode.E_BANK_UNAVAILABLE.value,
            )
            self.recorded_operations[bank_idempotency_key] = result
            return result

        if self.fail_insufficient_funds:
            result = BankResult(
                status=BankStatus.REJECTED,
                bank_idempotency_key=bank_idempotency_key,
                reason_code=ReasonCode.E_BANK_INSUFFICIENT_FUNDS.value,
            )
            self.recorded_operations[bank_idempotency_key] = result
            return result

        # Normal success
        result = BankResult(
            status=self.outcome,
            bank_operation_id=f"fake_op_{len(self.calls)}",
            bank_idempotency_key=bank_idempotency_key,
            balance_paise=12315000,
            executed_at=datetime.now(UTC),
        )
        self.recorded_operations[bank_idempotency_key] = result
        return result

    async def transfer(
        self, action: CanonicalAction, bank_idempotency_key: str
    ) -> BankResult:
        return await self.pay_bill(action, bank_idempotency_key)

    async def lookup(self, bank_idempotency_key: str) -> BankResult:
        if bank_idempotency_key in self.recorded_operations:
            return self.recorded_operations[bank_idempotency_key]
        return BankResult(
            status=self.outcome,
            bank_idempotency_key=bank_idempotency_key,
        )
