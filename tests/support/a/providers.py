"""Member A negative-capable bank substitute."""

from dataclasses import dataclass, field

from contracts.core_models import BankResult, BankStatus, CanonicalAction


@dataclass
class FakeBank:
    outcome: BankStatus = BankStatus.EXECUTED
    calls: list[str] = field(default_factory=list)

    async def pay_bill(self, action: CanonicalAction, bank_idempotency_key: str) -> BankResult:
        self.calls.append(action.request_id)
        return BankResult(status=self.outcome, bank_idempotency_key=bank_idempotency_key)

    async def transfer(self, action: CanonicalAction, bank_idempotency_key: str) -> BankResult:
        self.calls.append(action.request_id)
        return BankResult(status=self.outcome, bank_idempotency_key=bank_idempotency_key)

    async def lookup(self, bank_idempotency_key: str) -> BankResult:
        return BankResult(status=self.outcome, bank_idempotency_key=bank_idempotency_key)
