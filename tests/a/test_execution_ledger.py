"""Member A execution ledger, atomic reservations, idempotency, and recovery test suite."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from contracts.common import ActionType
from contracts.core_models import (
    ActionParams,
    ActionStatus,
    BankResult,
    BankStatus,
    CanonicalAction,
    Grant,
    Intent,
    MandateSnapshot,
    MandateStatus,
    MandateTerms,
    StepUpRules,
)
from contracts.reason_codes import ReasonCode
from gateway.errors import AgentGateError
from gateway.executor.actions import ActionStore
from gateway.executor.idempotency import derive_bank_idempotency_key
from gateway.executor.reservations import ReservationManager
from gateway.executor.service import ExecutionService
from gateway.main import create_app
from gateway.platform.db import Base
from gateway.platform.startup import init_database
from tests.support.a.providers import FakeBank
from tests.support.b.providers import MemoryAuditLog
from tests.support.platform import FrozenClock


def make_test_mandate(
    mandate_id: str = "mnd_exec_1",
    customer_id: str = "cust_001",
    agent_id: str = "agt_billpal",
    max_per_txn_paise: int = 200000,
    max_total_paise: int = 400000,
    max_count: int = 10,
    grants: tuple[Grant, ...] | None = None,
) -> MandateSnapshot:
    """Create test MandateSnapshot with specified grants/caps."""
    now = datetime(2026, 11, 1, 0, 0, 0, tzinfo=UTC)
    if grants is None:
        grants = (
            Grant(
                grant_id="grant_b1",
                action=ActionType.BILL_PAY,
                account_ids=("ACC-001",),
                payee_ids=("BILL-ELEC", "BILL-MOBILE"),
                max_per_txn_paise=max_per_txn_paise,
                max_total_paise=max_total_paise,
                max_count=max_count,
            ),
        )

    terms = MandateTerms(
        mandate_id=mandate_id,
        version=1,
        customer_id=customer_id,
        agent_id=agent_id,
        valid_from=now,
        valid_until=now + timedelta(days=60),
        grants=grants,
        step_up=StepUpRules(new_payee_requires_approval=False),
        risk_profile="balanced",
    )

    return MandateSnapshot(
        terms=terms,
        status=MandateStatus.ACTIVE,
        mandate_hash="sha256:0000111122223333444455556666777788889999aaaabbbbccccddddeeeeffff",
        sig="sig_dummy",
        key_id="key_1",
    )


def make_canonical_action(
    request_id: str = "req_exec_1",
    idempotency_key: str = "idemp_exec_1",
    mandate_id: str = "mnd_exec_1",
    amount_paise: int = 150000,
    action_type: ActionType = ActionType.BILL_PAY,
    payee_id: str = "BILL-ELEC",
    note: str = "monthly electricity",
) -> CanonicalAction:
    """Create CanonicalAction DTO."""
    return CanonicalAction(
        request_id=request_id,
        customer_id="cust_001",
        agent_id="agt_billpal",
        mandate_id=mandate_id,
        mandate_hash="sha256:0000111122223333444455556666777788889999aaaabbbbccccddddeeeeffff",
        action_type=action_type,
        params=ActionParams(
            from_account="ACC-001",
            payee_id=payee_id,
            amount_paise=amount_paise,
            currency="INR",
            reference="REF-001",
        ),
        intent=Intent(task="pay_bill", category="utilities", note=note),
        idempotency_key=idempotency_key,
        received_at=datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC),
    )


@pytest.fixture
async def exec_test_env(tmp_path: Path):
    """Fixture spinning up real SQLite WAL database and execution service harness."""
    db_file = tmp_path / "test_execution_ledger.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"

    app = create_app(
        profile="test",
        overrides={"database": {"gateway_url": db_url, "wal": True}},
    )

    engine = app.state.engine
    await init_database(engine, Base.metadata)

    fake_bank = FakeBank()
    audit_log = MemoryAuditLog()
    clock = FrozenClock(datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC))

    action_store = ActionStore(app.state.session_factory)
    mandate_reader = app.state.deps.mandate_reader
    res_manager = ReservationManager(mandate_reader)

    exec_service = ExecutionService(
        unit_of_work=app.state.deps.unit_of_work,
        action_store=action_store,
        reservation_manager=res_manager,
        bank_client=fake_bank,
        audit_log=audit_log,
        mandate_reader=mandate_reader,
        clock=clock,
        session_factory=app.state.session_factory,
    )

    yield {
        "app": app,
        "engine": engine,
        "session_factory": app.state.session_factory,
        "uow": app.state.deps.unit_of_work,
        "mandate_service": app.state.deps.mandate_service,
        "exec_service": exec_service,
        "res_manager": res_manager,
        "action_store": action_store,
        "fake_bank": fake_bank,
        "audit_log": audit_log,
        "clock": clock,
    }

    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrency_five_parallel_requests_s3_core(exec_test_env) -> None:
    """Core S3 concurrency requirement:

    total cap 400,000 paise, per-txn 200,000 paise, 5 parallel 150,000 requests.
    Exactly 2 execute, 3 fail budget, zero overspend. Final committed == 300,000, reserved == 0.
    """
    env = exec_test_env
    exec_srv: ExecutionService = env["exec_service"]
    session_factory = env["session_factory"]

    # 1. Create mandate in DB
    mandate_snap = make_test_mandate(
        mandate_id="mnd_s3",
        max_per_txn_paise=200000,
        max_total_paise=400000,
        max_count=10,
    )
    # Seed mandate and usage rows
    async with session_factory() as s:
        from gateway.mandates.schema import MandateTable, MandateUsageTable

        m_row = MandateTable(
            mandate_id="mnd_s3",
            version=1,
            customer_id="cust_001",
            agent_id="agt_billpal",
            status="ACTIVE",
            terms_json=mandate_snap.terms.model_dump_json(),
            mandate_hash=mandate_snap.mandate_hash,
            sig=mandate_snap.sig,
            key_id=mandate_snap.key_id,
        )
        s.add(m_row)
        u_row = MandateUsageTable(
            mandate_id="mnd_s3",
            grant_id="grant_b1",
            window_key="mnd_s3:grant_b1:mandate",
            committed_paise=0,
            reserved_paise=0,
            committed_count=0,
            reserved_count=0,
        )
        s.add(u_row)
        await s.commit()

    # 2. Prepare 5 distinct actions with unique idempotency keys
    actions = [
        make_canonical_action(
            request_id=f"req_c_{i}",
            idempotency_key=f"idemp_c_{i}",
            mandate_id="mnd_s3",
            amount_paise=150000,
        )
        for i in range(1, 6)
    ]

    for act in actions:
        await exec_srv.prepare_action(act)

    # 3. Fire all 5 executions in parallel
    results = await asyncio.gather(
        *[exec_srv.execute_stored(act.request_id) for act in actions],
        return_exceptions=True,
    )

    success_count = sum(
        1 for r in results if not isinstance(r, Exception) and r.status == ActionStatus.EXECUTED
    )
    failure_count = sum(
        1
        for r in results
        if isinstance(r, Exception)
        or (hasattr(r, "status") and r.status != ActionStatus.EXECUTED)
    )

    # Exactly 2 succeed (2 * 150_000 = 300_000 <= 400_000), 3 fail budget
    assert success_count == 2
    assert failure_count == 3

    # 4. Verify DB usage row: final committed == 300_000, reserved == 0
    async with session_factory() as s:
        q = "SELECT committed_paise, reserved_paise FROM mandate_usage WHERE mandate_id = 'mnd_s3'"
        row = (await s.execute(text(q))).fetchone()
        assert row is not None
        assert row[0] == 300000
        assert row[1] == 0


@pytest.mark.asyncio
async def test_multi_grant_atomic_rollback_on_partial_failure(exec_test_env) -> None:
    """Overlapping grants: if Grant 2 lacks budget, Grant 1's budget must not be modified."""
    env = exec_test_env
    res_mgr: ReservationManager = env["res_manager"]
    uow = env["uow"]
    session_factory = env["session_factory"]

    # Mandate with 2 grants for bill_pay:
    # Grant 1: max_total 500,000
    # Grant 2: max_total 100,000
    g1 = Grant(
        grant_id="grant_g1",
        action=ActionType.BILL_PAY,
        account_ids=("ACC-001",),
        payee_ids=("BILL-ELEC",),
        max_per_txn_paise=500000,
        max_total_paise=500000,
        max_count=10,
    )
    g2 = Grant(
        grant_id="grant_g2",
        action=ActionType.BILL_PAY,
        account_ids=("ACC-001",),
        payee_ids=("BILL-ELEC",),
        max_per_txn_paise=100000,
        max_total_paise=100000,
        max_count=10,
    )
    mandate_snap = make_test_mandate(
        mandate_id="mnd_multi",
        grants=(g1, g2),
    )

    # Seed DB
    async with session_factory() as s:
        from gateway.mandates.schema import MandateTable, MandateUsageTable

        s.add(
            MandateTable(
                mandate_id="mnd_multi",
                version=1,
                customer_id="cust_001",
                agent_id="agt_billpal",
                status="ACTIVE",
                terms_json=mandate_snap.terms.model_dump_json(),
                mandate_hash=mandate_snap.mandate_hash,
                sig=mandate_snap.sig,
                key_id=mandate_snap.key_id,
            )
        )
        s.add(
            MandateUsageTable(
                mandate_id="mnd_multi",
                grant_id="grant_g1",
                window_key="mnd_multi:grant_g1:mandate",
                committed_paise=0,
                reserved_paise=0,
                committed_count=0,
                reserved_count=0,
            )
        )
        s.add(
            MandateUsageTable(
                mandate_id="mnd_multi",
                grant_id="grant_g2",
                window_key="mnd_multi:grant_g2:mandate",
                committed_paise=0,
                reserved_paise=0,
                committed_count=0,
                reserved_count=0,
            )
        )
        await s.commit()

    # Attempt to reserve 150,000 paise (Grant 1 would allow, Grant 2 exceeds 100k cap)
    act = make_canonical_action(
        request_id="req_multi_1",
        mandate_id="mnd_multi",
        amount_paise=150000,
    )

    with pytest.raises(AgentGateError) as exc_info:
        async with uow as tx:
            await res_mgr.reserve(act, tx)

    assert exc_info.value.code == ReasonCode.E_REQ_INVALID

    # Verify zero partial reservation: Grant 1 reserved_paise is 0!
    async with session_factory() as s:
        q = "SELECT grant_id, reserved_paise FROM mandate_usage WHERE mandate_id = 'mnd_multi'"
        rows = (await s.execute(text(q))).fetchall()
        for r in rows:
            assert r[1] == 0


@pytest.mark.asyncio
async def test_idempotency_same_key_same_body_and_conflict(exec_test_env) -> None:
    """Same idempotency key + same body returns existing; changed body raises 409."""
    env = exec_test_env
    exec_srv: ExecutionService = env["exec_service"]

    act1 = make_canonical_action(
        request_id="req_idem_1",
        idempotency_key="idemp_key_alpha",
        amount_paise=50000,
    )

    # 1. First prepare -> new record
    rec1, is_new = await exec_srv.prepare_action(act1)
    assert is_new is True
    assert rec1.request_id == "req_idem_1"

    # 2. Second prepare with identical payload -> returns existing
    rec2, is_new2 = await exec_srv.prepare_action(act1)
    assert is_new2 is False
    assert rec2.request_id == "req_idem_1"

    # 3. Third prepare with same key but changed amount -> 409 Conflict
    act_changed = make_canonical_action(
        request_id="req_idem_conflict",
        idempotency_key="idemp_key_alpha",
        amount_paise=99999,  # Changed amount
    )
    with pytest.raises(AgentGateError) as exc_info:
        await exec_srv.prepare_action(act_changed)

    assert exc_info.value.code == ReasonCode.E_REQ_IDEMPOTENCY_CONFLICT
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_repeated_completion_callbacks_are_idempotent(exec_test_env) -> None:
    """Repeated calls to commit or release do not double-count or double-release."""
    env = exec_test_env
    exec_srv: ExecutionService = env["exec_service"]
    res_mgr: ReservationManager = env["res_manager"]
    uow = env["uow"]
    session_factory = env["session_factory"]

    # Seed mandate
    m_snap = make_test_mandate(mandate_id="mnd_rep", max_total_paise=1000000)
    async with session_factory() as s:
        from gateway.mandates.schema import MandateTable, MandateUsageTable

        s.add(
            MandateTable(
                mandate_id="mnd_rep",
                version=1,
                customer_id="cust_001",
                agent_id="agt_billpal",
                status="ACTIVE",
                terms_json=m_snap.terms.model_dump_json(),
                mandate_hash=m_snap.mandate_hash,
                sig=m_snap.sig,
                key_id=m_snap.key_id,
            )
        )
        s.add(
            MandateUsageTable(
                mandate_id="mnd_rep",
                grant_id="grant_b1",
                window_key="mnd_rep:grant_b1:mandate",
                committed_paise=0,
                reserved_paise=0,
                committed_count=0,
                reserved_count=0,
            )
        )
        await s.commit()

    act = make_canonical_action(
        request_id="req_rep_1",
        mandate_id="mnd_rep",
        amount_paise=50000,
    )
    await exec_srv.prepare_action(act)

    # 1. Reserve
    async with uow as tx:
        res = await res_mgr.reserve(act, tx)

    q_rep = "SELECT committed_paise, reserved_paise FROM mandate_usage WHERE mandate_id = 'mnd_rep'"

    # Check reserved
    async with session_factory() as s:
        row = (await s.execute(text(q_rep))).fetchone()
        assert row[0] == 0
        assert row[1] == 50000

    # 2. Commit once
    async with uow as tx:
        await res_mgr.commit(res.reservation_id, tx)

    async with session_factory() as s:
        row = (await s.execute(text(q_rep))).fetchone()
        assert row[0] == 50000
        assert row[1] == 0

    # 3. Commit second time -> safe no-op!
    async with uow as tx:
        await res_mgr.commit(res.reservation_id, tx)

    async with session_factory() as s:
        row = (await s.execute(text(q_rep))).fetchone()
        assert row[0] == 50000
        assert row[1] == 0

    # 4. Release after commit -> safe no-op
    async with uow as tx:
        await res_mgr.release(res.reservation_id, tx)

    async with session_factory() as s:
        row = (await s.execute(text(q_rep))).fetchone()
        assert row[0] == 50000
        assert row[1] == 0


@pytest.mark.asyncio
async def test_pre_dispatch_audit_failure_prevents_bank_call(exec_test_env) -> None:
    """If write-ahead audit append fails, transaction rolls back and BankClient is NEVER called."""
    env = exec_test_env
    exec_srv: ExecutionService = env["exec_service"]
    fake_bank: FakeBank = env["fake_bank"]
    audit_log: MemoryAuditLog = env["audit_log"]
    session_factory = env["session_factory"]

    # Seed mandate
    m_snap = make_test_mandate(mandate_id="mnd_audit_fail")
    async with session_factory() as s:
        from gateway.mandates.schema import MandateTable, MandateUsageTable

        s.add(
            MandateTable(
                mandate_id="mnd_audit_fail",
                version=1,
                customer_id="cust_001",
                agent_id="agt_billpal",
                status="ACTIVE",
                terms_json=m_snap.terms.model_dump_json(),
                mandate_hash=m_snap.mandate_hash,
                sig=m_snap.sig,
                key_id=m_snap.key_id,
            )
        )
        s.add(
            MandateUsageTable(
                mandate_id="mnd_audit_fail",
                grant_id="grant_b1",
                window_key="mnd_audit_fail:grant_b1:mandate",
                committed_paise=0,
                reserved_paise=0,
                committed_count=0,
                reserved_count=0,
            )
        )
        await s.commit()

    act = make_canonical_action(
        request_id="req_audit_fail",
        mandate_id="mnd_audit_fail",
        amount_paise=50000,
    )
    await exec_srv.prepare_action(act)

    # Inject failure into write-ahead audit log
    audit_log.fail_append = True

    with pytest.raises(RuntimeError, match="injected audit failure"):
        await exec_srv.execute_stored("req_audit_fail")

    # Bank was NEVER called
    assert len(fake_bank.calls) == 0

    # Usage was rolled back
    async with session_factory() as s:
        q = "SELECT reserved_paise FROM mandate_usage WHERE mandate_id = 'mnd_audit_fail'"
        row = (await s.execute(text(q))).fetchone()
        assert row[0] == 0


@pytest.mark.asyncio
async def test_recovery_commits_crashed_dispatched_action(exec_test_env) -> None:
    """Action crashed in DISPATCHED state -> recovery commits without re-debiting."""
    env = exec_test_env
    exec_srv: ExecutionService = env["exec_service"]
    fake_bank: FakeBank = env["fake_bank"]
    res_mgr: ReservationManager = env["res_manager"]
    uow = env["uow"]
    session_factory = env["session_factory"]

    # Seed mandate
    m_snap = make_test_mandate(mandate_id="mnd_recov")
    async with session_factory() as s:
        from gateway.mandates.schema import MandateTable, MandateUsageTable

        s.add(
            MandateTable(
                mandate_id="mnd_recov",
                version=1,
                customer_id="cust_001",
                agent_id="agt_billpal",
                status="ACTIVE",
                terms_json=m_snap.terms.model_dump_json(),
                mandate_hash=m_snap.mandate_hash,
                sig=m_snap.sig,
                key_id=m_snap.key_id,
            )
        )
        s.add(
            MandateUsageTable(
                mandate_id="mnd_recov",
                grant_id="grant_b1",
                window_key="mnd_recov:grant_b1:mandate",
                committed_paise=0,
                reserved_paise=0,
                committed_count=0,
                reserved_count=0,
            )
        )
        await s.commit()

    act = make_canonical_action(
        request_id="req_recov_1",
        idempotency_key="idemp_recov_1",
        mandate_id="mnd_recov",
        amount_paise=75000,
    )
    await exec_srv.prepare_action(act)

    # Simulate crash right after dispatch:
    # 1. Reserve and mark DISPATCHED in DB
    async with uow as tx:
        await res_mgr.reserve(act, tx)
        stmt = text("UPDATE actions SET status = 'DISPATCHED' WHERE request_id = 'req_recov_1'")
        await tx.session.execute(stmt)

    # 2. Record bank execution directly on fake_bank
    b_key = derive_bank_idempotency_key("cust_001", "agt_billpal", "mnd_recov", "idemp_recov_1")
    fake_bank.recorded_operations[b_key] = BankResult(
        status=BankStatus.EXECUTED,
        bank_operation_id="bank_op_recovered_001",
        bank_idempotency_key=b_key,
        balance_paise=12000000,
    )

    # 3. Run recover_unresolved()
    recovered = await exec_srv.recover_unresolved()
    assert "req_recov_1" in recovered

    # 4. Verify committed in DB
    async with session_factory() as s:
        q_act = "SELECT status, bank_operation_id FROM actions WHERE request_id = 'req_recov_1'"
        act_row = (await s.execute(text(q_act))).fetchone()
        assert act_row[0] == "EXECUTED"
        assert act_row[1] == "bank_op_recovered_001"

        q_u = (
            "SELECT committed_paise, reserved_paise "
            "FROM mandate_usage WHERE mandate_id = 'mnd_recov'"
        )
        u_row = (await s.execute(text(q_u))).fetchone()
        assert u_row[0] == 75000
        assert u_row[1] == 0


@pytest.mark.asyncio
async def test_unknown_timeout_retains_reservation_and_blocks_overspend(exec_test_env) -> None:
    """Bank UNKNOWN status retains reserved budget, blocks overspend, and resolves on recovery."""
    env = exec_test_env
    exec_srv: ExecutionService = env["exec_service"]
    fake_bank: FakeBank = env["fake_bank"]
    session_factory = env["session_factory"]

    # Cap: total 100,000 paise
    m_snap = make_test_mandate(mandate_id="mnd_unk", max_total_paise=100000)
    async with session_factory() as s:
        from gateway.mandates.schema import MandateTable, MandateUsageTable

        s.add(
            MandateTable(
                mandate_id="mnd_unk",
                version=1,
                customer_id="cust_001",
                agent_id="agt_billpal",
                status="ACTIVE",
                terms_json=m_snap.terms.model_dump_json(),
                mandate_hash=m_snap.mandate_hash,
                sig=m_snap.sig,
                key_id=m_snap.key_id,
            )
        )
        s.add(
            MandateUsageTable(
                mandate_id="mnd_unk",
                grant_id="grant_b1",
                window_key="mnd_unk:grant_b1:mandate",
                committed_paise=0,
                reserved_paise=0,
                committed_count=0,
                reserved_count=0,
            )
        )
        await s.commit()

    # 1. First action for 80,000 paise with bank timeout
    act1 = make_canonical_action(
        request_id="req_unk_1",
        idempotency_key="idemp_unk_1",
        mandate_id="mnd_unk",
        amount_paise=80000,
    )
    await exec_srv.prepare_action(act1)

    fake_bank.timeout = True
    res1 = await exec_srv.execute_stored("req_unk_1")
    assert res1.status == ActionStatus.EXECUTION_UNKNOWN

    q_unk = "SELECT committed_paise, reserved_paise FROM mandate_usage WHERE mandate_id = 'mnd_unk'"

    # Verify reservation is retained: reserved_paise == 80_000
    async with session_factory() as s:
        u_row = (await s.execute(text(q_unk))).fetchone()
        assert u_row[0] == 0
        assert u_row[1] == 80000

    # 2. Second action for 30,000 paise:
    # 80,000 reserved + 30,000 new = 110,000 > 100,000 cap -> MUST BE BLOCKED!
    act2 = make_canonical_action(
        request_id="req_unk_2",
        idempotency_key="idemp_unk_2",
        mandate_id="mnd_unk",
        amount_paise=30000,
    )
    await exec_srv.prepare_action(act2)

    with pytest.raises(AgentGateError) as exc_info:
        await exec_srv.execute_stored("req_unk_2")

    assert exc_info.value.code == ReasonCode.E_REQ_INVALID

    # 3. Bank confirms it was executed -> run recovery
    b_key1 = derive_bank_idempotency_key("cust_001", "agt_billpal", "mnd_unk", "idemp_unk_1")
    fake_bank.timeout = False
    fake_bank.recorded_operations[b_key1] = BankResult(
        status=BankStatus.EXECUTED,
        bank_operation_id="bank_op_unk_resolved",
        bank_idempotency_key=b_key1,
    )

    resolved = await exec_srv.recover_unresolved()
    assert "req_unk_1" in resolved

    # Now committed == 80_000, reserved == 0
    async with session_factory() as s:
        u_row = (await s.execute(text(q_unk))).fetchone()
        assert u_row[0] == 80000
        assert u_row[1] == 0
