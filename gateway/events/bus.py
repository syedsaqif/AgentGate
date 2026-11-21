"""Member A in-process event bus with bounded ring-buffer for SSE replay."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class EventItem:
    """Committed event item with sequential ID for SSE streaming and replay."""

    event_id: int
    event_type: str
    data: dict[str, Any]
    timestamp: datetime
    customer_audience: frozenset[str] = frozenset()

    @property
    def customer_id(self) -> str | None:
        return self.data.get("customer_id")

    @property
    def agent_id(self) -> str | None:
        return self.data.get("agent_id")

    @property
    def request_id(self) -> str | None:
        return self.data.get("request_id")


class EventBus:
    """In-process asynchronous event bus with bounded history for replay.

    Strict invariant: publish events ONLY AFTER the surrounding database transaction commits.
    """

    def __init__(self, history_limit: int = 2000) -> None:
        self.history_limit = history_limit
        self._history: deque[EventItem] = deque(maxlen=history_limit)
        self._subscribers: dict[asyncio.Queue[EventItem], bool] = {}
        self._seq: int = 0
        self._lock: asyncio.Lock | None = None

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def publish(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        customer_audience: frozenset[str] = frozenset(),
    ) -> EventItem:
        """Publish a committed event to all active subscribers and append to history."""
        async with self.lock:
            if event_type == "gate.step":
                previous = next(
                    (
                        item
                        for item in reversed(self._history)
                        if item.event_type == "gate.step" and item.request_id == data["request_id"]
                    ),
                    None,
                )
                # After replay eviction use the bus high-water mark to avoid reusing a sequence.
                first = 1 if data["stage"] == "edge" else self._seq + 1
                data = {**data, "seq": previous.data["seq"] + 1 if previous else first}
            self._seq += 1
            item = EventItem(
                event_id=self._seq,
                event_type=event_type,
                data=data,
                timestamp=datetime.now(UTC),
                customer_audience=customer_audience,
            )
            self._history.append(item)

            for queue, include_trace in list(self._subscribers.items()):
                if event_type == "gate.step" and not include_trace:
                    continue
                try:
                    queue.put_nowait(item)
                except asyncio.QueueFull:
                    # Drop oldest if queue is full
                    try:
                        queue.get_nowait()
                        queue.put_nowait(item)
                    except Exception:
                        pass

            return item

    def subscribe(
        self, max_queue_size: int = 256, *, include_trace: bool = True
    ) -> asyncio.Queue[EventItem]:
        """Subscribe to the event stream, returning an asyncio Queue."""
        q: asyncio.Queue[EventItem] = asyncio.Queue(maxsize=max_queue_size)
        self._subscribers[q] = include_trace
        return q

    def unsubscribe(self, queue: asyncio.Queue[EventItem]) -> None:
        """Remove a subscriber queue."""
        self._subscribers.pop(queue, None)

    def get_since(self, last_event_id: int) -> list[EventItem]:
        """Return history events with event_id > last_event_id."""
        return [item for item in self._history if item.event_id > last_event_id]

    def is_evicted(self, last_event_id: int) -> bool:
        """Check if any events between last_event_id and oldest retained event were evicted."""
        if not self._history:
            return last_event_id < self._seq
        oldest_id = self._history[0].event_id
        return last_event_id < oldest_id - 1

    def clear(self) -> None:
        """Clear history and reset sequence counter (used for dev/test reset)."""
        self._history.clear()
        self._seq = 0

    @property
    def latest_seq(self) -> int:
        return self._seq
