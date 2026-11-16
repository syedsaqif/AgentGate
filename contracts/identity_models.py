"""Member B owned identity boundary DTOs."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from .common import ContractModel, HumanRole, OpaqueId


class AgentStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    DISABLED = "DISABLED"


class AuthContext(ContractModel):
    customer_id: OpaqueId
    agent_id: OpaqueId
    mandate_id: OpaqueId
    mandate_hash: OpaqueId
    jkt: OpaqueId
    token_jti: OpaqueId
    proof_jti: OpaqueId


class HumanContext(ContractModel):
    session_id: OpaqueId
    subject_id: OpaqueId
    role: HumanRole
    expires_at: datetime


class AgentPublicIdentity(ContractModel):
    agent_id: OpaqueId
    name: OpaqueId
    vendor: OpaqueId
    public_jwk: dict[str, str]
    jkt: OpaqueId
    max_scopes: tuple[str, ...]
    status: AgentStatus


class AccessTokenResult(ContractModel):
    access_token: str
    token_type: str = "DPoP"
    expires_in: int
