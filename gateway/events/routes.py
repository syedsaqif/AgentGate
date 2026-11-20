"""Member A customer and ops SSE stream routes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse

from contracts.common import HumanRole
from contracts.reason_codes import ReasonCode
from gateway.deps import Dependencies
from gateway.errors import AgentGateError
from gateway.events.bus import EventBus
from gateway.events.sse import (
    format_event_frame,
    format_keepalive_frame,
    format_resync_frame,
)

router = APIRouter(prefix="/v1", tags=["events"])


def _extract_session_token(request: Request) -> str:
    """Extract human session token strictly from headers or cookies, never query strings."""
    # Check Authorization header (Bearer <token> or direct <token>)
    auth_header = request.headers.get("Authorization")
    if auth_header:
        parts = auth_header.strip().split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return auth_header.strip()

    # Check X-Session-Token header
    session_header = request.headers.get("X-Session-Token")
    if session_header:
        return session_header.strip()

    # Check cookies
    cookie_token = request.cookies.get("session_token")
    if cookie_token:
        return cookie_token.strip()

    raise AgentGateError(
        code=ReasonCode.E_AUTH_MISSING,
        message="Missing human session token in request headers",
        status_code=401,
    )


async def _sse_stream_generator(
    bus: EventBus,
    role: HumanRole,
    customer_id: str | None,
    last_event_id: int | None,
    request: Request | None = None,
    max_events: int | None = None,
) -> AsyncIterator[str]:
    """Generate SSE frames with replay and live queue subscription."""
    yielded_count = 0

    # 1. Handle historical replay or resync signaling
    if last_event_id is not None:
        if bus.is_evicted(last_event_id):
            yield format_resync_frame()
            yielded_count += 1
            if max_events is not None and yielded_count >= max_events:
                return
        else:
            for historical_item in bus.get_since(last_event_id):
                frame = format_event_frame(historical_item, role=role, customer_id=customer_id)
                if frame:
                    yield frame
                    yielded_count += 1
                    if max_events is not None and yielded_count >= max_events:
                        return

    if max_events is not None and yielded_count >= max_events:
        return

    # 2. Subscribe to live events
    queue = bus.subscribe()
    try:
        while True:
            if request is not None and await request.is_disconnected():
                break
            try:
                # Wait up to 1 second before checking disconnect and sending keepalive
                item = await asyncio.wait_for(queue.get(), timeout=1.0)
                frame = format_event_frame(item, role=role, customer_id=customer_id)
                if frame:
                    yield frame
                    yielded_count += 1
                    if max_events is not None and yielded_count >= max_events:
                        break
            except TimeoutError:
                if request is not None and await request.is_disconnected():
                    break
                yield format_keepalive_frame()
    except (asyncio.CancelledError, GeneratorExit):
        pass
    finally:
        bus.unsubscribe(queue)


@router.get("/customer/stream")
async def customer_stream(
    request: Request,
    last_event_id_hdr: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Customer-filtered SSE event stream.

    Requires customer session authentication via headers.
    Filters strictly by authenticated customer ID and redacts ops-only fields.
    """
    token = _extract_session_token(request)
    deps: Dependencies = request.app.state.deps

    if deps.human_auth is None:
        raise AgentGateError(
            code=ReasonCode.E_AUTH_MISSING,
            message="Human authentication provider is not configured",
            status_code=503,
        )

    human_ctx = await deps.human_auth.authenticate(token, required_role=HumanRole.CUSTOMER)
    if human_ctx.role != HumanRole.CUSTOMER:
        raise AgentGateError(
            code=ReasonCode.E_AUTH_KEY_MISMATCH,
            message=f"Role mismatch: required customer, got {human_ctx.role}",
            status_code=403,
        )

    last_id_raw = (
        last_event_id_hdr
        or request.headers.get("last-event-id")
        or request.headers.get("Last-Event-ID")
    )
    last_event_id: int | None = None
    if last_id_raw is not None:
        try:
            last_event_id = int(last_id_raw)
        except ValueError:
            pass

    max_events_raw = request.query_params.get("max_events") or request.headers.get(
        "X-Stream-Max-Events"
    )
    max_events: int | None = None
    if max_events_raw is not None:
        try:
            max_events = int(max_events_raw)
        except ValueError:
            pass

    bus: EventBus = deps.event_bus or request.app.state.deps.event_bus
    gen = _sse_stream_generator(
        bus=bus,
        role=HumanRole.CUSTOMER,
        customer_id=human_ctx.subject_id,
        last_event_id=last_event_id,
        request=request,
        max_events=max_events,
    )
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/ops/stream")
async def ops_stream(
    request: Request,
    last_event_id_hdr: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Ops SSE event stream.

    Requires ops session authentication via headers.
    Streams all system events without customer filtering or redaction.
    """
    token = _extract_session_token(request)
    deps: Dependencies = request.app.state.deps

    if deps.human_auth is None:
        raise AgentGateError(
            code=ReasonCode.E_AUTH_MISSING,
            message="Human authentication provider is not configured",
            status_code=503,
        )

    human_ctx = await deps.human_auth.authenticate(token, required_role=HumanRole.OPS)
    if human_ctx.role != HumanRole.OPS:
        raise AgentGateError(
            code=ReasonCode.E_AUTH_KEY_MISMATCH,
            message=f"Role mismatch: required ops, got {human_ctx.role}",
            status_code=403,
        )

    last_id_raw = (
        last_event_id_hdr
        or request.headers.get("last-event-id")
        or request.headers.get("Last-Event-ID")
    )
    last_event_id: int | None = None
    if last_id_raw is not None:
        try:
            last_event_id = int(last_id_raw)
        except ValueError:
            pass

    max_events_raw = request.query_params.get("max_events") or request.headers.get(
        "X-Stream-Max-Events"
    )
    max_events: int | None = None
    if max_events_raw is not None:
        try:
            max_events = int(max_events_raw)
        except ValueError:
            pass

    bus: EventBus = deps.event_bus or request.app.state.deps.event_bus
    gen = _sse_stream_generator(
        bus=bus,
        role=HumanRole.OPS,
        customer_id=None,
        last_event_id=last_event_id,
        request=request,
        max_events=max_events,
    )
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
