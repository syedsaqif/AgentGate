"""Member B persistent Ed25519 agent registry."""

from __future__ import annotations

import json
import secrets
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from contracts.audit_models import AuditEvent
from contracts.audit_ports import AuditLog
from contracts.common import ActionType, Clock, OpaqueId, Page, Tx
from contracts.identity_models import AgentPublicIdentity, AgentStatus

from .crypto import (
    InvalidJWKError,
    b64url_decode,
    b64url_encode,
    canonical_json,
    format_utc_timestamp,
    jwk_thumbprint,
    validate_ed25519_public_jwk,
)
from .errors import (
    DuplicateAgentKeyError,
    ResourceNotFoundError,
    ValidationDomainError,
)
from .models import AgentRecord, SessionFactory, SqlAlchemyTx, maybe_await, session_scope

_VALID_SCOPES: frozenset[str] = frozenset(item.value for item in ActionType)
_DEFAULT_PAGE_LIMIT = 50
_MAX_PAGE_LIMIT = 100


def _validate_label(value: str, field_name: str, max_length: int = 120) -> str:
    if not isinstance(value, str):
        raise ValidationDomainError(f"{field_name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > max_length:
        raise ValidationDomainError(
            f"{field_name} must be between 1 and {max_length} characters"
        )
    return cleaned


def _validate_scopes(max_scopes: Sequence[str]) -> tuple[str, ...]:
    if not max_scopes:
        raise ValidationDomainError("max_scopes must contain at least one valid action scope")
    seen: list[str] = []
    for scope in max_scopes:
        if not isinstance(scope, str) or scope not in _VALID_SCOPES:
            raise ValidationDomainError(f"unsupported agent scope: {scope!r}")
        if scope not in seen:
            seen.append(scope)
    return tuple(seen)


def _record_to_dto(record: AgentRecord) -> AgentPublicIdentity:
    public_jwk = json.loads(record.public_jwk_json)
    max_scopes = tuple(json.loads(record.max_scopes_json))
    return AgentPublicIdentity(
        agent_id=record.agent_id,
        name=record.name,
        vendor=record.vendor,
        public_jwk=public_jwk,
        jkt=record.jkt,
        max_scopes=max_scopes,
        status=AgentStatus(record.status),
    )


def _encode_cursor(agent_id: str) -> str:
    return b64url_encode(agent_id.encode("utf-8"))


def _decode_cursor(cursor: str) -> str:
    try:
        raw = b64url_decode(cursor)
        value = raw.decode("utf-8")
    except Exception as exc:
        raise ValidationDomainError("invalid pagination cursor") from exc
    if not value:
        raise ValidationDomainError("invalid pagination cursor")
    return value


class SqlAgentRegistry:
    """SQLAlchemy-backed Ed25519 agent registry conforming to `AgentRegistry`."""

    def __init__(
        self,
        session_factory: SessionFactory,
        clock: Clock,
        audit_log: AuditLog | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._audit_log = audit_log

    async def register(
        self,
        *,
        name: str,
        vendor: str,
        public_jwk: Mapping[str, Any],
        max_scopes: Sequence[str],
        agent_id: str | None = None,
        request_id: str | None = None,
        tx: Tx | None = None,
    ) -> AgentPublicIdentity:
        """Validate JWK and scopes, persist unique agent identity, and audit registration."""

        clean_name = _validate_label(name, "name", max_length=120)
        clean_vendor = _validate_label(vendor, "vendor", max_length=120)
        clean_scopes = _validate_scopes(max_scopes)

        try:
            normalized_jwk = validate_ed25519_public_jwk(public_jwk)
            thumbprint = jwk_thumbprint(normalized_jwk)
        except InvalidJWKError as exc:
            raise ValidationDomainError(str(exc), internal_reason="invalid_public_jwk") from exc

        if agent_id is not None:
            resolved_agent_id = _validate_label(agent_id, "agent_id", max_length=64)
            if any(ch.isspace() for ch in resolved_agent_id):
                raise ValidationDomainError("agent_id must not contain whitespace")
        else:
            resolved_agent_id = f"agt_{secrets.token_hex(8)}"

        now = self._clock.now()
        now_iso = format_utc_timestamp(now)

        record = AgentRecord(
            agent_id=resolved_agent_id,
            name=clean_name,
            vendor=clean_vendor,
            public_jwk_json=canonical_json(normalized_jwk).decode("utf-8"),
            jkt=thumbprint,
            max_scopes_json=json.dumps(list(clean_scopes)),
            status=AgentStatus.ACTIVE.value,
            created_at=now_iso,
            updated_at=now_iso,
        )

        async with session_scope(self._session_factory, tx, write=True) as session:
            session.add(record)
            try:
                await maybe_await(session.flush())
            except IntegrityError as exc:
                raise DuplicateAgentKeyError(
                    "An agent with this public key thumbprint or ID is already registered."
                ) from exc

            identity = _record_to_dto(record)
            if self._audit_log is not None:
                active_tx: Tx = (
                    tx
                    if tx is not None
                    else SqlAlchemyTx(id=f"tx_{secrets.token_hex(6)}", session=session)
                )
                await self._audit_log.append(
                    AuditEvent(
                        event_type="agent.registered",
                        request_id=request_id,
                        agent_id=identity.agent_id,
                        safe_payload={
                            "name": identity.name,
                            "vendor": identity.vendor,
                            "jkt": identity.jkt,
                            "max_scopes": list(identity.max_scopes),
                            "status": str(identity.status),
                        },
                        occurred_at=now,
                    ),
                    tx=active_tx,
                )
            return identity

    async def get(self, agent_id: OpaqueId, tx: Tx | None = None) -> AgentPublicIdentity:
        """Fetch a registered agent by ID or raise 404 ResourceNotFoundError."""

        async with session_scope(self._session_factory, tx, write=False) as session:
            record = await maybe_await(session.get(AgentRecord, agent_id))
            if record is None:
                raise ResourceNotFoundError(
                    "Agent not found.", internal_reason="agent_not_found"
                )
            return _record_to_dto(record)

    async def get_by_jkt(self, jkt: str, tx: Tx | None = None) -> AgentPublicIdentity | None:
        """Look up a registered agent by RFC 7638 JWK thumbprint."""

        async with session_scope(self._session_factory, tx, write=False) as session:
            stmt = select(AgentRecord).where(AgentRecord.jkt == jkt)
            result = await maybe_await(session.execute(stmt))
            record = result.scalar_one_or_none()
            return _record_to_dto(record) if record is not None else None

    async def set_status(self, agent_id: OpaqueId, status: AgentStatus, tx: Tx) -> None:
        """Update agent status inside the caller-owned transaction without committing `tx`."""

        await self.set_status_returning_previous(agent_id, status, tx)

    async def set_status_returning_previous(
        self, agent_id: OpaqueId, status: AgentStatus, tx: Tx
    ) -> AgentStatus:
        """Read and update under the same SQLite writer transaction."""
        resolved_status = AgentStatus(status)
        now_iso = format_utc_timestamp(self._clock.now())
        async with session_scope(self._session_factory, tx, write=True) as session:
            record = await maybe_await(session.get(AgentRecord, agent_id))
            if record is None:
                raise ResourceNotFoundError(
                    "Agent not found.", internal_reason="agent_not_found"
                )
            previous = AgentStatus(record.status)
            record.status = resolved_status.value
            record.updated_at = now_iso
            await maybe_await(session.flush())

            return previous

    async def list_public(self, tx: Tx | None = None) -> tuple[AgentPublicIdentity, ...]:
        """Return all registered agents ordered stably by `agent_id`."""

        async with session_scope(self._session_factory, tx, write=False) as session:
            stmt = select(AgentRecord).order_by(AgentRecord.agent_id.asc())
            result = await maybe_await(session.execute(stmt))
            records = result.scalars().all()
            return tuple(_record_to_dto(rec) for rec in records)

    async def list_public_page(
        self,
        *,
        cursor: str | None = None,
        limit: int = _DEFAULT_PAGE_LIMIT,
        tx: Tx | None = None,
    ) -> Page:
        """Return a bounded cursor-paginated `Page` of `AgentPublicIdentity` items."""

        if limit < 1 or limit > _MAX_PAGE_LIMIT:
            raise ValidationDomainError(f"limit must be between 1 and {_MAX_PAGE_LIMIT}")

        after_id = _decode_cursor(cursor) if cursor is not None else None
        async with session_scope(self._session_factory, tx, write=False) as session:
            stmt = select(AgentRecord).order_by(AgentRecord.agent_id.asc())
            if after_id is not None:
                stmt = stmt.where(AgentRecord.agent_id > after_id)
            stmt = stmt.limit(limit + 1)
            result = await maybe_await(session.execute(stmt))
            records = list(result.scalars().all())

        has_more = len(records) > limit
        page_records = records[:limit]
        items = tuple(_record_to_dto(rec) for rec in page_records)
        next_cursor = _encode_cursor(page_records[-1].agent_id) if has_more else None
        return Page(items=items, next_cursor=next_cursor)


__all__ = ["SqlAgentRegistry"]
