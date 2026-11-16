"""Member B owned append/query/verification protocol."""

from __future__ import annotations

from typing import Protocol

from .audit_models import AuditEvent, EvidenceBundle, VerifyReport
from .common import AuditRef, OpaqueId, Tx


class AuditLog(Protocol):
    async def append(self, event: AuditEvent, tx: Tx | None = None) -> AuditRef: ...

    async def verify(self, anchor: AuditRef | None = None) -> VerifyReport: ...

    async def checkpoint(self) -> AuditRef: ...

    async def evidence(self, request_id: OpaqueId) -> EvidenceBundle: ...
