"""Member A cross-owner lifecycle coordinator implementing Lifecycle protocol."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.audit_models import AuditEvent
from contracts.audit_ports import AuditLog
from contracts.common import AuditRef, Clock, OpaqueId, Tx, UnitOfWork
from contracts.core_ports import Lifecycle
from contracts.escalation_models import Escalation
from contracts.escalation_ports import EscalationService
from contracts.identity_models import AgentStatus
from contracts.identity_ports import AgentRegistry, TokenService
from gateway.events.bus import EventBus
from gateway.mandates.schema import MandateTable


class LifecycleCoordinator(Lifecycle):
    """Coordinates lifecycle transitions across mandates, tokens, agent registry, and escalations.

    Adheres to Section 7.3: executes mutations in ONE gateway unit of work transaction,
    then publishes events to the event bus strictly after commit.
    """

    def __init__(
        self,
        unit_of_work: UnitOfWork,
        audit_log: AuditLog,
        clock: Clock,
        event_bus: EventBus | None = None,
        token_service: TokenService | None = None,
        escalation_service: EscalationService | None = None,
        agent_registry: AgentRegistry | None = None,
    ) -> None:
        self.unit_of_work = unit_of_work
        self.audit_log = audit_log
        self.clock = clock
        self.event_bus = event_bus
        self.token_service = token_service
        self.escalation_service = escalation_service
        self.agent_registry = agent_registry

    async def revoke_mandate(
        self,
        mandate_id: OpaqueId,
        reason_codes: tuple[str, ...] = (),
        actor: str | None = None,
        reason: str | None = None,
    ) -> AuditRef | None:
        """Revoke a mandate, its issued tokens, and cancel pending approvals in ONE transaction."""
        event_to_publish: dict[str, Any] | None = None
        audit_ref: AuditRef | None = None

        async with self.unit_of_work as tx:
            session: AsyncSession = tx.session
            stmt = select(MandateTable).where(MandateTable.mandate_id == mandate_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return None

            is_already_revoked = row.status == "REVOKED"
            row.status = "REVOKED"
            await session.flush()

            # Revoke tokens associated with mandate
            if self.token_service is not None:
                await self.token_service.revoke_for_mandate(mandate_id, tx)

            # Cancel pending approvals associated with mandate
            if self.escalation_service is not None:
                await self.escalation_service.cancel_for_mandate(mandate_id, tx)

            # Record audit event
            audit_event = AuditEvent(
                event_type="mandate.revoked",
                customer_id=row.customer_id,
                agent_id=row.agent_id,
                mandate_id=mandate_id,
                mandate_hash=row.mandate_hash,
                reason_codes=reason_codes,
                safe_payload={
                    "actor": actor or row.customer_id,
                    "reason": reason or "Customer requested revocation",
                    "idempotent_replay": is_already_revoked,
                },
                occurred_at=self.clock.now(),
            )
            audit_ref = await self.audit_log.append(audit_event, tx=tx)
            event_to_publish = {
                "mandate_id": mandate_id,
                "customer_id": row.customer_id,
                "agent_id": row.agent_id,
                "status": "REVOKED",
                "audit_ref": audit_ref.model_dump(mode="json") if audit_ref else None,
            }

        # Publish event strictly AFTER commit
        if self.event_bus is not None and event_to_publish is not None:
            await self.event_bus.publish("mandate.revoked", event_to_publish)

        return audit_ref

    async def suspend_agent(
        self,
        agent_id: OpaqueId,
        reason_codes: tuple[str, ...] = (),
        actor: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Suspend an agent globally, revoke its tokens, and cancel approvals in ONE transaction."""
        event_to_publish: dict[str, Any] | None = None

        async with self.unit_of_work as tx:
            # 1. Update status in AgentRegistry
            if self.agent_registry is not None:
                await self.agent_registry.set_status(agent_id, AgentStatus.SUSPENDED, tx)

            # 2. Revoke all tokens for this agent
            if self.token_service is not None:
                await self.token_service.revoke_for_agent(agent_id, tx)

            # 3. Cancel all pending approvals for this agent
            if self.escalation_service is not None:
                await self.escalation_service.cancel_for_agent(agent_id, tx)

            # 4. Append audit event
            audit_event = AuditEvent(
                event_type="agent.suspended",
                agent_id=agent_id,
                reason_codes=reason_codes,
                safe_payload={
                    "actor": actor or "ops",
                    "reason": reason or "Circuit breaker triggered or ops suspension",
                },
                occurred_at=self.clock.now(),
            )
            audit_ref = await self.audit_log.append(audit_event, tx=tx)
            event_to_publish = {
                "agent_id": agent_id,
                "status": "SUSPENDED",
                "audit_ref": audit_ref.model_dump(mode="json") if audit_ref else None,
            }

        # Publish event strictly AFTER commit
        if self.event_bus is not None and event_to_publish is not None:
            await self.event_bus.publish("agent.suspended", event_to_publish)

    async def suspend_customer_delegation(
        self,
        customer_id: OpaqueId,
        agent_id: OpaqueId,
        reason_codes: tuple[str, ...] = (),
        actor: str | None = None,
        reason: str | None = None,
        *,
        before_suspend: Callable[[Tx | None], Awaitable[Escalation]] | None = None,
    ) -> Escalation | None:
        """Optionally decide the approval, then revoke only this customer's delegation."""
        events_to_publish: list[dict[str, Any]] = []

        async with self.unit_of_work as tx:
            decided = await before_suspend(tx) if before_suspend is not None else None
            session: AsyncSession = tx.session
            stmt = select(MandateTable).where(
                MandateTable.customer_id == customer_id,
                MandateTable.agent_id == agent_id,
                MandateTable.status == "ACTIVE",
            )
            result = await session.execute(stmt)
            mandates = list(result.scalars().all())

            for m in mandates:
                m.status = "REVOKED"
                await session.flush()

                if self.token_service is not None:
                    await self.token_service.revoke_for_mandate(m.mandate_id, tx)

                if self.escalation_service is not None:
                    await self.escalation_service.cancel_for_mandate(m.mandate_id, tx)

                a_event = AuditEvent(
                    event_type="mandate.revoked",
                    customer_id=customer_id,
                    agent_id=agent_id,
                    mandate_id=m.mandate_id,
                    mandate_hash=m.mandate_hash,
                    reason_codes=reason_codes,
                    safe_payload={
                        "actor": actor or customer_id,
                        "reason": reason or "Customer rejected and suspended delegation",
                    },
                    occurred_at=self.clock.now(),
                )
                a_ref = await self.audit_log.append(a_event, tx=tx)
                events_to_publish.append(
                    {
                        "mandate_id": m.mandate_id,
                        "customer_id": customer_id,
                        "agent_id": agent_id,
                        "status": "REVOKED",
                        "audit_ref": a_ref.model_dump(mode="json") if a_ref else None,
                    }
                )

        # Publish events strictly AFTER commit
        if self.event_bus is not None:
            for ev in events_to_publish:
                await self.event_bus.publish("mandate.revoked", ev)
        return decided

    async def reinstate_agent(
        self,
        agent_id: OpaqueId,
        actor: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Reinstate agent to ACTIVE; does NOT revive old tokens, approvals, or mandates."""
        event_to_publish: dict[str, Any] | None = None

        async with self.unit_of_work as tx:
            if self.agent_registry is not None:
                await self.agent_registry.set_status(agent_id, AgentStatus.ACTIVE, tx)

            audit_event = AuditEvent(
                event_type="agent.reinstated",
                agent_id=agent_id,
                safe_payload={
                    "actor": actor or "ops",
                    "reason": reason or "Ops reinstated agent",
                },
                occurred_at=self.clock.now(),
            )
            audit_ref = await self.audit_log.append(audit_event, tx=tx)
            event_to_publish = {
                "agent_id": agent_id,
                "status": "ACTIVE",
                "audit_ref": audit_ref.model_dump(mode="json") if audit_ref else None,
            }

        if self.event_bus is not None and event_to_publish is not None:
            await self.event_bus.publish("agent.reinstated", event_to_publish)
