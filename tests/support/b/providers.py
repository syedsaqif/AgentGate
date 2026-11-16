"""Member B explicit identity/audit substitutes for neighbor development."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from contracts.audit_models import AuditEvent
from contracts.common import AuditRef
from contracts.identity_models import AuthContext


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
class MemoryAuditLog:
    fail_append: bool = False
    events: list[AuditEvent] = field(default_factory=list)

    async def append(self, event: AuditEvent, tx: object | None = None) -> AuditRef:
        if self.fail_append:
            raise RuntimeError("injected audit failure")
        self.events.append(event)
        return AuditRef(seq=len(self.events), entry_hash="0" * 64)
