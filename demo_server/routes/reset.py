"""Demo server soft reset endpoint."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from demo_server.config import DemoServerConfig
from demo_server.gateway_client import GatewayClient
from demo_server.session import global_session_store

router = APIRouter()


def _get_gateway_client(request: Request) -> GatewayClient:
    return request.app.state.gateway_client


def _get_config(request: Request) -> DemoServerConfig:
    return request.app.state.config


@router.post("/reset")
async def demo_soft_reset(
    request: Request,
    gw_client: Annotated[GatewayClient, Depends(_get_gateway_client)],
    config: Annotated[DemoServerConfig, Depends(_get_config)],
) -> dict[str, Any]:
    async with request.app.state.run_manager.execution_lock:
        return await _reset(request, gw_client, config)


async def _reset(
    request: Request, gw_client: GatewayClient, config: DemoServerConfig,
) -> dict[str, Any]:
    """Execute a soft reset for the demo environment.

    Reinstates BillPal if suspended, revokes existing active customer mandates,
    prepares a fresh default demo mandate, and resets session trace boundaries.
    Reports precisely what was reset and reminds that fresh balance and risk history
    require a separately started disposable stack.
    """
    reinstated_agent: str | None = None
    revoked_mandates: list[str] = []
    created_mandate_id: str | None = None

    # 1. Reinstate BillPal via Gateway Ops API
    try:
        await gw_client.reinstate_agent("agt_billpal")
        reinstated_agent = "agt_billpal"
    except Exception:
        pass

    # 2. Authenticate customer to clean up and prepare fresh mandate
    customer_pin = config.customer_pin or os.getenv("D06_CUSTOMER_PIN", "4821")
    customer_id = config.customer_id
    gw_token: str | None = None

    try:
        login_res = await gw_client.customer_login(customer_id, customer_pin)
        gw_token = login_res.get("token")
    except Exception:
        pass

    if gw_token:
        # 3. Revoke existing active customer mandates
        try:
            mandates_res = await gw_client.list_customer_mandates(gw_token, limit=50)
            items = mandates_res.get("items", [])
            for item in items:
                status_val = item.get("status")
                terms = item.get("terms", {})
                mandate_id = terms.get("mandate_id")
                if status_val == "ACTIVE" and mandate_id:
                    try:
                        await gw_client.revoke_customer_mandate(gw_token, mandate_id)
                        revoked_mandates.append(mandate_id)
                    except Exception:
                        pass
        except Exception:
            pass

        # 4. Prepare fresh default demo mandate
        # Terms matching plan.md §10A.3: BillPal, ACC-001, BILL-ELEC, max ₹5,000 per txn,
        # ₹10,000 total, 5 uses, step-up above ₹4,000, new_payee toggle OFF.
        now = datetime.now(UTC)
        valid_until = now + timedelta(days=7)
        now_str = now.isoformat().replace("+00:00", "Z")
        valid_until_str = valid_until.isoformat().replace("+00:00", "Z")

        new_mandate_payload = {
            "agent_id": "agt_billpal",
            "valid_from": now_str,
            "valid_until": valid_until_str,
            "grants": [
                {
                    "grant_id": "grant_billpal_electricity",
                    "action": "bill_pay",
                    "account_ids": [config.account_id],
                    "payee_ids": ["BILL-ELEC"],
                    "currency": "INR",
                    "max_per_txn_paise": 500000,
                    "max_total_paise": 1000000,
                    "max_count": 5,
                    "window": "mandate",
                    "category": "utilities.electricity",
                }
            ],
            "step_up": {
                "amount_above_paise": 400000,
                "new_payee_requires_approval": False,
                "always_ask_actions": [],
            },
            "risk_profile": "balanced",
            "pin": customer_pin,
        }

        try:
            created_res = await gw_client.create_customer_mandate(gw_token, new_mandate_payload)
            mandate_obj = created_res.get("mandate", {})
            terms_obj = mandate_obj.get("terms", {})
            created_mandate_id = terms_obj.get("mandate_id")
        except Exception:
            pass

    # 5. Clear all demo customer sessions
    global_session_store.clear()
    request.app.state.run_manager.clear_retry()

    # 6. Reset presenter trace scope timestamp to now
    request.app.state.session_start_time = datetime.now(UTC)

    return {
        "status": "ok",
        "reinstated_agent": reinstated_agent,
        "revoked_mandates": revoked_mandates,
        "created_mandate_id": created_mandate_id,
        "reset_scope": {
            "agent_reinstated": reinstated_agent is not None,
            "mandates_reset": created_mandate_id is not None,
            "session_traces_cleared": True,
            "balance_reset": False,
            "risk_history_reset": False,
        },
        "message": (
            "Soft reset finished. The ledger is retained. "
            "A fresh balance and clean risk history require a separately started stack."
        ),
    }
