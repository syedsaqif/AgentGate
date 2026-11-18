"""Member B explicit identity/audit substitutes for neighbor development."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from contracts.audit_models import AuditEvent, EvidenceBundle, VerifyReport
from contracts.common import AuditRef, HumanRole, OpaqueId, Tx
from contracts.core_models import MandateSnapshot
from contracts.identity_models import (
    AgentPublicIdentity,
    AgentStatus,
    AuthContext,
    HumanContext,
)
from contracts.reason_codes import ReasonCode
from gateway.identity.errors import (
    AuthenticationError,
    PinVerificationError,
    ResourceNotFoundError,
)


@dataclass
class FixtureIdentityVerifier:
    result: AuthContext | None = None
    error: Exception | None = None

    async def authenticate(
        self,
        request_bytes: bytes,
        headers: Mapping[str, str],
        method: str,
        url: str,
    ) -> AuthContext:
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise RuntimeError("FixtureIdentityVerifier requires an explicit result or error")
        return self.result


@dataclass
class FixtureHumanAuth:
    """Explicit test double for `HumanAuth` covering positive and negative outcomes."""

    sessions: dict[str, HumanContext] = field(default_factory=dict)
    valid_pins: dict[str, str] = field(default_factory=dict)
    error: Exception | None = None

    async def authenticate(self, session_token: str, required_role: HumanRole) -> HumanContext:
        if self.error is not None:
            raise self.error
        if not session_token:
            raise AuthenticationError(ReasonCode.E_AUTH_MISSING, "Missing session token.")
        ctx = self.sessions.get(session_token)
        if ctx is None or ctx.role != required_role:
            raise AuthenticationError(ReasonCode.E_AUTH_INVALID_TOKEN, "Invalid human session.")
        return ctx

    async def verify_pin(self, customer_id: OpaqueId, pin: str) -> None:
        if self.error is not None:
            raise self.error
        expected = self.valid_pins.get(customer_id)
        if expected is None or pin != expected:
            raise PinVerificationError()


@dataclass
class FixtureAgentRegistry:
    """Explicit test double for `contracts.identity_ports.AgentRegistry`."""

    agents: dict[str, AgentPublicIdentity] = field(default_factory=dict)

    async def get(self, agent_id: OpaqueId) -> AgentPublicIdentity:
        agent = self.agents.get(agent_id)
        if agent is None:
            raise ResourceNotFoundError("Agent not found.")
        return agent

    async def set_status(self, agent_id: OpaqueId, status: AgentStatus, tx: Tx) -> None:
        agent = self.agents.get(agent_id)
        if agent is None:
            raise ResourceNotFoundError("Agent not found.")
        self.agents[agent_id] = agent.model_copy(update={"status": status})

    async def list_public(self) -> tuple[AgentPublicIdentity, ...]:
        return tuple(self.agents[key] for key in sorted(self.agents))


@dataclass
class FixtureMandateReader:
    """Explicit test double for `contracts.core_ports.MandateReader` used by B04 tests."""

    mandates: dict[str, MandateSnapshot] = field(default_factory=dict)

    async def get(self, mandate_id: OpaqueId, tx: Tx | None = None) -> MandateSnapshot:
        snapshot = self.mandates.get(mandate_id)
        if snapshot is None:
            raise ResourceNotFoundError("Mandate not found.", internal_reason="mandate_not_found")
        return snapshot


@dataclass
class MemoryAuditLog:
    """In-memory `AuditLog` test double with an explicit failure switch."""

    fail_append: bool = False
    events: list[AuditEvent] = field(default_factory=list)
    evidence_by_request: dict[str, EvidenceBundle] = field(default_factory=dict)

    async def append(self, event: AuditEvent, tx: Tx | None = None) -> AuditRef:
        if self.fail_append:
            raise RuntimeError("injected audit failure")
        self.events.append(event)
        return AuditRef(seq=len(self.events), entry_hash="0" * 64)

    async def verify(self, anchor: AuditRef | None = None) -> VerifyReport:
        head_seq = len(self.events)
        anchored = anchor.seq if anchor is not None else 0
        return VerifyReport(
            valid=True,
            entries_checked=head_seq,
            head_seq=head_seq,
            head_hash="0" * 64,
            anchored_through_seq=anchored,
            first_failure=None,
        )

    async def checkpoint(self) -> AuditRef:
        return AuditRef(seq=max(1, len(self.events)), entry_hash="0" * 64)

    async def evidence(self, request_id: OpaqueId) -> EvidenceBundle:
        bundle = self.evidence_by_request.get(request_id)
        if bundle is None:
            raise ResourceNotFoundError("Evidence not found.")
        return bundle
