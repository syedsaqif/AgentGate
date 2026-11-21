"""Member A action storage and ActionReader port implementation."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.canonical import action_hash
from contracts.common import OpaqueId, Tx
from contracts.core_models import ActionStatus, CanonicalAction
from contracts.core_ports import ActionReader
from contracts.reason_codes import ReasonCode
from gateway.errors import AgentGateError
from gateway.executor.schema import ActionTable


class ActionStore(ActionReader):
    """Storage and reader port for CanonicalAction records and execution status."""

    def __init__(self, session_factory: Any) -> None:
        self.session_factory = session_factory

    async def get(
        self, request_id: OpaqueId, tx: Tx | None = None
    ) -> CanonicalAction:
        """Retrieve stored CanonicalAction by request_id."""
        if tx is not None:
            session: AsyncSession = tx.session
            return await self._get_with_session(session, request_id)

        async with self.session_factory() as session:
            return await self._get_with_session(session, request_id)

    async def _get_with_session(
        self, session: AsyncSession, request_id: str
    ) -> CanonicalAction:
        stmt = select(ActionTable).where(ActionTable.request_id == request_id)
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            raise AgentGateError(
                code=ReasonCode.E_REQ_INVALID,
                message=f"Action '{request_id}' not found",
                status_code=404,
            )
        data = json.loads(row.canonical_json)
        return CanonicalAction.model_validate(data)

    async def get_record(
        self, session: AsyncSession, request_id: str
    ) -> ActionTable | None:
        """Retrieve ActionTable ORM row for mutation."""
        stmt = select(ActionTable).where(ActionTable.request_id == request_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def record_action(
        self,
        session: AsyncSession,
        action: CanonicalAction,
        bank_idempotency_key: str,
        initial_status: ActionStatus = ActionStatus.RECEIVED,
    ) -> ActionTable:
        """Persist a newly received CanonicalAction into the actions table."""
        # Unverified note is extracted and stored separately per Section 6.3
        note = action.intent.note
        a_hash = action_hash(action)
        action_json = action.model_dump_json()

        row = ActionTable(
            request_id=action.request_id,
            customer_id=action.customer_id,
            agent_id=action.agent_id,
            mandate_id=action.mandate_id,
            mandate_hash=action.mandate_hash,
            action_type=str(action.action_type),
            canonical_json=action_json,
            action_hash=a_hash,
            note=note,
            idempotency_key=action.idempotency_key,
            bank_idempotency_key=bank_idempotency_key,
            status=initial_status.value,
        )
        session.add(row)
        await session.flush()
        return row

    async def update_status(
        self,
        session: AsyncSession,
        request_id: str,
        status: ActionStatus,
        bank_operation_id: str | None = None,
        audit_seq: int | None = None,
        audit_hash: str | None = None,
        escalation_id: str | None = None,
    ) -> None:
        """Update action lifecycle status and outcome references."""
        row = await self.get_record(session, request_id)
        if row is not None:
            row.status = status.value
            if bank_operation_id is not None:
                row.bank_operation_id = bank_operation_id
            if audit_seq is not None:
                row.audit_seq = audit_seq
            if audit_hash is not None:
                row.audit_hash = audit_hash
            if escalation_id is not None:
                row.escalation_id = escalation_id
            await session.flush()
