"""Member A role-filtered SSE formatting and envelope construction."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from contracts.common import HumanRole
from gateway.events.bus import EventItem


def format_event_frame(
    item: EventItem,
    role: HumanRole,
    customer_id: str | None = None,
) -> str | None:
    """Format an EventItem into a compliant SSE wire frame.

    Enforces customer isolation and redacts internal ops diagnostics when streamed
    to a customer session.
    """
    if item.event_type == "gate.step" and role != HumanRole.OPS:
        return None

    # Strict customer isolation: customer only receives their own events
    if role == HumanRole.CUSTOMER:
        if item.event_type == "agent.suspended" and customer_id in item.customer_audience:
            # Scope the existing committed notification without disclosing other owners.
            item = replace(item, data={"customer_id": customer_id, "agent_id": item.agent_id,
                                       "status": "SUSPENDED"})
        item_customer = item.customer_id
        if item_customer is None or item_customer != customer_id:
            return None
        if item.event_type == "policy.decision":
            return None

    # Construct the JSON envelope per Section 6.8
    inner_data: dict[str, Any] = dict(item.data)
    inner_data.pop("customer_id", None)
    inner_data.pop("agent_id", None)
    inner_data.pop("request_id", None)

    # If "data" was already a sub-dictionary, unpack it
    if "data" in inner_data and isinstance(inner_data["data"], dict) and len(inner_data) == 1:
        inner_data = inner_data["data"]

    # Redact ops-only fields for customer stream
    if role == HumanRole.CUSTOMER:
        for ops_key in (
            "risk",
            "diagnostics",
            "raw_signals",
            "internal_trace",
            "internal_error",
            "traceback",
        ):
            inner_data.pop(ops_key, None)

    occurred_at_str = item.timestamp.isoformat().replace("+00:00", "Z")
    if not occurred_at_str.endswith("Z"):
        occurred_at_str += "Z"

    envelope = {
        "event_id": str(item.event_id),
        "event_type": item.event_type,
        "occurred_at": occurred_at_str,
        "request_id": item.request_id,
        "customer_id": item.customer_id,
        "agent_id": item.agent_id,
        "data": inner_data,
    }

    payload = json.dumps(envelope, separators=(",", ":"))
    return f"id: {item.event_id}\nevent: {item.event_type}\ndata: {payload}\n\n"


def format_resync_frame() -> str:
    """Return explicit resync event frame when client's Last-Event-ID was evicted."""
    return "event: resync\ndata: {}\n\n"


def format_keepalive_frame() -> str:
    """Return an SSE comment frame used for heartbeat/keep-alive."""
    return ": keepalive\n\n"
