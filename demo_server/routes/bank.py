"""Demo server bank and customer API proxy routes."""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field

from demo_server.bank_client import BankClient
from demo_server.config import DemoServerConfig
from demo_server.gateway_client import GatewayClient
from demo_server.session import (
    DemoSession,
    get_current_demo_session,
    global_session_store,
)

router = APIRouter()
DEFAULT_VERIFIER_URL = "https://syedsaqif.github.io/AgentGate/v1/dispute-verifier-v1.html"


def configured_verifier_url() -> str:
    value = (
        os.getenv("AGENTGATE_DISPUTE_VERIFIER_URL")
        or os.getenv("VITE_DISPUTE_VERIFIER_URL")
        or DEFAULT_VERIFIER_URL
    )
    filename = "/dispute-verifier-v1.html"
    try:
        parsed = urlsplit(value)
        if (
            0 < len(value) <= 256 and re.fullmatch(r"[\x21-\x7e]+", value)
            and not any(char in value for char in "?#\\")
            and parsed.scheme == "https" and parsed.hostname and "%" not in parsed.netloc
            and parsed.username is None and parsed.password is None
            and parsed.port != 0 and parsed.path.endswith(filename)
        ):
            normalized = str(AnyHttpUrl(value))
            if len(normalized) <= 256 and urlsplit(normalized).path.endswith(filename):
                return normalized
    except ValueError:
        pass
    raise HTTPException(409, detail={
        "code": "UI_INVALID_CONFIG",
        "message": "Set a public, versioned HTTPS verifier URL without a query or fragment.",
    })


class CustomerLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(min_length=1, max_length=64)
    pin: str = Field(min_length=4, max_length=16)


class LoginResponse(BaseModel):
    session_id: str
    customer_id: str
    account_id: str
    expires_at: float


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str = Field(pattern=r"^(APPROVE|REJECT|APPROVED|REJECTED)$")
    pin: str | None = Field(default=None, min_length=4, max_length=16)
    suspend_agent: bool = False


def _get_gateway_client(request: Request) -> GatewayClient:
    return request.app.state.gateway_client


def _get_bank_client(request: Request) -> BankClient:
    return request.app.state.bank_client


def _get_config(request: Request) -> DemoServerConfig:
    return request.app.state.config


@router.get("/receipts/{request_id}/qr")
async def demo_receipt_qr(
    request_id: str,
    response: Response,
    session: Annotated[DemoSession, Depends(get_current_demo_session)],
    gateway: Annotated[GatewayClient, Depends(_get_gateway_client)],
) -> dict[str, Any]:
    not_found = HTTPException(404, detail={
        "code": "E_REQ_INVALID", "message": "Payment receipt not found.",
    })
    if not re.fullmatch(r"req_[a-zA-Z0-9_-]{4,64}", request_id):
        raise not_found
    try:
        action = await gateway.read_payment_evidence(
            f"/v1/actions/{request_id}", session.gateway_token
        )
    except HTTPException as error:
        if error.status_code in (403, 404):
            raise not_found from None
        raise
    if (
        action.get("request_id") != request_id or action.get("customer_id") != session.customer_id
        or action.get("from_account") != session.account_id
    ):
        raise not_found
    if (
        action.get("status") != "EXECUTED"
        or action.get("action_type") not in {"bill_pay", "transfer"}
    ):
        raise HTTPException(409, detail={
            "code": "E_REQ_INVALID",
            "message": "No payment was made, so there is nothing to prove.",
        })
    url = configured_verifier_url()
    token = await gateway.get_ops_token()
    proof = await gateway.read_payment_evidence(f"/v1/audit/proof/{request_id}", token)
    record, evidence, receipt = proof.get("record"), proof.get("evidence"), action.get("audit_ref")
    record = record if isinstance(record, dict) else {}
    evidence = evidence if isinstance(evidence, dict) else {}
    receipt = receipt if isinstance(receipt, dict) else {}
    seq, digest = evidence.get("receipt_seq"), evidence.get("receipt_hash")
    checkpoint = proof.get("witness_checkpoint")
    coverage = checkpoint.get("seq") if isinstance(checkpoint, dict) else None
    if (
        proof.get("format") != "agentgate-dispute-v1" or record.get("request_id") != request_id
        or type(seq) is not int or seq < 1 or seq != receipt.get("seq")
        or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
        or digest != receipt.get("entry_hash") or "witness_checkpoint" not in proof
        or (checkpoint is not None and (type(coverage) is not int or coverage <= seq))
    ):
        raise HTTPException(502, detail={
            "code": "UI_INVALID_RESPONSE",
            "message": "The proof did not match the payment receipt.",
        })
    response.headers["Cache-Control"] = "no-store"
    return {
        "request_id": request_id, "receipt_seq": seq, "receipt_hash": digest,
        "witness_coverage": coverage, "verifier_url": url,
    }


@router.post("/login", response_model=LoginResponse)
async def demo_customer_login(
    payload: CustomerLoginRequest,
    response: Response,
    request: Request,
    gw_client: Annotated[GatewayClient, Depends(_get_gateway_client)],
    config: Annotated[DemoServerConfig, Depends(_get_config)],
) -> LoginResponse:
    """Authenticate customer with Gateway and establish a bound demo session.

    The user-entered PIN is passed only for login verification and is never stored,
    logged, or echoed.
    """
    # 1. Login with Gateway
    gw_result = await gw_client.customer_login(
        customer_id=payload.customer_id,
        pin=payload.pin,
    )
    gateway_token = gw_result.get("session_token") or gw_result.get("token")
    if (
        not gateway_token or gw_result.get("role") != "customer"
        or gw_result.get("subject_id", gw_result.get("customer_id")) != payload.customer_id
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "E_AUTH_MISSING",
                    "message": "Gateway login did not return a session token",
                }
            },
        )

    # 2. Resolve primary customer account
    bootstrap = await gw_client.get_customer_bootstrap(gateway_token)
    accounts = [
        item["account_id"] for item in bootstrap.get("accounts", [])
        if item.get("customer_id", payload.customer_id) == payload.customer_id
    ]
    if not accounts:
        raise HTTPException(
            403, detail={"code": "E_REQ_INVALID", "message": "No account available"}
        )
    account_id = config.account_id if config.account_id in accounts else accounts[0]
    expires_at = gw_result.get("expires_at")
    ttl = int(datetime.fromisoformat(expires_at).timestamp() - time.time()) if expires_at else 3600
    if ttl <= 0:
        raise HTTPException(401, detail={"code": "E_AUTH_EXPIRED", "message": "Session expired"})

    # 3. Create server-side session
    if prior := request.cookies.get("demo_session"):
        global_session_store.revoke_session(prior)
    session = global_session_store.create_session(
        customer_id=payload.customer_id,
        account_id=account_id,
        gateway_token=gateway_token,
        ttl_seconds=ttl,
    )

    # Set secure same-origin cookie for browser convenience
    response.set_cookie(
        key="demo_session",
        value=session.session_id,
        httponly=True,
        samesite="lax",
        max_age=ttl,
        path="/demo",
        secure=request.url.scheme == "https",
    )

    return LoginResponse(
        session_id=session.session_id,
        customer_id=session.customer_id,
        account_id=session.account_id,
        expires_at=session.expires_at,
    )


@router.post("/logout", status_code=204)
async def demo_customer_logout(request: Request) -> Response:
    response = Response(status_code=204)
    if token := request.cookies.get("demo_session"):
        global_session_store.revoke_session(token)
    response.delete_cookie("demo_session", path="/demo", httponly=True)
    return response


@router.get("/overview")
async def demo_bank_overview(
    session: Annotated[DemoSession, Depends(get_current_demo_session)],
    gw_client: Annotated[GatewayClient, Depends(_get_gateway_client)],
    bank_client: Annotated[BankClient, Depends(_get_bank_client)],
) -> dict[str, Any]:
    """Retrieve combined bank balance, ledger, and customer gateway data."""
    # Fetch bank data using server-side service credentials
    balance_task = bank_client.get_balance(
        account_id=session.account_id,
        customer_id=session.customer_id,
    )
    tx_task = bank_client.get_transactions(
        account_id=session.account_id,
        customer_id=session.customer_id,
        limit=50,
    )

    # Fetch customer gateway entities using customer gateway token
    bootstrap_task = gw_client.get_customer_bootstrap(session.gateway_token)
    mandates_task = gw_client.list_customer_mandates(session.gateway_token, limit=50)
    escalations_task = gw_client.list_customer_escalations(session.gateway_token, limit=50)
    receipts_task = gw_client.read_payment_evidence(
        "/v1/customer/payment-receipts", session.gateway_token
    )

    (
        balance_res,
        tx_res,
        bootstrap_res,
        mandates_res,
        escalations_res,
        receipts_res,
    ) = await asyncio.gather(
        balance_task,
        tx_task,
        bootstrap_task,
        mandates_task,
        escalations_task,
        receipts_task,
    )

    return {
        "customer_id": session.customer_id,
        "account_id": session.account_id,
        "balance_paise": balance_res["balance_paise"],
        "currency": balance_res["currency"],
        "transactions": tx_res["items"],
        "mandates": mandates_res["items"],
        "escalations": escalations_res["items"],
        "receipts": [item for item in receipts_res["items"]
                     if item.get("from_account") == session.account_id],
        "accounts": [item for item in bootstrap_res.get("accounts", [])
                     if item.get("account_id") == session.account_id],
        "payees": bootstrap_res.get("payees", []),
        "agents": bootstrap_res.get("agents", []),
    }


@router.post("/mandates", status_code=status.HTTP_201_CREATED)
async def demo_create_mandate(
    payload: dict[str, Any],
    session: Annotated[DemoSession, Depends(get_current_demo_session)],
    gw_client: Annotated[GatewayClient, Depends(_get_gateway_client)],
) -> dict[str, Any]:
    """Proxy mandate creation to Gateway on behalf of the customer session."""
    return await gw_client.create_customer_mandate(
        gateway_token=session.gateway_token,
        payload=payload,
    )


@router.post("/mandates/{id}/revoke")
async def demo_revoke_mandate(
    id: str,
    session: Annotated[DemoSession, Depends(get_current_demo_session)],
    gw_client: Annotated[GatewayClient, Depends(_get_gateway_client)],
) -> dict[str, Any]:
    """Proxy mandate revocation to Gateway on behalf of the customer session."""
    return await gw_client.revoke_customer_mandate(
        gateway_token=session.gateway_token,
        mandate_id=id,
    )


@router.post("/approvals/{id}")
async def demo_submit_approval(
    id: str,
    payload: ApprovalDecisionRequest,
    session: Annotated[DemoSession, Depends(get_current_demo_session)],
    gw_client: Annotated[GatewayClient, Depends(_get_gateway_client)],
) -> dict[str, Any]:
    """Submit approval decision with optional customer PIN for step-up actions.

    PIN is delivered to the gateway for verification and never retained or logged.
    """
    return await gw_client.submit_approval_decision(
        gateway_token=session.gateway_token,
        escalation_id=id,
        decision=payload.decision,
        pin=payload.pin,
        suspend_agent=payload.suspend_agent,
    )


@router.get("/stream")
async def demo_customer_notifications_stream(
    request: Request,
    session: Annotated[DemoSession, Depends(get_current_demo_session)],
    gw_client: Annotated[GatewayClient, Depends(_get_gateway_client)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Relay customer notifications from Gateway customer SSE stream.

    Handles reconnect with Last-Event-ID, detects disconnects, and ensures session validity.
    """
    # Open the gateway stream with the server-side customer session.
    upstream_url = f"{gw_client.gateway_origin}/v1/customer/stream"
    headers = {
        "Authorization": f"Bearer {session.gateway_token}",
        "Accept": "text/event-stream",
    }
    if last_event_id:
        headers["Last-Event-ID"] = str(last_event_id)

    async def _stream_generator() -> AsyncIterator[str]:
        client = httpx.AsyncClient(timeout=httpx.Timeout(8, read=15), trust_env=False)
        try:
            async with client.stream("GET", upstream_url, headers=headers) as resp:
                if resp.status_code != 200:
                    event = "session.expired" if resp.status_code == 401 else "reconnect"
                    yield f"event: {event}\ndata: {{}}\n\n"
                    return

                async for line in resp.aiter_lines():
                    if await request.is_disconnected():
                        break
                    if global_session_store.get_session(session.session_id) is None:
                        yield "event: session.expired\ndata: {}\n\n"
                        return
                    yield f"{line}\n"
        except (asyncio.CancelledError, GeneratorExit):
            pass
        except httpx.HTTPError:
            yield "event: reconnect\ndata: {}\n\n"
        finally:
            await client.aclose()

    return StreamingResponse(
        _stream_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
