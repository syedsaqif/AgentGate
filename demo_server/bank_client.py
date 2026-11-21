"""Client wrapper for communication with the simulated loopback bank."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import HTTPException


class BankClient:
    """Server-side client for the simulated bank service on port 8001."""

    def __init__(self, bank_origin: str, service_key: str, timeout: float = 5.0) -> None:
        self.bank_origin = bank_origin.rstrip("/")
        self.service_key = service_key
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "X-Bank-Service-Key": self.service_key,
            "Accept": "application/json",
        }

    async def get_balance(self, account_id: str, customer_id: str) -> dict[str, Any]:
        """Fetch account balance for the verified customer."""
        url = f"{self.bank_origin}/bank/accounts/{account_id}/balance"
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            try:
                resp = await client.get(
                    url,
                    headers=self._headers(),
                    params={"customer_id": customer_id},
                )
            except httpx.TransportError as exc:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "error": {
                            "code": "E_SERVICE_UNAVAILABLE",
                            "message": f"Bank service unreachable: {exc}",
                        }
                    },
                ) from exc

        if not resp.is_success:
            is_json = resp.headers.get("content-type", "").startswith("application/json")
            err = resp.json() if is_json else {}
            raise HTTPException(
                status_code=resp.status_code,
                detail=err or {"error": {"code": "E_REQ_INVALID", "message": resp.text}},
            )
        return resp.json()

    async def get_transactions(
        self,
        account_id: str,
        customer_id: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Fetch ledger entries for the verified customer."""
        url = f"{self.bank_origin}/bank/accounts/{account_id}/transactions"
        params: dict[str, Any] = {"customer_id": customer_id, "limit": limit}
        if cursor:
            params["cursor"] = cursor

        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            try:
                resp = await client.get(
                    url,
                    headers=self._headers(),
                    params=params,
                )
            except httpx.TransportError as exc:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "error": {
                            "code": "E_SERVICE_UNAVAILABLE",
                            "message": f"Bank service unreachable: {exc}",
                        }
                    },
                ) from exc

        if not resp.is_success:
            is_json = resp.headers.get("content-type", "").startswith("application/json")
            err = resp.json() if is_json else {}
            raise HTTPException(
                status_code=resp.status_code,
                detail=err or {"error": {"code": "E_REQ_INVALID", "message": resp.text}},
            )
        return resp.json()
