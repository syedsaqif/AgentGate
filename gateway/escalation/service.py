"""Member C escalation workflow service implementation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.audit_models import AuditEvent
from contracts.audit_ports import AuditLog
from contracts.canonical import action_hash
from contracts.common import Clock, OpaqueId, Tx
from contracts.core_models import CanonicalAction
from contracts.escalation_models import Escalation, EscalationDecision, EscalationState
from contracts.reason_codes import ReasonCode
from contracts.risk_models import RiskResult
from gateway.escalation.expiry import expire_due_in_session
from gateway.escalation.models import EscalationRecord
from gateway.escalation.summary import build_trusted_summary


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
        mandate_reader: Any = None,
    ) -> None:
        self.config = config or EscalationConfig()
        self.clock = clock or SystemClock()
        self.session_factory = session_factory
        self.audit_log = audit_log
        self.catalog = catalog
        self.mandate_reader = mandate_reader

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
            await session.flush()
        else:
            self._memory_store[escalation_id] = record

        return esc

    async def get_owned(self, escalation_id: OpaqueId, subject_id: OpaqueId) -> Escalation:
        """Retrieve an owned escalation.

        Wrong owner must not reveal the record (returns KeyError).
        """
        record: EscalationRecord | None = None
        if self.session_factory is not None:
            async with self.session_factory() as session:
                record = await session.get(EscalationRecord, str(escalation_id))
        else:
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
        tx: Tx,
    ) -> Escalation:
        """Atomically decide on a PENDING escalation."""
        session = self._get_session(tx)
        now = self.clock.now()

        # Fetch record
        if session is not None:
            record = await session.get(EscalationRecord, str(escalation_id))
        else:
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
            if res.rowcount == 0:
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
            await self.audit_log.append(event, tx)

        return record.to_dto()

    async def claim_execution(
        self, escalation_id: OpaqueId, expected_hash: str, tx: Tx
    ) -> Escalation:
        """Atomically claim an APPROVED escalation for execution once."""
        session = self._get_session(tx)
        now = self.clock.now()

        if session is not None:
            record = await session.get(EscalationRecord, str(escalation_id))
        else:
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

        if _ensure_utc(record.expires_at) <= now:
            record.state = EscalationState.EXPIRED.value
            if session is not None:
                await session.flush()
            raise ValueError(ReasonCode.E_ESC_EXPIRED.value)

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
            if res.rowcount == 0:
                raise ValueError(ReasonCode.E_ESC_ALREADY_CONSUMED.value)
            record = await session.get(EscalationRecord, str(escalation_id))
            assert record is not None
        else:
            record.state = EscalationState.EXECUTED.value

        return record.to_dto()

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
