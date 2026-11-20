"""Member A global deterministic platform test primitives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass
class FrozenClock:
    value: datetime

    def now(self) -> datetime:
        if self.value.tzinfo is None:
            raise ValueError("FrozenClock requires an aware UTC datetime")
        return self.value.astimezone(UTC)

    def advance(self, *, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)

    def set_time(self, new_value: datetime) -> None:
        if new_value.tzinfo is None:
            new_value = new_value.replace(tzinfo=UTC)
        self.value = new_value.astimezone(UTC)
