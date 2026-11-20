"""Integration tests for Member C approval race conditions using real SQLite.

Verifies:
- Simultaneous approvals produce exactly one winner (E_ESC_ALREADY_CONSUMED for losers).
- Concurrent creations cannot exceed three pending per mandate (E_ESC_LIMIT_REACHED).
- Transactions are short and atomic.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from contracts.common import ActionType, FinalDecision, RiskLevel
from contracts.core_models import ActionParams, CanonicalAction, Intent
from contracts.escalation_models import EscalationDecision
from contracts.reason_codes import ReasonCode
from contracts.risk_models import RiskResult, RiskSignal
from gateway.escalation.models import Base
from gateway.escalation.service import EscalationConfig, PersistentEscalationService
from tests.support.platform import FrozenClock

try:
    from gateway.platform.db import (
        SqliteUnitOfWork,
        create_db_engine,
        create_session_factory,
    )

    A_DB_EXISTS = True
except ImportError:
    A_DB_EXISTS = False


def _make_action(
    request_id: str,
    mandate_id: str = "man_race_001",
    customer_id: str = "cust_001",
) -> CanonicalAction:
    return CanonicalAction(
        request_id=request_id,
        customer_id=customer_id,
        agent_id="agt_billpal",
        mandate_id=mandate_id,
        mandate_hash="0" * 64,
        action_type=ActionType.BILL_PAY,
        params=ActionParams(
            from_account="ACC-001",
            payee_id="BILL-ELEC",
            amount_paise=480000,
            currency="INR",
            reference="RACE-2026",
        ),
        intent=Intent(
            task="pay_electricity_bill",
            category="utilities.electricity",
            note="Race test payment",
        ),
        idempotency_key=f"idem_{request_id}",
        received_at=datetime(2026, 11, 19, 11, 0, 0, tzinfo=UTC),
    )


def _make_assessment() -> RiskResult:
    return RiskResult(
        score=50,
        level=RiskLevel.MEDIUM,
        recommended=FinalDecision.ESCALATE,
        signals=(
            RiskSignal(
                name="amount_outlier",
                severity=80,
                weight=0.625,
                reason_code="E_RISK_AMOUNT_OUTLIER",
                message="Outlier step-up",
            ),
        ),
        model_version="1.0.0",
    )


@pytest.mark.asyncio
async def test_simultaneous_approvals_single_winner(tmp_path: Path) -> None:
    if not A_DB_EXISTS:
        pytest.skip("Member A's database session factory does not exist")

    db_path = tmp_path / "race_test.db"
    engine = create_db_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = create_session_factory(engine)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    clock = FrozenClock(datetime(2026, 11, 19, 11, 0, 0, tzinfo=UTC))
    service = PersistentEscalationService(
        clock=clock,
        session_factory=session_factory,
    )

    # Pre-create a PENDING escalation
    act = _make_action("req_race_single")
    uow_init = SqliteUnitOfWork(session_factory)
    async with uow_init as tx:
        esc = await service.create(act, _make_assessment(), tx)

    # Launch 5 concurrent approval tasks
    async def do_approve() -> tuple[str, str | None]:
        uow_task = SqliteUnitOfWork(session_factory)
        async with uow_task as tx_task:
            try:
                res = await service.decide(
                    escalation_id=esc.escalation_id,
                    decision=EscalationDecision.APPROVE,
                    subject_id="cust_001",
                    tx=tx_task,
                )
                state_val = res.state.value if hasattr(res.state, "value") else str(res.state)
                return "SUCCESS", state_val
            except ValueError as e:
                return "ERROR", str(e)

    tasks = [asyncio.create_task(do_approve()) for _ in range(5)]
    results = await asyncio.gather(*tasks)

    successes = [r for r in results if r[0] == "SUCCESS"]
    errors = [r for r in results if r[0] == "ERROR"]

    # Exactly one winner, all others get E_ESC_ALREADY_CONSUMED
    assert len(successes) == 1
    assert successes[0][1] == "APPROVED"
    assert len(errors) == 4
    for err in errors:
        assert err[1] == ReasonCode.E_ESC_ALREADY_CONSUMED.value

    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_creations_cannot_exceed_pending_limit(tmp_path: Path) -> None:
    if not A_DB_EXISTS:
        pytest.skip("Member A's database session factory does not exist")

    db_path = tmp_path / "limit_race_test.db"
    engine = create_db_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = create_session_factory(engine)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    clock = FrozenClock(datetime(2026, 11, 19, 11, 0, 0, tzinfo=UTC))
    service = PersistentEscalationService(
        clock=clock,
        session_factory=session_factory,
        config=EscalationConfig(max_pending_per_mandate=3),
    )

    # Attempt to create 8 concurrent escalations for the same mandate
    async def do_create(idx: int) -> tuple[str, str | None]:
        uow_task = SqliteUnitOfWork(session_factory)
        async with uow_task as tx_task:
            try:
                act = _make_action(f"req_race_limit_{idx}", mandate_id="man_race_limit")
                esc = await service.create(act, _make_assessment(), tx_task)
                return "SUCCESS", str(esc.escalation_id)
            except ValueError as e:
                return "ERROR", str(e)

    tasks = [asyncio.create_task(do_create(i)) for i in range(8)]
    results = await asyncio.gather(*tasks)

    successes = [r for r in results if r[0] == "SUCCESS"]
    errors = [r for r in results if r[0] == "ERROR"]

    # Exactly 3 creations succeed
    assert len(successes) == 3
    # The remaining 5 receive E_ESC_LIMIT_REACHED
    assert len(errors) == 5
    for err in errors:
        assert err[1] == ReasonCode.E_ESC_LIMIT_REACHED.value

    await engine.dispose()
