"""Member A private HTTP bank adapter conforming to BankClient protocol."""

from __future__ import annotations

import httpx

from contracts.core_models import BankResult, BankStatus, CanonicalAction
from contracts.core_ports import BankClient
from contracts.reason_codes import ReasonCode
from gateway.errors import AgentGateError


class HttpBankClient(BankClient):
    """Client communicating with the private bank mock over HTTP or ASGI transport."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8001",
        service_key: str = "local-bank-dev-secret",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.base_url = base_url
        self.service_key = service_key
        self.transport = transport
        self.timeout = timeout

    def _client(self) -> httpx.AsyncClient:
        headers = {
            "X-Bank-Service-Key": self.service_key,
            "Content-Type": "application/json",
        }
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            transport=self.transport,
            timeout=self.timeout,
        )

    async def pay_bill(
        self, action: CanonicalAction, bank_idempotency_key: str
    ) -> BankResult:
        payload = {
            "customer_id": action.customer_id,
            "from_account": action.params.from_account,
            "payee_id": action.params.payee_id,
            "amount_paise": action.params.amount_paise,
            "currency": action.params.currency,
            "reference": action.params.reference,
            "bank_idempotency_key": bank_idempotency_key,
        }

        async with self._client() as client:
            try:
                res = await client.post("/bank/payments", json=payload)
            except (httpx.ConnectError, httpx.TimeoutException):
                return BankResult(
                    status=BankStatus.UNKNOWN,
                    bank_idempotency_key=bank_idempotency_key,
                    reason_code=ReasonCode.E_BANK_UNAVAILABLE.value,
                )

        if res.status_code == 200:
            return BankResult.model_validate(res.json())
        if res.status_code == 422:
            body = res.json().get("error", {})
            return BankResult(
                status=BankStatus.REJECTED,
                bank_idempotency_key=bank_idempotency_key,
                reason_code=body.get("code", ReasonCode.E_BANK_INSUFFICIENT_FUNDS.value),
            )
        if res.status_code == 409:
            raise AgentGateError(
                code=ReasonCode.E_REQ_IDEMPOTENCY_CONFLICT,
                message="Bank idempotency conflict: key reused with different parameters",
                status_code=409,
            )
        if res.status_code in (401, 403):
            raise AgentGateError(
                code=ReasonCode.E_AUTH_MISSING,
                message="Bank service authentication failed",
                status_code=502,
            )

        return BankResult(
            status=BankStatus.UNKNOWN,
            bank_idempotency_key=bank_idempotency_key,
            reason_code=ReasonCode.E_BANK_UNAVAILABLE.value,
        )

    async def transfer(
        self, action: CanonicalAction, bank_idempotency_key: str
    ) -> BankResult:
        payload = {
            "customer_id": action.customer_id,
            "from_account": action.params.from_account,
            "to_account": action.params.payee_id or "ACC-UNKNOWN",
            "amount_paise": action.params.amount_paise,
            "currency": action.params.currency,
            "reference": action.params.reference,
            "bank_idempotency_key": bank_idempotency_key,
        }

        async with self._client() as client:
            try:
                res = await client.post("/bank/transfers", json=payload)
            except (httpx.ConnectError, httpx.TimeoutException):
                return BankResult(
                    status=BankStatus.UNKNOWN,
                    bank_idempotency_key=bank_idempotency_key,
                    reason_code=ReasonCode.E_BANK_UNAVAILABLE.value,
                )

        if res.status_code == 200:
            return BankResult.model_validate(res.json())
        if res.status_code == 422:
            body = res.json().get("error", {})
            return BankResult(
                status=BankStatus.REJECTED,
                bank_idempotency_key=bank_idempotency_key,
                reason_code=body.get("code", ReasonCode.E_BANK_INSUFFICIENT_FUNDS.value),
            )
        if res.status_code == 409:
            raise AgentGateError(
                code=ReasonCode.E_REQ_IDEMPOTENCY_CONFLICT,
                message="Bank idempotency conflict: key reused with different parameters",
                status_code=409,
            )
        if res.status_code in (401, 403):
            raise AgentGateError(
                code=ReasonCode.E_AUTH_MISSING,
                message="Bank service authentication failed",
                status_code=502,
            )

        return BankResult(
            status=BankStatus.UNKNOWN,
            bank_idempotency_key=bank_idempotency_key,
            reason_code=ReasonCode.E_BANK_UNAVAILABLE.value,
        )

    async def lookup(self, bank_idempotency_key: str) -> BankResult:
        async with self._client() as client:
            try:
                res = await client.get(f"/bank/operations/{bank_idempotency_key}")
            except (httpx.ConnectError, httpx.TimeoutException):
                return BankResult(
                    status=BankStatus.UNKNOWN,
                    bank_idempotency_key=bank_idempotency_key,
                    reason_code=ReasonCode.E_BANK_UNAVAILABLE.value,
                )

        if res.status_code == 200:
            return BankResult.model_validate(res.json())
        if res.status_code == 404:
            return BankResult(
                status=BankStatus.UNKNOWN,
                bank_idempotency_key=bank_idempotency_key,
                reason_code="E_BANK_OP_NOT_FOUND",
            )
        return BankResult(
            status=BankStatus.UNKNOWN,
            bank_idempotency_key=bank_idempotency_key,
            reason_code=ReasonCode.E_BANK_UNAVAILABLE.value,
        )
