"""Member A execution service, write-ahead dispatch, and recovery coordinator."""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.audit_models import AuditEvent
from contracts.audit_ports import AuditLog
from contracts.common import ActionType, Clock, OpaqueId, UnitOfWork
from contracts.core_models import (
    ActionStatus,
    BankResult,
    BankStatus,
    CanonicalAction,
    ExecutionResult,
    MandateStatus,
)
from contracts.core_ports import BankClient, ExecutionPort, MandateReader
from contracts.reason_codes import ReasonCode
from gateway.errors import AgentGateError
from gateway.executor.actions import ActionStore
from gateway.executor.idempotency import (
    claim_idempotency,
    compute_request_fingerprint,
    derive_bank_idempotency_key,
)
from gateway.executor.reservations import ReservationManager
from gateway.executor.schema import ActionTable


def _get_action_str(action: Any) -> str:
    if hasattr(action, "value"):
        return str(action.value)
    return str(action)


class ExecutionService(ExecutionPort):
    """Coordinates write-ahead dispatch, reservations, bank execution, and recovery."""

    def __init__(
        self,
        unit_of_work: UnitOfWork,
        action_store: ActionStore,
        reservation_manager: ReservationManager,
        bank_client: BankClient,
        audit_log: AuditLog,
        mandate_reader: MandateReader,
        clock: Clock,
        session_factory: Any,
        event_bus: Any = None,
    ) -> None:
        self.unit_of_work = unit_of_work
        self.action_store = action_store
        self.reservation_manager = reservation_manager
        self.bank_client = bank_client
        self.audit_log = audit_log
        self.mandate_reader = mandate_reader
        self.clock = clock
        self.session_factory = session_factory
        self.event_bus = event_bus

    async def prepare_action(
        self,
        action: CanonicalAction,
        method: str = "POST",
        path: str = "/v1/agent/actions",
        initial_status: ActionStatus = ActionStatus.RECEIVED,
    ) -> tuple[ActionTable, bool]:
        """Atomically claim idempotency and persist canonical action."""
        fingerprint = compute_request_fingerprint(
            method=method,
            path=path,
            body=action,
        )
        bank_key = derive_bank_idempotency_key(
            customer_id=action.customer_id,
            agent_id=action.agent_id,
            mandate_id=action.mandate_id,
            idempotency_key=action.idempotency_key,
        )

        async with self.unit_of_work as tx:
            session: AsyncSession = tx.session
            idem_rec, is_new = await claim_idempotency(
                session=session,
                customer_id=action.customer_id,
                agent_id=action.agent_id,
                mandate_id=action.mandate_id,
                idempotency_key=action.idempotency_key,
                request_fingerprint=fingerprint,
                request_id=action.request_id,
            )

            if not is_new:
                existing = await self.action_store.get_record(session, idem_rec.request_id)
                if existing is not None:
                    return existing, False

            record = await self.action_store.record_action(
                session=session,
                action=action,
                bank_idempotency_key=bank_key,
                initial_status=initial_status,
            )
            return record, True

    async def execute_stored(
        self, request_id: OpaqueId, escalation_id: OpaqueId | None = None
    ) -> ExecutionResult:
        """Execute a previously stored canonical action through the two-phase commit lifecycle."""
        # 0. Retrieve canonical action
        action = await self.action_store.get(request_id)

        # -------------------------------------------------------------
        # Phase 1: Write-ahead BEGIN IMMEDIATE SQLite transaction
        # -------------------------------------------------------------
        async with self.unit_of_work as tx:
            session: AsyncSession = tx.session
            rec = await self.action_store.get_record(session, request_id)
            if rec is None:
                raise AgentGateError(
                    code=ReasonCode.E_REQ_INVALID,
                    message=f"Action '{request_id}' not found",
                    status_code=404,
                )

            # Check if already completed
            if rec.status == ActionStatus.EXECUTED.value:
                from contracts.common import AuditRef

                b_res = BankResult(
                    status=BankStatus.EXECUTED,
                    bank_operation_id=rec.bank_operation_id,
                    bank_idempotency_key=rec.bank_idempotency_key,
                )
                a_ref = AuditRef(
                    seq=rec.audit_seq or 1,
                    entry_hash=rec.audit_hash or "0" * 64,
                )
                return ExecutionResult(
                    request_id=request_id,
                    status=ActionStatus.EXECUTED,
                    bank_result=b_res,
                    audit_ref=a_ref,
                    escalation_id=rec.escalation_id or escalation_id,
                )

            # Recheck live mandate status and expiry
            snapshot = await self.mandate_reader.get(action.mandate_id, tx=tx)
            now = self.clock.now()

            status_str = str(snapshot.status).upper()
            if status_str == MandateStatus.REVOKED.value:
                raise AgentGateError(
                    code=ReasonCode.E_MANDATE_REVOKED,
                    message="Mandate has been revoked",
                    status_code=403,
                )
            if status_str != MandateStatus.ACTIVE.value:
                raise AgentGateError(
                    code=ReasonCode.E_AUTH_AGENT_SUSPENDED,
                    message=f"Mandate status is {snapshot.status}",
                    status_code=403,
                )
            if now < snapshot.terms.valid_from or now >= snapshot.terms.valid_until:
                raise AgentGateError(
                    code=ReasonCode.E_AUTH_EXPIRED,
                    message="Mandate validity window has expired",
                    status_code=403,
                )

            # Atomic multi-grant reservation
            reservation = await self.reservation_manager.reserve(action, tx)

            # Mark action DISPATCHED (the execution boundary per Section 7.3)
            rec.status = ActionStatus.DISPATCHED.value
            if escalation_id:
                rec.escalation_id = escalation_id

            # Write-ahead audit event: if audit append fails, tx rolls back and NO bank call occurs
            dispatch_event = AuditEvent(
                event_type="action.dispatched",
                request_id=request_id,
                customer_id=action.customer_id,
                agent_id=action.agent_id,
                mandate_id=action.mandate_id,
                mandate_hash=action.mandate_hash,
                safe_payload={
                    "action_type": str(action.action_type),
                    "reservation_id": reservation.reservation_id,
                    "bank_idempotency_key": rec.bank_idempotency_key,
                },
                occurred_at=now,
            )
            audit_ref = await self.audit_log.append(dispatch_event, tx=tx)
            rec.audit_seq = audit_ref.seq
            rec.audit_hash = audit_ref.entry_hash

            bank_key = rec.bank_idempotency_key
            res_id = reservation.reservation_id

        # -------------------------------------------------------------
        # Phase 2: Outside SQLite writer lock -> network call to BankClient
        # -------------------------------------------------------------
        act_str = _get_action_str(action.action_type)
        if act_str == ActionType.BILL_PAY.value:
            bank_res = await self.bank_client.pay_bill(action, bank_key)
        elif act_str == ActionType.TRANSFER.value:
            bank_res = await self.bank_client.transfer(action, bank_key)
        else:
            # Read operations do not call payments/transfers on the bank
            bank_res = BankResult(
                status=BankStatus.EXECUTED,
                bank_idempotency_key=bank_key,
            )

        # -------------------------------------------------------------
        # Phase 3: Completion transaction
        # -------------------------------------------------------------
        async with self.unit_of_work as tx:
            rec = await self.action_store.get_record(tx.session, request_id)
            if bank_res.status == BankStatus.EXECUTED:
                # Commit reservation idempotently
                await self.reservation_manager.commit(res_id, tx)
                if rec:
                    rec.status = ActionStatus.EXECUTED.value
                    rec.bank_operation_id = bank_res.bank_operation_id

                exec_event = AuditEvent(
                    event_type="bank.executed",
                    request_id=request_id,
                    customer_id=action.customer_id,
                    agent_id=action.agent_id,
                    mandate_id=action.mandate_id,
                    mandate_hash=action.mandate_hash,
                    safe_payload={
                        "bank_operation_id": bank_res.bank_operation_id,
                        "bank_result": bank_res.model_dump(mode="json"),
                    },
                    occurred_at=self.clock.now(),
                )
                final_audit = await self.audit_log.append(exec_event, tx=tx)
                if rec:
                    rec.audit_seq = final_audit.seq
                    rec.audit_hash = final_audit.entry_hash

            elif bank_res.status == BankStatus.REJECTED:
                # Definitive business rejection -> release reservation
                await self.reservation_manager.release(res_id, tx)
                if rec:
                    rec.status = ActionStatus.FAILED.value
                    rec.bank_operation_id = bank_res.bank_operation_id

                reason = bank_res.reason_code or ReasonCode.E_BANK_INSUFFICIENT_FUNDS.value
                fail_event = AuditEvent(
                    event_type="bank.failed",
                    request_id=request_id,
                    customer_id=action.customer_id,
                    agent_id=action.agent_id,
                    mandate_id=action.mandate_id,
                    mandate_hash=action.mandate_hash,
                    reason_codes=(reason,),
                    safe_payload={"bank_result": bank_res.model_dump(mode="json")},
                    occurred_at=self.clock.now(),
                )
                final_audit = await self.audit_log.append(fail_event, tx=tx)
                return ExecutionResult(
                    request_id=request_id,
                    status=ActionStatus.FAILED,
                    bank_result=bank_res,
                    audit_ref=final_audit,
                    escalation_id=escalation_id,
                )

            else:
                # BankStatus.UNKNOWN (Timeout or network disconnection)
                # Retain reservation; mark EXECUTION_UNKNOWN
                await self.reservation_manager.mark_unknown(res_id, tx)
                if rec:
                    rec.status = ActionStatus.EXECUTION_UNKNOWN.value

                unk_event = AuditEvent(
                    event_type="bank.unknown",
                    request_id=request_id,
                    customer_id=action.customer_id,
                    agent_id=action.agent_id,
                    mandate_id=action.mandate_id,
                    mandate_hash=action.mandate_hash,
                    reason_codes=(ReasonCode.E_BANK_UNAVAILABLE.value,),
                    safe_payload={"bank_result": bank_res.model_dump(mode="json")},
                    occurred_at=self.clock.now(),
                )
                final_audit = await self.audit_log.append(unk_event, tx=tx)
                return ExecutionResult(
                    request_id=request_id,
                    status=ActionStatus.EXECUTION_UNKNOWN,
                    bank_result=bank_res,
                    audit_ref=final_audit,
                    escalation_id=escalation_id,
                )

        if self.event_bus is not None:
            await self.event_bus.publish(
                "bank.executed",
                {
                    "request_id": request_id,
                    "customer_id": action.customer_id,
                    "agent_id": action.agent_id,
                    "status": "EXECUTED",
                    "audit_ref": final_audit.model_dump(mode="json"),
                },
            )
        return ExecutionResult(
            request_id=request_id,
            status=ActionStatus.EXECUTED,
            bank_result=bank_res,
            audit_ref=final_audit,
            escalation_id=escalation_id,
        )

    async def recover_unresolved(self) -> list[str]:
        """Scan DISPATCHED and EXECUTION_UNKNOWN actions and reconcile with BankClient."""
        resolved: list[str] = []

        async with self.session_factory() as session:
            stmt = select(ActionTable).where(
                or_(
                    ActionTable.status == ActionStatus.DISPATCHED.value,
                    ActionTable.status == ActionStatus.EXECUTION_UNKNOWN.value,
                )
            )
            result = await session.execute(stmt)
            unresolved_actions = list(result.scalars().all())

        for act_rec in unresolved_actions:
            bank_res = await self.bank_client.lookup(act_rec.bank_idempotency_key)

            if bank_res.status == BankStatus.EXECUTED:
                # Bank confirmed execution -> commit reservation
                async with self.unit_of_work as tx:
                    session = tx.session
                    res_row = await self.reservation_manager.get_by_request_id(
                        session, act_rec.request_id
                    )
                    if res_row:
                        await self.reservation_manager.commit(res_row.reservation_id, tx)

                    target = await self.action_store.get_record(session, act_rec.request_id)
                    if target:
                        target.status = ActionStatus.EXECUTED.value
                        target.bank_operation_id = bank_res.bank_operation_id

                    payload = {
                        "bank_operation_id": bank_res.bank_operation_id,
                        "recovered": True,
                    }
                    event = AuditEvent(
                        event_type="bank.executed",
                        request_id=act_rec.request_id,
                        customer_id=act_rec.customer_id,
                        agent_id=act_rec.agent_id,
                        mandate_id=act_rec.mandate_id,
                        mandate_hash=act_rec.mandate_hash,
                        safe_payload=payload,
                        occurred_at=self.clock.now(),
                    )
                    audit_ref = await self.audit_log.append(event, tx=tx)
                    if target:
                        target.audit_seq = audit_ref.seq
                        target.audit_hash = audit_ref.entry_hash

                resolved.append(act_rec.request_id)

            elif (
                bank_res.status == BankStatus.REJECTED
                or bank_res.reason_code == "E_BANK_OP_NOT_FOUND"
            ):
                # Bank confirms never executed or rejected -> release reservation
                async with self.unit_of_work as tx:
                    session = tx.session
                    res_row = await self.reservation_manager.get_by_request_id(
                        session, act_rec.request_id
                    )
                    if res_row:
                        await self.reservation_manager.release(res_row.reservation_id, tx)

                    target = await self.action_store.get_record(session, act_rec.request_id)
                    if target:
                        target.status = ActionStatus.FAILED.value

                    event = AuditEvent(
                        event_type="bank.failed",
                        request_id=act_rec.request_id,
                        customer_id=act_rec.customer_id,
                        agent_id=act_rec.agent_id,
                        mandate_id=act_rec.mandate_id,
                        mandate_hash=act_rec.mandate_hash,
                        reason_codes=(bank_res.reason_code or "E_BANK_OP_NOT_FOUND",),
                        safe_payload={"recovered": True},
                        occurred_at=self.clock.now(),
                    )
                    await self.audit_log.append(event, tx=tx)

                resolved.append(act_rec.request_id)

        return resolved
