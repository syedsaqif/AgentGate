"""Member C explicit risk/execution substitutes and test doubles."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from contracts.audit_models import AuditEvent, EvidenceBundle, VerifyReport
from contracts.common import AuditRef, HumanRole, OpaqueId, Tx
from contracts.core_models import (
    AccountCard,
    ActionStatus,
    BankResult,
    BankStatus,
    CanonicalAction,
    ExecutionResult,
    MandateSnapshot,
    PayeeCard,
    UsageSnapshot,
)
from contracts.escalation_models import Escalation, EscalationDecision, EscalationState
from contracts.identity_models import HumanContext
from contracts.reason_codes import ReasonCode
from contracts.risk_models import RiskInput, RiskResult


@dataclass
class DummyTx:
    id: str = "tx_dummy"


@dataclass
class FixtureRiskEngine:
    result: RiskResult | None = None
    scores_called: list[RiskInput] = field(default_factory=list)
    recorded_attempts: list[tuple[RiskInput, str]] = field(default_factory=list)
    recorded_outcomes: list[tuple[object, str]] = field(default_factory=list)

    def score(self, risk_input: RiskInput) -> RiskResult:
        self.scores_called.append(risk_input)
        if self.result is None:
            raise ValueError("FixtureRiskEngine result not configured")
        return self.result

    async def record_attempt(self, risk_input: RiskInput, tx: Tx) -> None:
        self.recorded_attempts.append((risk_input, tx.id))

    async def record_outcome(self, outcome: object, tx: Tx) -> None:
        self.recorded_outcomes.append((outcome, tx.id))


@dataclass
class RecordingExecutionPort:
    request_ids: list[str] = field(default_factory=list)
    escalation_ids: list[str | None] = field(default_factory=list)
    outcome_status: ActionStatus = ActionStatus.EXECUTED
    bank_status: BankStatus = BankStatus.EXECUTED

    async def execute_stored(
        self, request_id: str, escalation_id: str | None = None
    ) -> ExecutionResult:
        self.request_ids.append(request_id)
        self.escalation_ids.append(escalation_id)
        return ExecutionResult(
            request_id=request_id,
            status=self.outcome_status,
            bank_result=BankResult(
                status=self.bank_status,
                bank_operation_id=f"bank_op_{request_id}",
                bank_idempotency_key=f"idem_{request_id}",
                executed_at=datetime.now(UTC),
            )
            if self.outcome_status == ActionStatus.EXECUTED
            else None,
            audit_ref=AuditRef(seq=1, entry_hash="0" * 64),
            escalation_id=escalation_id,
        )


@dataclass
class FixtureHumanAuth:
    valid_sessions: dict[str, HumanContext] = field(default_factory=dict)
    valid_pins: dict[str, str] = field(default_factory=lambda: {"cust_001": "4821"})
    pin_checks: list[tuple[str, str, bool]] = field(default_factory=list)

    async def authenticate(self, session_token: str, required_role: HumanRole) -> HumanContext:
        if session_token not in self.valid_sessions:
            raise ValueError(ReasonCode.E_AUTH_INVALID_TOKEN)
        ctx = self.valid_sessions[session_token]
        if ctx.role != required_role:
            raise ValueError(ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH)
        return ctx

    async def verify_pin(self, customer_id: OpaqueId, pin: str) -> None:
        expected = self.valid_pins.get(customer_id)
        success = expected is not None and expected == pin
        self.pin_checks.append((customer_id, pin, success))
        if not success:
            raise ValueError(ReasonCode.E_ESC_BAD_PIN)


@dataclass
class MemoryAuditLog:
    events: list[AuditEvent] = field(default_factory=list)
    fail_on_append: bool = False

    async def append(self, event: AuditEvent, tx: Tx | None = None) -> AuditRef:
        if self.fail_on_append:
            raise ValueError(ReasonCode.E_AUDIT_WRITE_FAILED)
        self.events.append(event)
        seq = len(self.events)
        return AuditRef(seq=seq, entry_hash=f"{seq:064x}")

    async def verify(self, anchor: AuditRef | None = None) -> VerifyReport:
        return VerifyReport(
            valid=True,
            checked_count=len(self.events),
            first_mismatch_seq=None,
            reason="All entries valid in memory log",
        )

    async def checkpoint(self) -> AuditRef:
        seq = max(len(self.events), 1)
        return AuditRef(seq=seq, entry_hash=f"{seq:064x}")

    async def evidence(self, request_id: OpaqueId) -> EvidenceBundle:
        return EvidenceBundle(
            request_id=request_id,
            timestamp=datetime.now(UTC),
            events=tuple(self.events),
            checkpoints=(),
            signer_key_id="key_test",
        )


@dataclass
class FixtureActionReader:
    actions: dict[str, CanonicalAction] = field(default_factory=dict)

    async def get(self, request_id: OpaqueId, tx: Tx | None = None) -> CanonicalAction:
        if request_id not in self.actions:
            raise KeyError(f"Action {request_id} not found")
        return self.actions[request_id]


@dataclass
class FixtureMandateReader:
    mandates: dict[str, MandateSnapshot] = field(default_factory=dict)

    async def get(self, mandate_id: OpaqueId, tx: Tx | None = None) -> MandateSnapshot:
        if mandate_id not in self.mandates:
            raise KeyError(f"Mandate {mandate_id} not found")
        return self.mandates[mandate_id]


@dataclass
class FixtureUsageReader:
    usages: dict[str, tuple[UsageSnapshot, ...]] = field(default_factory=dict)

    async def get(
        self, mandate_id: OpaqueId, tx: Tx | None = None
    ) -> tuple[UsageSnapshot, ...]:
        return self.usages.get(mandate_id, ())


@dataclass
class FixtureCatalog:
    payees: dict[str, PayeeCard] = field(default_factory=dict)
    accounts: dict[str, AccountCard] = field(default_factory=dict)

    def get_payee(self, payee_id: str) -> PayeeCard | None:
        return self.payees.get(payee_id)

    def get_account(self, account_id: str) -> AccountCard | None:
        return self.accounts.get(account_id)


@dataclass
class FixtureLifecycle:
    revoked_mandates: list[str] = field(default_factory=list)
    suspended_agents: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    suspended_delegations: list[tuple[str, str, tuple[str, ...]]] = field(default_factory=list)
    reinstated_agents: list[str] = field(default_factory=list)

    async def revoke_mandate(self, mandate_id: OpaqueId) -> None:
        self.revoked_mandates.append(mandate_id)

    async def suspend_agent(self, agent_id: OpaqueId, reason_codes: tuple[str, ...]) -> None:
        self.suspended_agents.append((agent_id, reason_codes))

    async def suspend_customer_delegation(
        self, customer_id: OpaqueId, agent_id: OpaqueId, reason_codes: tuple[str, ...]
    ) -> None:
        self.suspended_delegations.append((customer_id, agent_id, reason_codes))

    async def reinstate_agent(self, agent_id: OpaqueId) -> None:
        self.reinstated_agents.append(agent_id)


@dataclass
class MemoryEscalationStore:
    escalations: dict[str, Escalation] = field(default_factory=dict)
    claims: list[tuple[str, str]] = field(default_factory=list)

    async def create(
        self, action: CanonicalAction, assessment: RiskResult, tx: Tx
    ) -> Escalation:
        esc = Escalation(
            escalation_id=f"esc_{action.request_id}",
            request_id=action.request_id,
            customer_id=action.customer_id,
            agent_id=action.agent_id,
            mandate_id=action.mandate_id,
            action=action,
            action_hash="0" * 64,
            trusted_summary={},
            reason_codes=tuple(s.reason_code for s in assessment.signals),
            expires_at=datetime.now(UTC),
            state=EscalationState.PENDING,
        )
        self.escalations[esc.escalation_id] = esc
        return esc

    async def get_owned(self, escalation_id: OpaqueId, subject_id: OpaqueId) -> Escalation:
        esc = self.escalations.get(escalation_id)
        if esc is None:
            raise KeyError(f"Escalation {escalation_id} not found")
        if esc.customer_id != subject_id and esc.agent_id != subject_id:
            raise PermissionError("Access denied")
        return esc

    async def decide(
        self,
        escalation_id: OpaqueId,
        decision: EscalationDecision,
        subject_id: OpaqueId,
        tx: Tx,
    ) -> Escalation:
        esc = await self.get_owned(escalation_id, subject_id)
        new_state = (
            EscalationState.APPROVED
            if decision == EscalationDecision.APPROVE
            else EscalationState.REJECTED
        )
        updated = esc.model_copy(update={"state": new_state, "decided_by": subject_id})
        self.escalations[escalation_id] = updated
        return updated

    async def claim_execution(
        self, escalation_id: OpaqueId, expected_hash: str, tx: Tx
    ) -> Escalation:
        self.claims.append((escalation_id, expected_hash))
        esc = self.escalations[escalation_id]
        updated = esc.model_copy(update={"state": EscalationState.EXECUTED})
        self.escalations[escalation_id] = updated
        return updated

    async def cancel_for_mandate(self, mandate_id: OpaqueId, tx: Tx) -> None:
        for k, v in list(self.escalations.items()):
            if v.mandate_id == mandate_id and v.state == EscalationState.PENDING:
                self.escalations[k] = v.model_copy(update={"state": EscalationState.CANCELLED})

    async def cancel_for_agent(self, agent_id: OpaqueId, tx: Tx) -> None:
        for k, v in list(self.escalations.items()):
            if v.agent_id == agent_id and v.state == EscalationState.PENDING:
                self.escalations[k] = v.model_copy(update={"state": EscalationState.CANCELLED})

    async def expire_due(self, now: datetime) -> int:
        count = 0
        for k, v in list(self.escalations.items()):
            if v.state == EscalationState.PENDING and v.expires_at <= now:
                self.escalations[k] = v.model_copy(update={"state": EscalationState.EXPIRED})
                count += 1
        return count
