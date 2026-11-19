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


class EventBus:
    """In-process asynchronous event bus with bounded history for replay.

    Strict invariant: publish events ONLY AFTER the surrounding database transaction commits.
    """

    def __init__(self, history_limit: int = 2000) -> None:
        self.history_limit = history_limit
        self._history: deque[EventItem] = deque(maxlen=history_limit)
        self._subscribers: set[asyncio.Queue[EventItem]] = set()
        self._seq: int = 0
        self._lock: asyncio.Lock | None = None

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def publish(self, event_type: str, data: dict[str, Any]) -> EventItem:
        """Publish a committed event to all active subscribers and append to history."""
        async with self.lock:
            self._seq += 1
            item = EventItem(
                event_id=self._seq,
                event_type=event_type,
                data=data,
                timestamp=datetime.now(UTC),
            )
            self._history.append(item)

            for queue in list(self._subscribers):
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

    def subscribe(self, max_queue_size: int = 256) -> asyncio.Queue[EventItem]:
        """Subscribe to the event stream, returning an asyncio Queue."""
        q: asyncio.Queue[EventItem] = asyncio.Queue(maxsize=max_queue_size)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, queue: asyncio.Queue[EventItem]) -> None:
        """Remove a subscriber queue."""
        self._subscribers.discard(queue)

    def get_since(self, last_event_id: int) -> list[EventItem]:
        """Return history events with event_id > last_event_id."""
        return [item for item in self._history if item.event_id > last_event_id]

    @property
    def latest_seq(self) -> int:
        return self._seq
