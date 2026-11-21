"""Member B approval and action-status polling helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .retry import SleepFn

ESCALATION_TERMINAL_STATES: frozenset[str] = frozenset(
    {"EXECUTED", "REJECTED", "EXPIRED", "CANCELLED", "FAILED", "EXECUTION_UNKNOWN"}
)
ACTION_TERMINAL_STATES: frozenset[str] = frozenset(
    {"EXECUTED", "REJECTED", "FAILED", "EXECUTION_UNKNOWN"}
)


class _StatusClient(Protocol):
    async def get_escalation(self, escalation_id: str) -> dict[str, Any]: ...

    async def get_action(self, request_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class PollResult:
    """Outcome of polling an escalation or action status endpoint."""

    status: str
    payload: dict[str, Any]
    attempts: int
    timed_out: bool


async def _poll_until_terminal(
    *,
    fetch_fn: Callable[[], Awaitable[dict[str, Any]]],
    terminal_states: frozenset[str],
    interval_seconds: float,
    max_attempts: int,
    sleep_fn: SleepFn,
) -> PollResult:
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if interval_seconds < 0:
        raise ValueError("interval_seconds must be >= 0")

    last_payload: dict[str, Any] = {}
    last_status = "UNKNOWN"

    for attempt in range(1, max_attempts + 1):
        payload = await fetch_fn()
        raw_status = payload.get("status") or payload.get("state") or "UNKNOWN"
        status = str(raw_status).upper()
        last_payload = payload
        last_status = status

        if status in terminal_states:
            return PollResult(
                status=status,
                payload=payload,
                attempts=attempt,
                timed_out=False,
            )

        if attempt < max_attempts and interval_seconds > 0:
            await sleep_fn(interval_seconds)

    return PollResult(
        status=last_status,
        payload=last_payload,
        attempts=max_attempts,
        timed_out=True,
    )


async def poll_escalation(
    client: _StatusClient,
    escalation_id: str,
    *,
    interval_seconds: float = 2.0,
    max_attempts: int = 30,
    terminal_states: frozenset[str] = ESCALATION_TERMINAL_STATES,
    sleep_fn: SleepFn = asyncio.sleep,
) -> PollResult:
    """Poll `GET /v1/escalations/{id}` until a terminal decision/execution state or timeout."""

    return await _poll_until_terminal(
        fetch_fn=lambda: client.get_escalation(escalation_id),
        terminal_states=terminal_states,
        interval_seconds=interval_seconds,
        max_attempts=max_attempts,
        sleep_fn=sleep_fn,
    )


async def poll_action(
    client: _StatusClient,
    request_id: str,
    *,
    interval_seconds: float = 2.0,
    max_attempts: int = 30,
    terminal_states: frozenset[str] = ACTION_TERMINAL_STATES,
    sleep_fn: SleepFn = asyncio.sleep,
) -> PollResult:
    """Poll `GET /v1/actions/{request_id}` until a terminal action state or timeout."""

    return await _poll_until_terminal(
        fetch_fn=lambda: client.get_action(request_id),
        terminal_states=terminal_states,
        interval_seconds=interval_seconds,
        max_attempts=max_attempts,
        sleep_fn=sleep_fn,
    )


__all__ = [
    "ACTION_TERMINAL_STATES",
    "ESCALATION_TERMINAL_STATES",
    "PollResult",
    "poll_action",
    "poll_escalation",
]
