"""Member C owned risk boundary DTOs."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import Field, StrictInt

from .common import ContractModel, FinalDecision, OpaqueId, RiskLevel
from .core_models import CanonicalAction, MandateSnapshot, PolicyResult
from .identity_models import AuthContext


class RiskSignal(ContractModel):
    name: OpaqueId
    severity: Annotated[StrictInt, Field(ge=0, le=100)]
    weight: Annotated[float, Field(ge=0, le=1)]
    reason_code: OpaqueId
    message: str


class HistorySnapshot(ContractModel):
    successful_amounts_paise: tuple[int, ...] = ()
    recent_attempt_times: tuple[datetime, ...] = ()
    recent_denial_times: tuple[datetime, ...] = ()
    previously_paid_payees: tuple[OpaqueId, ...] = ()


class RiskInput(ContractModel):
    auth: AuthContext
    action: CanonicalAction
    policy: PolicyResult
    mandate: MandateSnapshot
    history: HistorySnapshot
    now: datetime
    attempt_id: OpaqueId


class RiskResult(ContractModel):
    score: Annotated[StrictInt, Field(ge=0, le=100)]
    level: RiskLevel
    recommended: FinalDecision
    signals: tuple[RiskSignal, ...] = ()
    breaker_recommended: bool = False
    breaker_reasons: tuple[str, ...] = ()
    model_version: OpaqueId
