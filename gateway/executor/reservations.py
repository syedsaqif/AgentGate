"""Member A atomic multi-grant reservation manager."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.common import OpaqueId, Tx
from contracts.core_models import (
    CanonicalAction,
    Reservation,
    ReservationState,
)
from contracts.core_ports import MandateReader, ReservationPort
from contracts.reason_codes import ReasonCode
from gateway.errors import AgentGateError
from gateway.executor.schema import ReservationTable
from gateway.mandates.schema import MandateUsageTable


def _get_action_str(action: Any) -> str:
    if hasattr(action, "value"):
        return str(action.value)
    return str(action)


class ReservationManager(ReservationPort):
    """Atomic multi-grant reservation implementation adhering to Section 7.2."""

    def __init__(self, mandate_reader: MandateReader) -> None:
        self.mandate_reader = mandate_reader

    async def reserve(self, action: CanonicalAction, tx: Tx) -> Reservation:
        """Atomically reserve budgets across all matching grants in sorted order."""
        session: AsyncSession = tx.session

        # 1. Fetch mandate snapshot to find matching grants
        snapshot = await self.mandate_reader.get(action.mandate_id, tx=tx)
        action_type_str = _get_action_str(action.action_type)

        matching_grants = [
            g
            for g in snapshot.terms.grants
            if _get_action_str(g.action) == action_type_str
        ]
        if not matching_grants:
            raise AgentGateError(
                code=ReasonCode.E_REQ_INVALID,
                message=f"No matching grants found for action '{action.action_type}'",
                status_code=400,
            )

        # Enforce deterministic sorted lock order by grant_id
        sorted_grants = sorted(matching_grants, key=lambda g: g.grant_id)

        amount_paise = action.params.amount_paise
        count_delta = 1

        usage_rows: list[MandateUsageTable] = []

        # 2. Check and lock all matching usage rows
        for grant in sorted_grants:
            wkey = f"{action.mandate_id}:{grant.grant_id}:mandate"
            stmt = select(MandateUsageTable).where(
                MandateUsageTable.mandate_id == action.mandate_id,
                MandateUsageTable.grant_id == grant.grant_id,
                MandateUsageTable.window_key == wkey,
            )
            result = await session.execute(stmt)
            u_row = result.scalar_one_or_none()

            if u_row is None:
                # Initialize usage row if missing
                u_row = MandateUsageTable(
                    mandate_id=action.mandate_id,
                    grant_id=grant.grant_id,
                    window_key=wkey,
                    committed_paise=0,
                    reserved_paise=0,
                    committed_count=0,
                    reserved_count=0,
                )
                session.add(u_row)
                await session.flush()

            # Enforce caps
            if grant.max_total_paise is not None:
                total_projected = u_row.committed_paise + u_row.reserved_paise + amount_paise
                if total_projected > grant.max_total_paise:
                    raise AgentGateError(
                        code=ReasonCode.E_REQ_INVALID,
                        message=(
                            f"Grant '{grant.grant_id}' budget cap exceeded: "
                            f"{total_projected} > {grant.max_total_paise}"
                        ),
                        status_code=422,
                    )

            if grant.max_count is not None:
                count_projected = u_row.committed_count + u_row.reserved_count + count_delta
                if count_projected > grant.max_count:
                    raise AgentGateError(
                        code=ReasonCode.E_REQ_INVALID,
                        message=(
                            f"Grant '{grant.grant_id}' count cap exceeded: "
                            f"{count_projected} > {grant.max_count}"
                        ),
                        status_code=422,
                    )

            usage_rows.append(u_row)

        # 3. Apply reservations across all matching grants
        window_keys: list[str] = []
        for u_row in usage_rows:
            u_row.reserved_paise += amount_paise
            u_row.reserved_count += count_delta
            window_keys.append(u_row.window_key)

        res_id = f"res_{uuid.uuid4().hex[:12]}"
        res_row = ReservationTable(
            reservation_id=res_id,
            request_id=action.request_id,
            mandate_id=action.mandate_id,
            grant_window_keys_json=json.dumps(window_keys),
            amount_paise=amount_paise,
            count_delta=count_delta,
            state=ReservationState.RESERVED.value,
        )
        session.add(res_row)
        await session.flush()

        return Reservation(
            reservation_id=res_id,
            request_id=action.request_id,
            grant_window_keys=tuple(window_keys),
            amount_paise=amount_paise,
            count=count_delta,
            state=ReservationState.RESERVED,
        )

    async def commit(self, reservation_id: OpaqueId, tx: Tx) -> None:
        """Idempotently commit reservation: move reserved -> committed counters."""
        session: AsyncSession = tx.session
        stmt = select(ReservationTable).where(
            ReservationTable.reservation_id == reservation_id
        )
        result = await session.execute(stmt)
        res_row = result.scalar_one_or_none()

        if res_row is None or res_row.state == ReservationState.COMMITTED.value:
            return  # Idempotent no-op

        if res_row.state in (ReservationState.RESERVED.value, ReservationState.UNKNOWN.value):
            wkeys: list[str] = json.loads(res_row.grant_window_keys_json)
            for wkey in wkeys:
                u_stmt = select(MandateUsageTable).where(MandateUsageTable.window_key == wkey)
                u_res = await session.execute(u_stmt)
                u_row = u_res.scalar_one_or_none()
                if u_row is not None:
                    u_row.reserved_paise = max(0, u_row.reserved_paise - res_row.amount_paise)
                    u_row.committed_paise += res_row.amount_paise
                    u_row.reserved_count = max(0, u_row.reserved_count - res_row.count_delta)
                    u_row.committed_count += res_row.count_delta

            res_row.state = ReservationState.COMMITTED.value
            await session.flush()

    async def release(self, reservation_id: OpaqueId, tx: Tx) -> None:
        """Idempotently release reservation: decrement reserved counters."""
        session: AsyncSession = tx.session
        stmt = select(ReservationTable).where(
            ReservationTable.reservation_id == reservation_id
        )
        result = await session.execute(stmt)
        res_row = result.scalar_one_or_none()

        if res_row is None or res_row.state == ReservationState.RELEASED.value:
            return  # Idempotent no-op

        if res_row.state in (ReservationState.RESERVED.value, ReservationState.UNKNOWN.value):
            wkeys: list[str] = json.loads(res_row.grant_window_keys_json)
            for wkey in wkeys:
                u_stmt = select(MandateUsageTable).where(MandateUsageTable.window_key == wkey)
                u_res = await session.execute(u_stmt)
                u_row = u_res.scalar_one_or_none()
                if u_row is not None:
                    u_row.reserved_paise = max(0, u_row.reserved_paise - res_row.amount_paise)
                    u_row.reserved_count = max(0, u_row.reserved_count - res_row.count_delta)

            res_row.state = ReservationState.RELEASED.value
            await session.flush()

    async def mark_unknown(self, reservation_id: OpaqueId, tx: Tx) -> None:
        """Mark reservation as UNKNOWN, keeping budget reserved for recovery."""
        session: AsyncSession = tx.session
        stmt = select(ReservationTable).where(
            ReservationTable.reservation_id == reservation_id
        )
        result = await session.execute(stmt)
        res_row = result.scalar_one_or_none()
        if res_row is not None and res_row.state == ReservationState.RESERVED.value:
            res_row.state = ReservationState.UNKNOWN.value
            await session.flush()

    async def get_by_request_id(
        self, session: AsyncSession, request_id: str
    ) -> ReservationTable | None:
        """Find reservation associated with a given request_id."""
        stmt = select(ReservationTable).where(ReservationTable.request_id == request_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()
