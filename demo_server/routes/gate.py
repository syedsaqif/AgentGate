"""Presenter-only gate event stream relay."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from demo_server.config import DemoServerConfig
from demo_server.gateway_client import GatewayClient

router = APIRouter()


def _get_gateway_client(request: Request) -> GatewayClient:
    return request.app.state.gateway_client


def _sanitize_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Allow only presentation fields across the operator-to-browser boundary."""
    def pick(source: Any, keys: tuple[str, ...]) -> dict[str, Any]:
        return {
            key: source[key] for key in keys if isinstance(source, dict)
            and isinstance(source.get(key), (str, bool, float, int))
        }

    envelope = pick(payload, ("event_type", "event_id", "occurred_at", "request_id",
                              "agent_id", "customer_id"))
    raw = payload.get("data", {})
    data = pick(raw, ("stage", "check", "status", "seq", "at", "state", "decision", "reason_code",
                      "expires_at", "escalation_id", "amount_paise", "payee_id", "from_account"))
    if isinstance(raw, dict):
        if isinstance(raw.get("detail"), dict):
            data["detail"] = pick(raw["detail"], (
                "requested_paise", "threshold_paise", "limit_paise", "projected_paise",
                "projected_count", "limit", "grant_index", "elapsed_ns", "score", "severity",
                "weight", "new", "decisive", "recommended", "fallback", "outcome", "verdict",
                "band", "decision", "reason_code",
            ))
        if isinstance(raw.get("risk"), dict):
            risk = raw["risk"]
            data["risk"] = pick(risk, ("score", "level", "model_version"))
            data["risk"]["signals"] = [
                pick(signal, ("name", "severity", "weight")) for signal in risk.get("signals", [])
            ]
        if isinstance(raw.get("audit_ref"), dict):
            data["audit_ref"] = pick(raw["audit_ref"], ("seq", "entry_hash"))
        if isinstance(raw.get("reason_codes"), list):
            data["reason_codes"] = [v for v in raw["reason_codes"]
                                    if isinstance(v, str) and v.startswith("E_")]
    return {**envelope, "data": data}



async def _attach_display_evidence(
    client: httpx.AsyncClient,
    gateway: GatewayClient,
    config: DemoServerConfig,
    headers: dict[str, str],
    event: dict[str, Any],
) -> None:
    """Project only display fields from the demo customer's authoritative records."""
    request_id = event.get("request_id")
    data = event.get("data")
    if not isinstance(request_id, str) or not isinstance(data, dict):
        return
    if event.get("customer_id") != config.customer_id:
        return

    kind = event.get("event_type")
    if kind == "escalation.created" and config.customer_pin and not data.get("expires_at"):
        try:
            session = await gateway.customer_login(config.customer_id, config.customer_pin)
            token = session.get("session_token") or session.get("token")
            if token:
                page = await gateway.list_customer_escalations(token, limit=100)
                for item in page.get("items", []):
                    if (
                        item.get("escalation_id") == data.get("escalation_id")
                        and item.get("request_id") == request_id
                    ):
                        data["expires_at"] = item.get("expires_at")
                        break
        except (HTTPException, httpx.HTTPError):
            pass

    if kind not in {"policy.decision", "bank.executed"}:
        return
    try:
        response = await client.get(
            f"{gateway.gateway_origin}/v1/audit/entries",
            headers={"Authorization": headers["Authorization"]},
            params={"request_id": request_id},
            timeout=2,
        )
        entries = [
            entry for entry in response.raise_for_status().json().get("items", [])
            if entry.get("request_id") == request_id
        ]
    except (httpx.HTTPError, ValueError):
        return

    audit_fields = ("seq", "event_type", "entry_hash", "prev_hash")
    data["audit"] = [{key: entry[key] for key in audit_fields if key in entry} for entry in entries]
    if kind == "policy.decision":
        decision = next((entry for entry in entries if entry.get("event_type") == kind), {})
        safe = decision.get("safe_payload") or {}
        action = safe.get("action") or {}
        params = action.get("params") or {}
        identity = safe.get("agent_identity") or {}
        terms = (safe.get("mandate_snapshot") or {}).get("terms") or {}
        data["request"] = {
            "agent_name": identity.get("name"),
            "agent_id": event.get("agent_id"),
            "fingerprint": identity.get("jkt"),
            "action_type": action.get("action_type", safe.get("action_type")),
            "payee_id": params.get("payee_id"),
            "amount_paise": params.get("amount_paise", safe.get("amount_paise")),
            "category": (action.get("intent") or {}).get("category"),
            "risk_profile": terms.get("risk_profile"),
        }


@router.get("/stream")
async def demo_gate_presenter_stream(
    request: Request,
    gw_client: Annotated[GatewayClient, Depends(_get_gateway_client)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Presenter-only relay of sanitized gate events from Gateway ops stream.

    Maintains server-side operator authentication and scopes events to the active
    demo session.
    """
    ops_token = await gw_client.get_ops_token()
    config = request.app.state.config
    session_start: datetime = getattr(
        request.app.state,
        "session_start_time",
        datetime.now(UTC),
    )

    upstream_url = f"{gw_client.gateway_origin}/v1/ops/stream"
    headers = {
        "Authorization": f"Bearer {ops_token}",
        "Accept": "text/event-stream",
    }
    headers["Last-Event-ID"] = last_event_id or "0"

    async def _stream_generator() -> AsyncIterator[str]:
        client = httpx.AsyncClient(timeout=None, trust_env=False)
        try:
            async with client.stream("GET", upstream_url, headers=headers) as resp:
                if resp.status_code != 200:
                    err_msg = f"Gateway ops stream returned {resp.status_code}"
                    yield f'event: error\ndata: {{"error": "{err_msg}"}}\n\n'
                    return

                cur_id: str | None = None
                cur_event: str | None = None
                cur_data_lines: list[str] = []

                async for line in resp.aiter_lines():
                    if await request.is_disconnected():
                        break

                    if not line:
                        # Frame boundary
                        if cur_data_lines:
                            try:
                                parsed = json.loads("\n".join(cur_data_lines))
                            except ValueError:
                                parsed = None

                            payload = None
                            if cur_event in {"resync", "session.expired", "reconnect"}:
                                payload = {}
                            elif (
                                isinstance(parsed, dict)
                                and parsed.get("event_type") == cur_event
                                and parsed.get("customer_id") in (None, config.customer_id)
                            ):
                                try:
                                    occurred = datetime.fromisoformat(parsed["occurred_at"])
                                    if occurred >= session_start:
                                        payload = _sanitize_event_payload(parsed)
                                        await _attach_display_evidence(
                                            client, gw_client, config, headers, payload
                                        )
                                except (KeyError, TypeError, ValueError):
                                    pass
                            if payload is not None:
                                frame_parts = [f"id: {cur_id}"] if cur_id else []
                                frame_parts.extend((f"event: {cur_event}",
                                                    f"data: {json.dumps(payload)}", "\n"))
                                yield "\n".join(frame_parts)

                        cur_id = None
                        cur_event = None
                        cur_data_lines = []
                        continue

                    if line.startswith(":"):
                        # Keep-alive comment frame
                        yield f"{line}\n\n"
                    elif line.startswith("id:"):
                        cur_id = line[3:].strip()
                    elif line.startswith("event:"):
                        cur_event = line[6:].strip()
                    elif line.startswith("data:"):
                        cur_data_lines.append(line[5:].strip())
        except (asyncio.CancelledError, GeneratorExit):
            pass
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
