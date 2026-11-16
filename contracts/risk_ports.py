"""Member C owned risk protocol."""

from __future__ import annotations

from typing import Protocol

from .common import Tx
from .risk_models import RiskInput, RiskResult


class RiskEngine(Protocol):
    def score(self, risk_input: RiskInput) -> RiskResult: ...

    async def record_attempt(self, risk_input: RiskInput, tx: Tx) -> None: ...

    async def record_outcome(self, outcome: object, tx: Tx) -> None: ...
