"""Operation-time, best-effort observations for the Ops money-action stream."""

from datetime import UTC, datetime
from typing import Any

from contracts.reason_codes import ReasonCode
from gateway.events.bus import EventBus

STAGES = (
    "edge",
    "identity",
    "registry",
    "canonical",
    "idempotency",
    "mandate",
    "policy",
    "risk",
    "breaker",
    "decision",
    "execution",
)

SIGNALS = frozenset(
    (
        "new_payee",
        "amount_outlier",
        "near_limit_structuring",
        "velocity",
        "request_rate",
        "off_pattern_time",
        "sequence_anomaly",
        "probing",
        "intent_mismatch",
        "read_enumeration",
    )
)


# Only renderer-facing scalars cross the stream boundary.
NUMERIC_DETAILS = frozenset(
    (
        "requested_paise",
        "threshold_paise",
        "limit_paise",
        "projected_paise",
        "projected_count",
        "limit",
        "grant_index",
        "elapsed_ns",
        "score",
        "severity",
        "weight",
        "new",
        "decisive",
        "recommended",
        "fallback",
    )
)
ENUM_DETAILS = {
    "outcome": {
        "EXECUTED",
        "EXECUTION_UNKNOWN",
        "FAILED",
        "PENDING_APPROVAL",
        "REJECTED",
        "UNKNOWN",
        "DECLINED",
    },
    "verdict": {"DENY", "ALLOW", "ESCALATE", "STEP_UP"},
    "band": {"LOW", "MEDIUM", "HIGH"},
    "decision": {"APPROVE", "REJECT"},
    "reason_code": ReasonCode.__members__,
}


class GateTrace:
    def __init__(self, bus: EventBus | None, request_id: str) -> None:
        self.bus = bus
        self.request_id = request_id
        self.stage = "edge"
        self.check = "idempotency_key"
        self.finished = False
        self.rows: list[dict[str, Any]] = []

    def enter(self, stage: str, check: str) -> None:
        self.stage, self.check = stage, check

    def row(self, status: str, **detail: Any) -> None:
        if self.bus is not None:
            self.rows.append(
                {
                    "request_id": self.request_id,
                    "stage": self.stage,
                    "check": self.check,
                    "status": status,
                    "at": datetime.now(UTC).isoformat(),
                    "detail": {
                        key: value
                        for key, value in detail.items()
                        if (key in NUMERIC_DETAILS and isinstance(value, (int, float)))
                        or (isinstance(value, str) and value in ENUM_DETAILS.get(key, ()))
                    },
                }
            )

    def skip_remaining(self) -> None:
        for stage in STAGES[STAGES.index(self.stage) + 1 :]:
            self.enter(stage, "not_reached")
            self.row("skip")
        self.finished = True

    def failure(self, error: Exception) -> None:
        code = getattr(error, "code", None)
        detail = (
            {"reason_code": code}
            if isinstance(code, str) and code in ReasonCode.__members__
            else {}
        )
        self.row("fail", **detail)

    async def flush(self) -> None:
        if self.bus is not None:
            for row in self.rows:
                try:
                    await self.bus.publish("gate.step", row)
                except Exception:
                    # A diagnostic subscriber cannot change a financial outcome.
                    pass
        self.rows.clear()
