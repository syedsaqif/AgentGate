"""Member C escalation workflow service implementation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.audit_models import AuditEvent
from contracts.audit_ports import AuditLog
from contracts.canonical import action_hash
from contracts.common import Clock, OpaqueId, Tx
from contracts.core_models import ActionStatus, CanonicalAction, ExecutionResult, MandateStatus
from contracts.core_ports import ExecutionPort, MandateReader, UsageReader
from contracts.escalation_models import Escalation, EscalationDecision, EscalationState
from contracts.identity_models import AgentStatus
from contracts.identity_ports import AgentRegistry
from contracts.reason_codes import ReasonCode
from contracts.risk_models import RiskResult
from gateway.escalation.expiry import expire_due_in_session
from gateway.escalation.models import EscalationRecord
from gateway.escalation.summary import build_trusted_summary
from gateway.platform.db import SqliteTx


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure datetime is UTC-aware for database comparisons."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class SystemClock(Clock):
    """Default system UTC clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass
class EscalationConfig:
    """Escalation workflow configuration."""

    default_ttl_seconds: int = 300
    scripted_ttl_seconds: int = 60
    max_pending_per_mandate: int = 3
    profile: str = "default"

    @property
    def ttl_seconds(self) -> int:
        return (
            self.scripted_ttl_seconds
            if self.profile == "scripted"
            else self.default_ttl_seconds
        )


class PersistentEscalationService:
    """Persistent escalation service managing approval state machine and SQLite transactions."""

    def __init__(
        self,
        *,
        config: EscalationConfig | None = None,
        clock: Clock | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        audit_log: AuditLog | None = None,
        catalog: Any = None,
        mandate_reader: MandateReader | None = None,
        agent_registry: AgentRegistry | None = None,
        usage_reader: UsageReader | None = None,
        execution_port: ExecutionPort | None = None,
    ) -> None:
        self.config = config or EscalationConfig()
        self.clock = clock or SystemClock()
        self.session_factory = session_factory
        self.audit_log = audit_log
        self.catalog = catalog
        self.mandate_reader = mandate_reader
        self.agent_registry = agent_registry
        self.usage_reader = usage_reader
        self.execution_port = execution_port

        # In-memory store fallback when operating without an active DB session
        self._memory_store: dict[str, EscalationRecord] = {}

    def _get_session(self, tx: Tx | None) -> AsyncSession | None:
        if tx is not None and hasattr(tx, "session") and tx.session is not None:
            return tx.session  # type: ignore[no-any-return]
        return None

    async def create(
        self, action: CanonicalAction, assessment: RiskResult, tx: Tx
    ) -> Escalation:
        """Create a new human escalation for step-up actions."""
        session = self._get_session(tx)
        close_session = False
        if session is None and self.session_factory is not None:
            session = self.session_factory()
            await session.begin()
            close_session = True

        try:
            now = self.clock.now()

            # Enforce max 3 PENDING per mandate in the same transaction
            if session is not None:
                stmt = (
                    select(func.count())
                    .select_from(EscalationRecord)
                    .where(
                        EscalationRecord.mandate_id == action.mandate_id,
                        EscalationRecord.state == EscalationState.PENDING.value,
                    )
                )
                count = (await session.execute(stmt)).scalar() or 0
            else:
                count = sum(
                    1
                    for r in self._memory_store.values()
                    if r.mandate_id == action.mandate_id
                    and r.state == EscalationState.PENDING.value
                )

            if count >= self.config.max_pending_per_mandate:
                raise ValueError(ReasonCode.E_ESC_LIMIT_REACHED.value)

            # Compute bound action hash and expiration
            bound_hash = action_hash(action)
            expires_at = now + timedelta(seconds=self.config.ttl_seconds)

            # Lookup catalog details if available
            payee_card = None
            account_card = None
            if self.catalog is not None:
                if action.params.payee_id:
                    payee_card = self.catalog.get_payee(action.params.payee_id)
                account_card = self.catalog.get_account(action.params.from_account)

            mandate_snapshot = None
            if self.mandate_reader is not None:
                try:
                    mandate_snapshot = await self.mandate_reader.get(action.mandate_id, tx)
                except Exception:
                    mandate_snapshot = None

            summary = build_trusted_summary(
                action=action,
                payee=payee_card,
                account=account_card,
                mandate=mandate_snapshot,
                assessment=assessment,
            )

            escalation_id = f"esc_{action.request_id}"
            reason_codes = tuple(s.reason_code for s in assessment.signals)

            esc = Escalation(
                escalation_id=escalation_id,
                request_id=action.request_id,
                customer_id=action.customer_id,
                agent_id=action.agent_id,
                mandate_id=action.mandate_id,
                action=action,
                action_hash=bound_hash,
                trusted_summary=summary,
                reason_codes=reason_codes,
                expires_at=expires_at,
                state=EscalationState.PENDING,
            )

            record = EscalationRecord.from_dto(esc, created_at=now)

            if session is not None:
                session.add(record)
                if close_session:
                    await session.commit()
                else:
                    await session.flush()
            else:
                self._memory_store[escalation_id] = record

            return esc
        except Exception:
            if close_session and session is not None:
                await session.rollback()
            raise
        finally:
            if close_session and session is not None:
                await session.close()

    async def get_owned(self, escalation_id: OpaqueId, subject_id: OpaqueId) -> Escalation:
        """Retrieve an owned escalation.

        Wrong owner must not reveal the record (returns KeyError).
        """
        record: EscalationRecord | None = None
        if self.session_factory is not None:
            async with self.session_factory() as session:
                record = await session.get(EscalationRecord, str(escalation_id))
        if record is None:
            record = self._memory_store.get(str(escalation_id))

        if record is None or (
            record.customer_id != subject_id and record.agent_id != subject_id
        ):
            raise KeyError(f"Escalation {escalation_id} not found")

        return record.to_dto()

    async def decide(
        self,
        escalation_id: OpaqueId,
        decision: EscalationDecision,
        subject_id: OpaqueId,
        tx: Tx | None = None,
    ) -> Escalation:
        """Atomically decide on a PENDING escalation."""
        session = self._get_session(tx)
        close_session = False
        if session is None and self.session_factory is not None:
            session = self.session_factory()
            await session.begin()
            close_session = True

        tx_to_use = tx or (
            SqliteTx(tx_id=f"tx_dec_{uuid.uuid4().hex[:8]}", session=session)
            if session is not None
            else None
        )

        try:
            now = self.clock.now()

            # Fetch record
            if session is not None:
                record = await session.get(EscalationRecord, str(escalation_id))
            else:
                record = self._memory_store.get(str(escalation_id))
            if record is None:
                record = self._memory_store.get(str(escalation_id))
            if record is None:
                record = self._memory_store.get(str(escalation_id))

            if record is None or record.customer_id != subject_id:
                raise KeyError(f"Escalation {escalation_id} not found")

            # Validate current state
            if record.state != EscalationState.PENDING.value:
                if record.state in (
                    EscalationState.APPROVED.value,
                    EscalationState.EXECUTED.value,
                ):
                    raise ValueError(ReasonCode.E_ESC_ALREADY_CONSUMED.value)
                if record.state == EscalationState.EXPIRED.value:
                    raise ValueError(ReasonCode.E_ESC_EXPIRED.value)
                if record.state == EscalationState.CANCELLED.value:
                    raise ValueError(ReasonCode.E_ESC_CANCELLED.value)
                if record.state == EscalationState.REJECTED.value:
                    raise ValueError(ReasonCode.E_ESC_REJECTED.value)
                raise ValueError(ReasonCode.E_REQ_INVALID.value)

            # Check TTL deadline
            if _ensure_utc(record.expires_at) <= now:
                record.state = EscalationState.EXPIRED.value
                if session is not None:
                    await session.flush()
                raise ValueError(ReasonCode.E_ESC_EXPIRED.value)

            new_state = (
                EscalationState.APPROVED.value
                if decision == EscalationDecision.APPROVE
                else EscalationState.REJECTED.value
            )

            if session is not None:
                stmt = (
                    update(EscalationRecord)
                    .where(
                        EscalationRecord.escalation_id == str(escalation_id),
                        EscalationRecord.state == EscalationState.PENDING.value,
                    )
                    .values(
                        state=new_state,
                        decided_at=now,
                        decided_by=str(subject_id),
                    )
                )
                res = await session.execute(stmt)
                if getattr(res, "rowcount", 0) == 0:
                    raise ValueError(ReasonCode.E_ESC_ALREADY_CONSUMED.value)
                record = await session.get(EscalationRecord, str(escalation_id))
                assert record is not None
            else:
                record.state = new_state
                record.decided_at = now
                record.decided_by = str(subject_id)

            # Emit audit event through the AuditLog port if available
            if self.audit_log is not None:
                dto = record.to_dto()
                event = AuditEvent(
                    event_type=(
                        "escalation.approved"
                        if decision == EscalationDecision.APPROVE
                        else "escalation.rejected"
                    ),
                    request_id=dto.request_id,
                    customer_id=dto.customer_id,
                    agent_id=dto.agent_id,
                    mandate_id=dto.mandate_id,
                    mandate_hash=dto.action.mandate_hash,
                    decision="ALLOW" if decision == EscalationDecision.APPROVE else "DENY",
                    reason_codes=(
                        (ReasonCode.E_ESC_REJECTED.value,)
                        if decision == EscalationDecision.REJECT
                        else ()
                    ),
                    safe_payload={
                        "escalation_id": str(escalation_id),
                        "decision": str(decision),
                        "decided_by": str(subject_id),
                    },
                    occurred_at=now,
                )
                await self.audit_log.append(event, tx_to_use)

            if close_session:
                await session.commit()

            return record.to_dto()
        except Exception:
            if close_session:
                await session.rollback()
            raise
        finally:
            if close_session:
                await session.close()

    async def claim_execution(
        self, escalation_id: OpaqueId, expected_hash: str, tx: Tx | None = None
    ) -> Escalation:
        """Atomically claim an APPROVED escalation for execution once."""
        session = self._get_session(tx)
        close_session = False
        if session is None and self.session_factory is not None:
            session = self.session_factory()
            await session.begin()
            close_session = True

        tx_to_use = tx or (
            SqliteTx(tx_id=f"tx_dec_{uuid.uuid4().hex[:8]}", session=session)
            if session is not None
            else None
        )

        try:
            now = self.clock.now()

            if session is not None:
                record = await session.get(EscalationRecord, str(escalation_id))
            else:
                record = self._memory_store.get(str(escalation_id))
            if record is None:
                record = self._memory_store.get(str(escalation_id))

            if record is None:
                raise KeyError(f"Escalation {escalation_id} not found")

            if record.action_hash != expected_hash:
                raise ValueError(ReasonCode.E_ESC_HASH_MISMATCH.value)

            if record.state == EscalationState.EXECUTED.value:
                raise ValueError(ReasonCode.E_ESC_ALREADY_CONSUMED.value)
            if record.state == EscalationState.EXPIRED.value:
                raise ValueError(ReasonCode.E_ESC_EXPIRED.value)
            if record.state == EscalationState.CANCELLED.value:
                raise ValueError(ReasonCode.E_ESC_CANCELLED.value)
            if record.state == EscalationState.REJECTED.value:
                raise ValueError(ReasonCode.E_ESC_REJECTED.value)
            if record.state != EscalationState.APPROVED.value:
                raise ValueError(ReasonCode.E_REQ_INVALID.value)

            # Validate approval deadline at claim time
            if _ensure_utc(record.expires_at) <= now:
                record.state = EscalationState.EXPIRED.value
                if session is not None:
                    await session.flush()
                raise ValueError(ReasonCode.E_ESC_EXPIRED.value)

            # Validate live mandate state through injected reader port
            if self.mandate_reader is not None:
                try:
                    mandate_snap = await self.mandate_reader.get(record.mandate_id, tx_to_use)
                    status_val = (
                        mandate_snap.status.value
                        if hasattr(mandate_snap.status, "value")
                        else str(mandate_snap.status)
                    )
                    if status_val != MandateStatus.ACTIVE.value:
                        if status_val == MandateStatus.REVOKED.value:
                            raise ValueError(ReasonCode.E_MANDATE_REVOKED.value)
                        if status_val == MandateStatus.SUSPENDED.value:
                            raise ValueError(ReasonCode.E_AUTH_AGENT_SUSPENDED.value)
                        if status_val == MandateStatus.EXPIRED.value:
                            raise ValueError(ReasonCode.E_ESC_EXPIRED.value)
                        raise ValueError(ReasonCode.E_MANDATE_REVOKED.value)

                    valid_from = _ensure_utc(mandate_snap.terms.valid_from)
                    valid_until = _ensure_utc(mandate_snap.terms.valid_until)
                    if now < valid_from or now > valid_until:
                        raise ValueError(ReasonCode.E_MANDATE_REVOKED.value)
                except ValueError:
                    raise
                except Exception as e:
                    raise ValueError(ReasonCode.E_MANDATE_REVOKED.value) from e

            # Validate live agent state through injected registry port
            if self.agent_registry is not None:
                try:
                    try:
                        agent_ident = await self.agent_registry.get(record.agent_id, tx=tx_to_use)
                    except TypeError:
                        agent_ident = await self.agent_registry.get(record.agent_id)
                    agent_status = (
                        agent_ident.status.value
                        if hasattr(agent_ident.status, "value")
                        else str(agent_ident.status)
                    )
                    if agent_status != AgentStatus.ACTIVE.value:
                        raise ValueError(ReasonCode.E_AUTH_AGENT_SUSPENDED.value)
                except ValueError:
                    raise
                except Exception as e:
                    raise ValueError(ReasonCode.E_AUTH_AGENT_SUSPENDED.value) from e

            # Validate remaining budget through injected usage reader port
            if self.usage_reader is not None:
                usages = await self.usage_reader.get(record.mandate_id, tx_to_use)
                dto = record.to_dto()
                action_amount = dto.action.params.amount_paise
                for u in usages:
                    if u.remaining_paise is not None and action_amount > u.remaining_paise:
                        raise ValueError(ReasonCode.E_BANK_INSUFFICIENT_FUNDS.value)
                    if u.remaining_count is not None and u.remaining_count <= 0:
                        raise ValueError(ReasonCode.E_BANK_INSUFFICIENT_FUNDS.value)

            if session is not None:
                stmt = (
                    update(EscalationRecord)
                    .where(
                        EscalationRecord.escalation_id == str(escalation_id),
                        EscalationRecord.state == EscalationState.APPROVED.value,
                    )
                    .values(state=EscalationState.EXECUTED.value)
                )
                res = await session.execute(stmt)
                if getattr(res, "rowcount", 0) == 0:
                    raise ValueError(ReasonCode.E_ESC_ALREADY_CONSUMED.value)
                record = await session.get(EscalationRecord, str(escalation_id))
                assert record is not None
            else:
                record.state = EscalationState.EXECUTED.value

            if close_session:
                await session.commit()

            return record.to_dto()
        except Exception:
            if close_session:
                await session.rollback()
            raise
        finally:
            if close_session:
                await session.close()

    async def execute_approved(
        self,
        escalation_id: OpaqueId,
        tx: Tx | None = None,
        execution_port: ExecutionPort | None = None,
    ) -> ExecutionResult:
        """Handoff an approved escalation to Member A's ExecutionPort.

        Guarantees:
        - Atomic pre-dispatch claim validating hash, live mandate/agent state, budget, and expiry.
        - Only stored request_id and escalation_id are passed (never replacement parameters).
        - No recursion: never calls decide().
        - Duplicate invocation returns stored outcome and never executes twice.
        - Maps bank outcomes to EscalationState (EXECUTED, FAILED, or retaining unknown outcome).
        """
        port = execution_port or self.execution_port
        if port is None:
            raise ValueError(ReasonCode.E_SERVICE_UNAVAILABLE.value)

        session = self._get_session(tx)
        record = None
        if session is not None:
            record = await session.get(EscalationRecord, str(escalation_id))
        elif self.session_factory is not None:
            async with self.session_factory() as sess:
                record = await sess.get(EscalationRecord, str(escalation_id))
        else:
            record = self._memory_store.get(str(escalation_id))

        if record is None:
            raise KeyError(f"Escalation {escalation_id} not found")

        # Duplicate executor job returns the stored outcome without repeating execution
        if record.state == EscalationState.EXECUTED.value and record.result_json is not None:
            return ExecutionResult.model_validate_json(record.result_json)

        # Pre-dispatch claim checks all live constraints atomically
        await self.claim_execution(
            escalation_id=record.escalation_id,
            expected_hash=str(record.action_hash),
            tx=tx,
        )

        # Call ExecutionPort with stored IDs only (no replacement parameters!)
        exec_res = await port.execute_stored(
            request_id=record.request_id,
            escalation_id=str(escalation_id),
        )

        # Map bank outcomes
        if exec_res.status == ActionStatus.EXECUTED:
            new_state = EscalationState.EXECUTED.value
        elif exec_res.status in (ActionStatus.REJECTED, ActionStatus.FAILED):
            new_state = EscalationState.FAILED.value
        elif exec_res.status == ActionStatus.EXECUTION_UNKNOWN:
            # Retain record with unknown outcome, do not label as rejected or offer new key
            new_state = EscalationState.APPROVED.value
        else:
            new_state = EscalationState.FAILED.value

        # Persist terminal outcome
        result_json = exec_res.model_dump_json()
        seq = exec_res.audit_ref.seq if exec_res.audit_ref else None
        entry_hash = exec_res.audit_ref.entry_hash if exec_res.audit_ref else None

        if session is not None:
            stmt = (
                update(EscalationRecord)
                .where(EscalationRecord.escalation_id == str(escalation_id))
                .values(
                    state=new_state,
                    result_json=result_json,
                    audit_ref_seq=seq,
                    audit_ref_hash=entry_hash,
                )
            )
            await session.execute(stmt)
        elif self.session_factory is not None:
            async with self.session_factory() as sess:
                stmt = (
                    update(EscalationRecord)
                    .where(EscalationRecord.escalation_id == str(escalation_id))
                    .values(
                        state=new_state,
                        result_json=result_json,
                        audit_ref_seq=seq,
                        audit_ref_hash=entry_hash,
                    )
                )
                await sess.execute(stmt)
                await sess.commit()
        else:
            record.state = new_state
            record.result_json = result_json
            record.audit_ref_seq = seq
            record.audit_ref_hash = entry_hash

        return exec_res

    async def recover_undispatched(
        self,
        execution_port: ExecutionPort | None = None,
        tx_provider: Any = None,
    ) -> list[ExecutionResult]:
        """Crash recovery on startup: resumes APPROVED-but-undispatched records.

        Rechecks all constraints, dispatches each once, and avoids duplicate execution.
        """
        port = execution_port or self.execution_port
        if port is None:
            return []

        recovered: list[ExecutionResult] = []

        if self.session_factory is not None and tx_provider is not None:
            async with tx_provider as tx:
                session = self._get_session(tx)
                assert session is not None
                stmt = select(EscalationRecord).where(
                    EscalationRecord.state == EscalationState.APPROVED.value
                )
                records = (await session.execute(stmt)).scalars().all()
                for r in records:
                    try:
                        res = await self.execute_approved(r.escalation_id, tx, port)
                        recovered.append(res)
                    except Exception:
                        pass
        else:
            class SimpleRecoveryTx(Tx):

                @property
                def id(self) -> str:
                    return "tx_recovery"

            rec_tx = SimpleRecoveryTx()
            approved_records = [
                r
                for r in list(self._memory_store.values())
                if r.state == EscalationState.APPROVED.value
            ]
            for r in approved_records:
                try:
                    res = await self.execute_approved(r.escalation_id, rec_tx, port)
                    recovered.append(res)
                except Exception:
                    pass

        return recovered

    async def cancel_for_mandate(self, mandate_id: OpaqueId, tx: Tx) -> None:
        """Cancel all pending or approved escalations for the given mandate."""
        session = self._get_session(tx)
        targets = [EscalationState.PENDING.value, EscalationState.APPROVED.value]
        if session is not None:
            stmt = (
                update(EscalationRecord)
                .where(
                    EscalationRecord.mandate_id == str(mandate_id),
                    EscalationRecord.state.in_(targets),
                )
                .values(state=EscalationState.CANCELLED.value)
            )
            await session.execute(stmt)
        else:
            for r in self._memory_store.values():
                if r.mandate_id == mandate_id and r.state in targets:
                    r.state = EscalationState.CANCELLED.value

    async def cancel_for_agent(self, agent_id: OpaqueId, tx: Tx) -> None:
        """Cancel all pending or approved escalations for the given agent."""
        session = self._get_session(tx)
        targets = [EscalationState.PENDING.value, EscalationState.APPROVED.value]
        if session is not None:
            stmt = (
                update(EscalationRecord)
                .where(
                    EscalationRecord.agent_id == str(agent_id),
                    EscalationRecord.state.in_(targets),
                )
                .values(state=EscalationState.CANCELLED.value)
            )
            await session.execute(stmt)
        else:
            for r in self._memory_store.values():
                if r.agent_id == agent_id and r.state in targets:
                    r.state = EscalationState.CANCELLED.value

    async def expire_due(self, now: datetime) -> int:
        """Expire all due PENDING escalations."""
        if self.session_factory is not None:
            async with self.session_factory() as session:
                async with session.begin():
                    return await expire_due_in_session(session, now)
        count = 0
        for r in self._memory_store.values():
            if r.state == EscalationState.PENDING.value and _ensure_utc(r.expires_at) <= now:
                r.state = EscalationState.EXPIRED.value
                count += 1
        return count

    async def list_for_customer(
        self,
        customer_id: OpaqueId,
        status: EscalationState | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[Escalation], str | None]:
        """Return paginated escalations belonging to the customer."""
        bounded_limit = min(max(1, limit), 100)
        items: list[Escalation] = []

        if self.session_factory is not None:
            async with self.session_factory() as session:
                stmt = select(EscalationRecord).where(
                    EscalationRecord.customer_id == str(customer_id)
                )
                if status is not None:
                    status_val = status.value if hasattr(status, "value") else str(status)
                    stmt = stmt.where(EscalationRecord.state == status_val)
                if cursor is not None:
                    stmt = stmt.where(EscalationRecord.escalation_id > cursor)
                stmt = stmt.order_by(EscalationRecord.escalation_id).limit(bounded_limit + 1)
                records = (await session.execute(stmt)).scalars().all()
                for r in records[:bounded_limit]:
                    items.append(r.to_dto())
                next_cursor = (
                    records[bounded_limit].escalation_id
                    if len(records) > bounded_limit
                    else None
                )
                return items, next_cursor
        else:
            filtered = [
                r
                for r in self._memory_store.values()
                if r.customer_id == customer_id
                and (status is None or r.state == str(status))
                and (cursor is None or r.escalation_id > cursor)
            ]
            filtered.sort(key=lambda x: x.escalation_id)
            for r in filtered[:bounded_limit]:
                items.append(r.to_dto())
            next_cursor = (
                filtered[bounded_limit].escalation_id
                if len(filtered) > bounded_limit
                else None
            )
            return items, next_cursor


__all__ = [
    "EscalationConfig",
    "PersistentEscalationService",
    "SystemClock",
]
