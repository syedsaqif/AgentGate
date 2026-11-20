"""Member A mandate domain service, validation, canonical hashing, and signing."""

from __future__ import annotations

import base64
import inspect
import json
import uuid
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.audit_models import AuditEvent
from contracts.audit_ports import AuditLog
from contracts.canonical import canonical_json, sha256_prefixed
from contracts.common import Clock, Tx, UnitOfWork
from contracts.core_models import (
    MandateSnapshot,
    MandateStatus,
    MandateTerms,
    PolicyResult,
    UsageSnapshot,
)
from contracts.core_ports import MandateReader, UsageReader
from contracts.identity_models import AgentStatus
from contracts.identity_ports import AgentRegistry, HumanAuth
from contracts.reason_codes import ReasonCode
from gateway.errors import AgentGateError
from gateway.mandates import repository
from gateway.mandates.models import (
    Catalog,
    CreateMandateRequest,
    CreateMandateResponse,
    CustomerMandateItem,
)
from gateway.mandates.policy import PolicyEvaluator
from gateway.mandates.schema import MandateTable, MandateUsageTable


def get_action_str(action: Any) -> str:
    """Safely extract string value from ActionType or string."""
    return str(action.value if hasattr(action, "value") else action)


class MandateUsageReader(UsageReader):
    """Adapter exposing the UsageReader protocol get(...) method for MandateService."""

    def __init__(self, service: MandateService) -> None:
        self._service = service

    async def get(
        self, mandate_id: str, tx: Tx | None = None
    ) -> tuple[UsageSnapshot, ...]:
        return await self._service.get_usage(mandate_id, tx)


class MandateService(MandateReader, UsageReader):
    """Member A service coordinating mandate lifecycle, storage, and validation."""

    def __init__(
        self,
        human_auth: HumanAuth,
        agent_registry: AgentRegistry,
        audit_log: AuditLog,
        catalog: Catalog,
        clock: Clock,
        unit_of_work: UnitOfWork,
        session_factory: Any,
        signing_key: Ed25519PrivateKey | None = None,
        key_id: str = "key_mandate_issuer_1",
    ) -> None:
        self.human_auth = human_auth
        self.agent_registry = agent_registry
        self.audit_log = audit_log
        self.catalog = catalog
        self.clock = clock
        self.unit_of_work = unit_of_work
        self.session_factory = session_factory
        self.signing_key = signing_key or Ed25519PrivateKey.generate()
        self.key_id = key_id
        self.policy_evaluator = PolicyEvaluator()

    def evaluate_policy(self, *args: Any, **kwargs: Any) -> PolicyResult:
        """Pure synchronous policy evaluation conforming to PolicyEvaluator protocol."""
        return self.policy_evaluator.evaluate(*args, **kwargs)

    async def create_mandate(
        self, customer_id: str, request: CreateMandateRequest
    ) -> CreateMandateResponse:
        """Create a new immutable signed mandate after strict verification."""
        # 1. Verify customer PIN (never logged or audited)
        try:
            await self.human_auth.verify_pin(customer_id, request.pin)
        except Exception:
            raise AgentGateError(
                code=ReasonCode.E_ESC_BAD_PIN,
                message="Incorrect customer PIN",
                status_code=403,
            ) from None

        # 2. Verify agent registry status and capability ceiling
        agent = await self.agent_registry.get(request.agent_id)
        if not agent or agent.status != AgentStatus.ACTIVE:
            raise AgentGateError(
                code=ReasonCode.E_AUTH_AGENT_SUSPENDED,
                message=f"Agent '{request.agent_id}' is not active",
                status_code=403,
            )

        allowed_scopes = set(agent.max_scopes)
        for grant in request.grants:
            act = get_action_str(grant.action)
            if act not in allowed_scopes:
                msg = f"Agent '{request.agent_id}' ceiling does not permit action '{grant.action}'"
                raise AgentGateError(
                    code=ReasonCode.E_REQ_INVALID,
                    message=msg,
                    status_code=400,
                )

        # 3. Verify account ownership and payee catalog existence
        for grant in request.grants:
            for acc_id in grant.account_ids:
                acc_res = self.catalog.get_account(acc_id)
                acc = await acc_res if inspect.isawaitable(acc_res) else acc_res
                if not acc or acc.customer_id != customer_id:
                    raise AgentGateError(
                        code=ReasonCode.E_REQ_INVALID,
                        message=f"Account '{acc_id}' does not belong to customer '{customer_id}'",
                        status_code=400,
                    )
            for payee_id in grant.payee_ids:
                payee_res = self.catalog.get_payee(payee_id)
                payee = await payee_res if inspect.isawaitable(payee_res) else payee_res
                if not payee:
                    raise AgentGateError(
                        code=ReasonCode.E_REQ_INVALID,
                        message=f"Payee '{payee_id}' does not exist in trusted catalog",
                        status_code=400,
                    )

        # 4. Validate constraints and boundaries
        if request.valid_until <= request.valid_from:
            raise AgentGateError(
                code=ReasonCode.E_REQ_INVALID,
                message="valid_until must be strictly after valid_from",
                status_code=400,
            )

        for grant in request.grants:
            if grant.window != "mandate":
                msg = (
                    f"Only window='mandate' is supported in this build baseline, "
                    f"got '{grant.window}'"
                )
                raise AgentGateError(
                    code=ReasonCode.E_REQ_INVALID,
                    message=msg,
                    status_code=400,
                )

            act = get_action_str(grant.action)
            if act in ("bill_pay", "transfer"):
                if grant.max_per_txn_paise is None or grant.max_total_paise is None:
                    msg = f"Action '{grant.action}' requires max_per_txn_paise and max_total_paise"
                    raise AgentGateError(
                        code=ReasonCode.E_REQ_INVALID,
                        message=msg,
                        status_code=400,
                    )
                if grant.max_per_txn_paise > grant.max_total_paise:
                    msg = (
                        f"max_per_txn_paise ({grant.max_per_txn_paise}) cannot exceed "
                        f"max_total_paise ({grant.max_total_paise})"
                    )
                    raise AgentGateError(
                        code=ReasonCode.E_REQ_INVALID,
                        message=msg,
                        status_code=400,
                    )
                if grant.max_count is None or grant.max_count < 1:
                    raise AgentGateError(
                        code=ReasonCode.E_REQ_INVALID,
                        message="max_count must be at least 1",
                        status_code=400,
                    )
                if grant.currency != "INR":
                    raise AgentGateError(
                        code=ReasonCode.E_REQ_INVALID,
                        message=f"Currency must be INR, got '{grant.currency}'",
                        status_code=400,
                    )

            if grant.time_window:
                if grant.time_window.timezone != "Asia/Kolkata":
                    msg = (
                        f"TimeWindow timezone must be Asia/Kolkata, "
                        f"got '{grant.time_window.timezone}'"
                    )
                    raise AgentGateError(
                        code=ReasonCode.E_REQ_INVALID,
                        message=msg,
                        status_code=400,
                    )
                if grant.time_window.start_local >= grant.time_window.end_local:
                    msg = (
                        f"Overnight or invalid time windows (start "
                        f"'{grant.time_window.start_local}' >= end "
                        f"'{grant.time_window.end_local}') are not supported"
                    )
                    raise AgentGateError(
                        code=ReasonCode.E_REQ_INVALID,
                        message=msg,
                        status_code=400,
                    )

        # 5. Assemble server-controlled terms
        mandate_id = f"mnd_{uuid.uuid4().hex[:12]}"
        now = self.clock.now()
        terms = MandateTerms(
            mandate_id=mandate_id,
            version=1,
            customer_id=customer_id,
            agent_id=request.agent_id,
            valid_from=request.valid_from,
            valid_until=request.valid_until,
            grants=request.grants,
            step_up=request.step_up,
            risk_profile=request.risk_profile,
        )

        # 6. Canonical Hash & Ed25519 signature
        terms_dict = terms.model_dump(mode="json", exclude_none=True)
        terms_bytes = canonical_json(terms_dict)
        mandate_hash = sha256_prefixed(terms_bytes)

        sig_raw = self.signing_key.sign(mandate_hash.encode("ascii"))
        sig = base64.urlsafe_b64encode(sig_raw).rstrip(b"=").decode("ascii")

        snapshot = MandateSnapshot(
            terms=terms,
            status=MandateStatus.ACTIVE,
            mandate_hash=mandate_hash,
            sig=sig,
            key_id=self.key_id,
        )

        # 7. Atomic persistence & write-ahead audit event in ONE transaction
        async with self.unit_of_work as tx:
            session: AsyncSession = tx.session
            mandate_row = MandateTable(
                mandate_id=mandate_id,
                version=1,
                customer_id=customer_id,
                agent_id=request.agent_id,
                status=MandateStatus.ACTIVE.value,
                terms_json=json.dumps(terms_dict),
                mandate_hash=mandate_hash,
                sig=sig,
                key_id=self.key_id,
                created_at=now,
                updated_at=now,
            )
            await repository.insert_mandate(session, mandate_row)

            # Initialize usage tracking rows to zero for each grant
            for grant in terms.grants:
                usage_row = MandateUsageTable(
                    mandate_id=mandate_id,
                    grant_id=grant.grant_id,
                    window_key=f"{mandate_id}:{grant.grant_id}:{grant.window}",
                    committed_paise=0,
                    reserved_paise=0,
                    committed_count=0,
                    reserved_count=0,
                    updated_at=now,
                )
                await repository.insert_usage_row(session, usage_row)

            # Append audit event (never includes PIN or tokens)
            audit_event = AuditEvent(
                event_type="mandate.created",
                customer_id=customer_id,
                agent_id=request.agent_id,
                mandate_id=mandate_id,
                mandate_hash=mandate_hash,
                safe_payload={
                    "mandate_id": mandate_id,
                    "agent_id": request.agent_id,
                    "customer_id": customer_id,
                    "valid_from": request.valid_from.isoformat(),
                    "valid_until": request.valid_until.isoformat(),
                },
                occurred_at=now,
            )
            audit_ref = await self.audit_log.append(audit_event, tx=tx)

        return CreateMandateResponse(mandate=snapshot, audit_ref=audit_ref)

    async def get(
        self, mandate_id: str, tx: Tx | None = None
    ) -> MandateSnapshot:
        """Fetch a mandate snapshot by ID."""
        if tx is not None and hasattr(tx, "session"):
            row = await repository.get_mandate(tx.session, mandate_id)
        else:
            async with self.session_factory() as session:
                row = await repository.get_mandate(session, mandate_id)

        if not row:
            raise AgentGateError(
                code=ReasonCode.E_REQ_INVALID,
                message=f"Mandate '{mandate_id}' not found",
                status_code=404,
            )

        terms_data = json.loads(row.terms_json)
        terms = MandateTerms.model_validate(terms_data)
        return MandateSnapshot(
            terms=terms,
            status=MandateStatus(row.status),
            mandate_hash=row.mandate_hash,
            sig=row.sig,
            key_id=row.key_id,
        )

    async def get_usage(
        self, mandate_id: str, tx: Tx | None = None
    ) -> tuple[UsageSnapshot, ...]:
        """Fetch cumulative and remaining usage for all grants in a mandate."""
        snapshot = await self.get(mandate_id, tx)
        grants_by_id = {g.grant_id: g for g in snapshot.terms.grants}

        if tx is not None and hasattr(tx, "session"):
            rows = await repository.get_usage_rows(tx.session, mandate_id)
        else:
            async with self.session_factory() as session:
                rows = await repository.get_usage_rows(session, mandate_id)

        usage_list: list[UsageSnapshot] = []
        for r in rows:
            grant = grants_by_id.get(r.grant_id)
            remaining_paise = None
            remaining_count = None
            if grant:
                if grant.max_total_paise is not None:
                    spent = r.committed_paise + r.reserved_paise
                    remaining_paise = max(0, grant.max_total_paise - spent)
                if grant.max_count is not None:
                    counted = r.committed_count + r.reserved_count
                    remaining_count = max(0, grant.max_count - counted)

            usage_list.append(
                UsageSnapshot(
                    mandate_id=r.mandate_id,
                    grant_id=r.grant_id,
                    window_key=r.window_key,
                    committed_paise=r.committed_paise,
                    reserved_paise=r.reserved_paise,
                    committed_count=r.committed_count,
                    reserved_count=r.reserved_count,
                    remaining_paise=remaining_paise,
                    remaining_count=remaining_count,
                )
            )

        return tuple(usage_list)

    # Alias for UsageReader protocol
    async def get_usage_for_mandate(
        self, mandate_id: str, tx: Tx | None = None
    ) -> tuple[UsageSnapshot, ...]:
        return await self.get_usage(mandate_id, tx)

    async def list_customer_mandates(
        self,
        customer_id: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[tuple[CustomerMandateItem, ...], str | None]:
        """List all mandates and usage for an authenticated customer."""
        async with self.session_factory() as session:
            rows, next_cursor = await repository.list_mandates_by_customer(
                session, customer_id=customer_id, limit=limit, cursor=cursor
            )

        items: list[CustomerMandateItem] = []
        for row in rows:
            terms = MandateTerms.model_validate(json.loads(row.terms_json))
            snapshot = MandateSnapshot(
                terms=terms,
                status=MandateStatus(row.status),
                mandate_hash=row.mandate_hash,
                sig=row.sig,
                key_id=row.key_id,
            )
            usage = await self.get_usage(row.mandate_id)
            items.append(
                CustomerMandateItem(
                    terms=snapshot.terms,
                    status=snapshot.status,
                    mandate_hash=snapshot.mandate_hash,
                    sig=snapshot.sig,
                    key_id=snapshot.key_id,
                    usage=usage,
                    mandate=snapshot,
                )
            )

        return tuple(items), next_cursor

    async def set_status(
        self, mandate_id: str, status: MandateStatus, tx: Tx
    ) -> None:
        """Update mandate status in lifecycle transactions (e.g. revoke/suspend)."""
        session: AsyncSession = tx.session
        await repository.update_mandate_status(session, mandate_id, status.value)
