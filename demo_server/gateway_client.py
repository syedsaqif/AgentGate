"""Client wrapper for communication with the AgentGate gateway service."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import httpx
from fastapi import HTTPException


class GatewayClient:
    """Server-side client for proxying and integrating with the AgentGate gateway on port 8000."""

    def __init__(
        self,
        gateway_origin: str,
        ops_id: str = "ops_admin",
        ops_password: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.gateway_origin = gateway_origin.rstrip("/")
        self.ops_id = ops_id
        self.ops_password = ops_password
        self.timeout = timeout
        self._ops_token: str | None = None
        self._ops_token_expires_at: float = 0.0

    async def read_payment_evidence(self, path: str, token: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            try:
                response = await client.get(
                    self.gateway_origin + path, headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as error:
                raise HTTPException(
                    503,
                    detail={
                        "error": {
                            "code": "E_SERVICE_UNAVAILABLE",
                            "message": f"Gateway service unreachable: {error}",
                        }
                    },
                ) from error
        try:
            data = response.json()
        except ValueError:
            data = None
        if not response.is_success:
            err_detail = data if isinstance(data, dict) else {
                "error": {
                    "code": "E_REQ_INVALID" if response.status_code == 400 else "E_SERVICE_UNAVAILABLE",
                    "message": f"Gateway returned {response.status_code}: {data or 'Payment evidence unavailable.'}",
                }
            }
            raise HTTPException(response.status_code, detail=err_detail)
        if not isinstance(data, dict):
            raise HTTPException(
                502,
                detail={
                    "error": {
                        "code": "UI_INVALID_RESPONSE",
                        "message": "Gateway returned invalid payment evidence.",
                    }
                },
            )
        return data

    async def customer_login(self, customer_id: str, pin: str) -> dict[str, Any]:
        """Authenticate customer credentials against Gateway /v1/customer/login.

        PIN is strictly passed in the request body and never retained.
        """
        url = f"{self.gateway_origin}/v1/customer/login"
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            try:
                resp = await client.post(
                    url,
                    json={"customer_id": customer_id, "pin": pin},
                )
            except httpx.TransportError as exc:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "error": {
                            "code": "E_SERVICE_UNAVAILABLE",
                            "message": f"Gateway service unreachable: {exc}",
                        }
                    },
                ) from exc

        if not resp.is_success:
            err = (
                resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            raise HTTPException(
                status_code=resp.status_code,
                detail=err or {"error": {"code": "E_AUTH_MISSING", "message": resp.text}},
            )
        session = resp.json()
        if "session_token" in session:
            session.setdefault("token", session["session_token"])
        return session

    async def get_customer_bootstrap(self, gateway_token: str) -> dict[str, Any]:
        """Fetch bootstrap accounts, payees, and agents for customer."""
        url = f"{self.gateway_origin}/v1/customer/bootstrap"
        headers = {"Authorization": f"Bearer {gateway_token}"}
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            try:
                resp = await client.get(url, headers=headers)
            except httpx.TransportError as exc:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "error": {
                            "code": "E_SERVICE_UNAVAILABLE",
                            "message": f"Gateway service unreachable: {exc}",
                        }
                    },
                ) from exc

        if not resp.is_success:
            err = (
                resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            raise HTTPException(
                status_code=resp.status_code,
                detail=err or {"error": {"code": "E_REQ_INVALID", "message": resp.text}},
            )
        return resp.json()

    async def list_customer_mandates(
        self,
        gateway_token: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Fetch mandates owned by customer."""
        url = f"{self.gateway_origin}/v1/customer/mandates"
        headers = {"Authorization": f"Bearer {gateway_token}"}
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor

        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            try:
                resp = await client.get(url, headers=headers, params=params)
            except httpx.TransportError as exc:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "error": {
                            "code": "E_SERVICE_UNAVAILABLE",
                            "message": f"Gateway service unreachable: {exc}",
                        }
                    },
                ) from exc

        if not resp.is_success:
            err = (
                resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            raise HTTPException(
                status_code=resp.status_code,
                detail=err or {"error": {"code": "E_REQ_INVALID", "message": resp.text}},
            )
        return resp.json()

    async def create_customer_mandate(
        self,
        gateway_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a new signed mandate via Gateway."""
        url = f"{self.gateway_origin}/v1/customer/mandates"
        headers = {"Authorization": f"Bearer {gateway_token}"}
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            try:
                resp = await client.post(url, headers=headers, json=payload)
            except httpx.TransportError as exc:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "error": {
                            "code": "E_SERVICE_UNAVAILABLE",
                            "message": f"Gateway service unreachable: {exc}",
                        }
                    },
                ) from exc

        if not resp.is_success:
            err = (
                resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            raise HTTPException(
                status_code=resp.status_code,
                detail=err or {"error": {"code": "E_REQ_INVALID", "message": resp.text}},
            )
        return resp.json()

    async def revoke_customer_mandate(
        self,
        gateway_token: str,
        mandate_id: str,
    ) -> dict[str, Any]:
        """Revoke a customer mandate via Gateway."""
        url = f"{self.gateway_origin}/v1/customer/mandates/{mandate_id}/revoke"
        headers = {"Authorization": f"Bearer {gateway_token}"}
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            try:
                resp = await client.post(url, headers=headers)
            except httpx.TransportError as exc:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "error": {
                            "code": "E_SERVICE_UNAVAILABLE",
                            "message": f"Gateway service unreachable: {exc}",
                        }
                    },
                ) from exc

        if not resp.is_success:
            err = (
                resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            raise HTTPException(
                status_code=resp.status_code,
                detail=err or {"error": {"code": "E_REQ_INVALID", "message": resp.text}},
            )
        return resp.json()

    async def list_customer_escalations(
        self,
        gateway_token: str,
        status_filter: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Fetch customer escalation items (approvals inbox)."""
        url = f"{self.gateway_origin}/v1/customer/escalations"
        headers = {"Authorization": f"Bearer {gateway_token}"}
        params: dict[str, Any] = {"limit": limit}
        if status_filter:
            params["status"] = status_filter
        if cursor:
            params["cursor"] = cursor

        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            try:
                resp = await client.get(url, headers=headers, params=params)
            except httpx.TransportError as exc:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "error": {
                            "code": "E_SERVICE_UNAVAILABLE",
                            "message": f"Gateway service unreachable: {exc}",
                        }
                    },
                ) from exc

        if not resp.is_success:
            err = (
                resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            raise HTTPException(
                status_code=resp.status_code,
                detail=err or {"error": {"code": "E_REQ_INVALID", "message": resp.text}},
            )
        return resp.json()

    async def submit_approval_decision(
        self,
        gateway_token: str,
        escalation_id: str,
        decision: str,
        pin: str | None = None,
        suspend_agent: bool = False,
    ) -> dict[str, Any]:
        """Submit customer decision on an escalation.

        PIN is used strictly for this call and never retained or logged.
        """
        url = f"{self.gateway_origin}/v1/customer/escalations/{escalation_id}/decision"
        headers = {"Authorization": f"Bearer {gateway_token}"}
        payload: dict[str, Any] = {
            "decision": {"APPROVED": "APPROVE", "REJECTED": "REJECT"}.get(decision, decision),
            "suspend_agent": suspend_agent,
        }
        if pin:
            payload["pin"] = pin

        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            try:
                resp = await client.post(url, headers=headers, json=payload)
            except httpx.TransportError as exc:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "error": {
                            "code": "E_SERVICE_UNAVAILABLE",
                            "message": f"Gateway service unreachable: {exc}",
                        }
                    },
                ) from exc

        if not resp.is_success:
            err = (
                resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            raise HTTPException(
                status_code=resp.status_code,
                detail=err or {"error": {"code": "E_REQ_INVALID", "message": resp.text}},
            )
        return resp.json()

    async def get_ops_token(self) -> str:
        """Obtain or return cached valid operator token from Gateway."""
        now = time.time()
        if self._ops_token and now < (self._ops_token_expires_at - 60):
            return self._ops_token

        if not self.ops_password:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": {
                        "code": "E_SERVICE_UNAVAILABLE",
                        "message": "D06_OPS_PASSWORD not configured for server-side ops relay",
                    }
                },
            )

        url = f"{self.gateway_origin}/v1/ops/login"
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            try:
                resp = await client.post(
                    url,
                    json={"operator_id": self.ops_id, "password": self.ops_password},
                )
            except httpx.TransportError as exc:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "error": {
                            "code": "E_SERVICE_UNAVAILABLE",
                            "message": f"Gateway service unreachable for ops login: {exc}",
                        }
                    },
                ) from exc

        if not resp.is_success:
            err = (
                resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            raise HTTPException(
                status_code=resp.status_code,
                detail=err or {"error": {"code": "E_AUTH_MISSING", "message": resp.text}},
            )

        data = resp.json()
        token = data.get("session_token") or data.get("token")
        if not token:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": {
                        "code": "E_AUTH_MISSING",
                        "message": "No token in ops login response",
                    }
                },
            )

        self._ops_token = token
        expires_at = data.get("expires_at")
        self._ops_token_expires_at = (
            datetime.fromisoformat(expires_at).timestamp() if expires_at else now + 3600
        )
        return self._ops_token

    async def reinstate_agent(self, agent_id: str) -> dict[str, Any]:
        """Reinstate a suspended agent via Gateway ops endpoint."""
        ops_token = await self.get_ops_token()
        url = f"{self.gateway_origin}/v1/ops/agents/{agent_id}/reinstate"
        headers = {"Authorization": f"Bearer {ops_token}"}
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            try:
                resp = await client.post(url, headers=headers)
            except httpx.TransportError as exc:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "error": {
                            "code": "E_SERVICE_UNAVAILABLE",
                            "message": f"Gateway service unreachable: {exc}",
                        }
                    },
                ) from exc

        if not resp.is_success:
            err = (
                resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            raise HTTPException(
                status_code=resp.status_code,
                detail=err or {"error": {"code": "E_REQ_INVALID", "message": resp.text}},
            )
        return resp.json()
