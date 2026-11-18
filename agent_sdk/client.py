"""Member B authenticated AgentGate HTTP client."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from types import TracebackType
from typing import Any

import httpx
from pydantic import BaseModel

from contracts.common import Clock, Currency
from contracts.core_models import Intent

from .keys import AgentKeyPair
from .polling import (
    ACTION_TERMINAL_STATES,
    ESCALATION_TERMINAL_STATES,
    PollResult,
    poll_action,
    poll_escalation,
)
from .proofs import normalize_gateway_origin
from .retry import RetryPolicy, SleepFn, execute_with_fresh_proof_retry
from .serialization import (
    MoneyActionPayload,
    new_idempotency_key,
    serialize_json_body,
)
from .tokens import CachedAccessToken, TokenManager


def _decode_json_dict(response: httpx.Response) -> dict[str, Any]:
    parsed: Any = response.json()
    if not isinstance(parsed, dict):
        raise ValueError("Expected JSON object response from AgentGate gateway")
    return parsed


class AgentGateClient:
    """High-level async AgentGate SDK client with Ed25519 DPoP proofs and safe retries."""

    def __init__(
        self,
        *,
        gateway_url: str,
        agent_id: str,
        mandate_id: str,
        keypair: AgentKeyPair,
        clock: Clock | None = None,
        retry_policy: RetryPolicy | None = None,
        sleep_fn: SleepFn = asyncio.sleep,
        http_client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 15.0,
        refresh_leeway_seconds: int = 15,
    ) -> None:
        self._gateway_origin = normalize_gateway_origin(gateway_url)
        self._agent_id = agent_id
        self._mandate_id = mandate_id
        self._keypair = keypair
        self._clock = clock
        self._retry_policy = retry_policy or RetryPolicy()
        self._sleep_fn = sleep_fn
        self._owns_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient(
            transport=transport,
            timeout=timeout_seconds,
        )
        self._token_manager = TokenManager(
            gateway_origin=self._gateway_origin,
            agent_id=agent_id,
            mandate_id=mandate_id,
            keypair=keypair,
            clock=clock,
            refresh_leeway_seconds=refresh_leeway_seconds,
        )

    @property
    def gateway_origin(self) -> str:
        return self._gateway_origin

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def mandate_id(self) -> str:
        return self._mandate_id

    @property
    def token_manager(self) -> TokenManager:
        return self._token_manager

    async def __aenter__(self) -> AgentGateClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http_client.aclose()

    async def fetch_token(self) -> CachedAccessToken:
        """Explicitly fetch (or refresh) a key-bound DPoP access token."""

        return await self._token_manager.fetch_token(self._http_client)

    async def request_signed(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | BaseModel | None = None,
        params: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> httpx.Response:
        """Serialize body once, sign with fresh DPoP proof, and execute with safe retries."""

        serialized = serialize_json_body(json_body)
        return await execute_with_fresh_proof_retry(
            http_client=self._http_client,
            token_manager=self._token_manager,
            keypair=self._keypair,
            gateway_origin=self._gateway_origin,
            method=method,
            path=path,
            serialized_body=serialized,
            params=params,
            idempotency_key=idempotency_key,
            extra_headers=extra_headers,
            retry_policy=retry_policy or self._retry_policy,
            clock=self._clock,
            sleep_fn=self._sleep_fn,
        )

    async def get_balance(self, account_id: str) -> dict[str, Any]:
        """Signed `GET /v1/accounts/{account_id}/balance` with empty-body `bsh`."""

        response = await self.request_signed("GET", f"/v1/accounts/{account_id}/balance")
        return _decode_json_dict(response)

    async def list_transactions(
        self,
        account_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Signed `GET /v1/accounts/{account_id}/transactions` (`htu` excludes query params)."""

        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        response = await self.request_signed(
            "GET",
            f"/v1/accounts/{account_id}/transactions",
            params=params,
        )
        return _decode_json_dict(response)

    async def _money_action(
        self,
        path: str,
        *,
        from_account: str,
        payee_id: str,
        amount_paise: int,
        currency: Currency,
        reference: str | None,
        task: str,
        category: str,
        note: str | None,
        idempotency_key: str | None,
        key_prefix: str,
    ) -> dict[str, Any]:
        payload = MoneyActionPayload(
            from_account=from_account,
            payee_id=payee_id,
            amount_paise=amount_paise,
            currency=currency,
            reference=reference,
            intent=Intent(task=task, category=category, note=note),
        )
        stable_key = (
            idempotency_key
            if idempotency_key is not None
            else new_idempotency_key(prefix=key_prefix)
        )
        response = await self.request_signed(
            "POST",
            path,
            json_body=payload,
            idempotency_key=stable_key,
        )
        return _decode_json_dict(response)

    async def pay_bill(
        self,
        *,
        from_account: str,
        payee_id: str,
        amount_paise: int,
        task: str,
        category: str,
        currency: Currency = "INR",
        reference: str | None = None,
        note: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Signed `POST /v1/payments/bills` with strict integer paise and stable idempotency key."""

        return await self._money_action(
            "/v1/payments/bills",
            from_account=from_account,
            payee_id=payee_id,
            amount_paise=amount_paise,
            currency=currency,
            reference=reference,
            task=task,
            category=category,
            note=note,
            idempotency_key=idempotency_key,
            key_prefix="bill",
        )

    async def transfer(
        self,
        *,
        from_account: str,
        payee_id: str,
        amount_paise: int,
        task: str,
        category: str,
        currency: Currency = "INR",
        reference: str | None = None,
        note: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Signed `POST /v1/transfers` with strict integer paise and stable idempotency key."""

        return await self._money_action(
            "/v1/transfers",
            from_account=from_account,
            payee_id=payee_id,
            amount_paise=amount_paise,
            currency=currency,
            reference=reference,
            task=task,
            category=category,
            note=note,
            idempotency_key=idempotency_key,
            key_prefix="xfer",
        )

    async def get_action(self, request_id: str) -> dict[str, Any]:
        """Signed `GET /v1/actions/{request_id}` status lookup."""

        response = await self.request_signed("GET", f"/v1/actions/{request_id}")
        return _decode_json_dict(response)

    async def get_escalation(self, escalation_id: str) -> dict[str, Any]:
        """Signed `GET /v1/escalations/{escalation_id}` approval status lookup."""

        response = await self.request_signed("GET", f"/v1/escalations/{escalation_id}")
        return _decode_json_dict(response)

    async def poll_action(
        self,
        request_id: str,
        *,
        interval_seconds: float = 2.0,
        max_attempts: int = 30,
        terminal_states: frozenset[str] = ACTION_TERMINAL_STATES,
    ) -> PollResult:
        """Poll `GET /v1/actions/{request_id}` until terminal state or timeout."""

        return await poll_action(
            self,
            request_id,
            interval_seconds=interval_seconds,
            max_attempts=max_attempts,
            terminal_states=terminal_states,
            sleep_fn=self._sleep_fn,
        )

    async def poll_escalation(
        self,
        escalation_id: str,
        *,
        interval_seconds: float = 2.0,
        max_attempts: int = 30,
        terminal_states: frozenset[str] = ESCALATION_TERMINAL_STATES,
    ) -> PollResult:
        """Poll `GET /v1/escalations/{escalation_id}` until terminal state or timeout."""

        return await poll_escalation(
            self,
            escalation_id,
            interval_seconds=interval_seconds,
            max_attempts=max_attempts,
            terminal_states=terminal_states,
            sleep_fn=self._sleep_fn,
        )


__all__ = ["AgentGateClient"]
