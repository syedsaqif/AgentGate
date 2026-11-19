"""Member A platform clock implementations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from contracts.common import Clock


class SystemClock(Clock):
    """Production system clock returning timezone-aware UTC datetimes."""

    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass
class FrozenClock(Clock):
    """Deterministic frozen clock for testing and scripted verification."""

    value: datetime

    def now(self) -> datetime:
        if self.value.tzinfo is None:
            raise ValueError("FrozenClock requires an aware UTC datetime")
        return self.value.astimezone(UTC)

    def advance(self, *, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)
