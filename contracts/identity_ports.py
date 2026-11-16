"""Member B owned identity and human-auth protocols."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from .common import HumanRole, OpaqueId, Tx
from .identity_models import AgentPublicIdentity, AgentStatus, AuthContext, HumanContext


class IdentityVerifier(Protocol):
    async def authenticate(
        self,
        request_bytes: bytes,
        headers: Mapping[str, str],
        method: str,
        url: str,
    ) -> AuthContext: ...


class HumanAuth(Protocol):
    async def authenticate(self, session_token: str, required_role: HumanRole) -> HumanContext: ...

    async def verify_pin(self, customer_id: OpaqueId, pin: str) -> None: ...


class AgentRegistry(Protocol):
    async def get(self, agent_id: OpaqueId) -> AgentPublicIdentity: ...

    async def set_status(self, agent_id: OpaqueId, status: AgentStatus, tx: Tx) -> None: ...

    async def list_public(self) -> tuple[AgentPublicIdentity, ...]: ...


class TokenService(Protocol):
    async def revoke_for_mandate(self, mandate_id: OpaqueId, tx: Tx) -> None: ...

    async def revoke_for_agent(self, agent_id: OpaqueId, tx: Tx) -> None: ...
