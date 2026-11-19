"""Member C ScenarioClient abstraction and fixture double."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from contracts.core_models import CanonicalAction


class ScenarioClient(Protocol):
    """Protocol for scenario drivers to interact with gateway and simulation endpoints."""

    async def request_token(self, agent_id: str, mandate_id: str) -> dict[str, Any]: ...

    async def send_action(
        self, action: CanonicalAction, token: str | None = None
    ) -> dict[str, Any]: ...

    async def get_action(self, request_id: str, token: str | None = None) -> dict[str, Any]: ...

    async def get_escalation(
        self, escalation_id: str, token: str | None = None
    ) -> dict[str, Any]: ...

    async def customer_login(self, customer_id: str, pin: str) -> dict[str, Any]: ...

    async def get_customer_escalations(
        self, session_token: str, status: str = "PENDING"
    ) -> dict[str, Any]: ...

    async def decide_escalation(
        self,
        session_token: str,
        escalation_id: str,
        decision: str,
        pin: str | None = None,
        suspend_agent: bool = False,
    ) -> dict[str, Any]: ...

    async def revoke_mandate(self, session_token: str, mandate_id: str) -> dict[str, Any]: ...

    async def ops_login(self, username: str, password: str) -> dict[str, Any]: ...

    async def suspend_agent(
        self, session_token: str, agent_id: str, reason: str | None = None
    ) -> dict[str, Any]: ...

    async def reinstate_agent(self, session_token: str, agent_id: str) -> dict[str, Any]: ...

    async def get_audit_entries(self, session_token: str) -> dict[str, Any]: ...

    async def verify_audit(self, session_token: str) -> dict[str, Any]: ...

    async def get_evidence(self, session_token: str, request_id: str) -> dict[str, Any]: ...


@dataclass
class FixtureScenarioClient:
    """Configurable test double implementing ScenarioClient for scenario development."""

    token_responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    action_responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    escalation_responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    customer_sessions: dict[str, str] = field(
        default_factory=lambda: {"cust_001": "sess_cust_001"}
    )
    ops_sessions: dict[str, str] = field(
        default_factory=lambda: {"ops_admin": "sess_ops_001"}
    )
    recorded_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    simulated_audit_entries: list[dict[str, Any]] = field(default_factory=list)

    async def request_token(self, agent_id: str, mandate_id: str) -> dict[str, Any]:
        self.recorded_calls.append(
            ("request_token", {"agent_id": agent_id, "mandate_id": mandate_id})
        )
        return self.token_responses.get(
            agent_id,
            {
                "access_token": f"tok_{agent_id}_{mandate_id}",
                "token_type": "DPoP",
                "expires_in": 300,
            },
        )

    async def send_action(
        self, action: CanonicalAction, token: str | None = None
    ) -> dict[str, Any]:
        self.recorded_calls.append(
            ("send_action", {"request_id": action.request_id, "token": token})
        )
        return self.action_responses.get(
            action.request_id,
            {
                "request_id": action.request_id,
                "decision": "ALLOW",
                "status": "EXECUTED",
                "audit_ref": {"seq": 1, "entry_hash": "a" * 64},
            },
        )

    async def get_action(self, request_id: str, token: str | None = None) -> dict[str, Any]:
        self.recorded_calls.append(("get_action", {"request_id": request_id, "token": token}))
        return self.action_responses.get(
            request_id, {"request_id": request_id, "status": "NOT_FOUND"}
        )

    async def get_escalation(
        self, escalation_id: str, token: str | None = None
    ) -> dict[str, Any]:
        self.recorded_calls.append(
            ("get_escalation", {"escalation_id": escalation_id, "token": token})
        )
        return self.escalation_responses.get(
            escalation_id,
            {"escalation_id": escalation_id, "state": "PENDING"},
        )

    async def customer_login(self, customer_id: str, pin: str) -> dict[str, Any]:
        self.recorded_calls.append(("customer_login", {"customer_id": customer_id, "pin": pin}))
        if pin != "4821":
            return {"error": {"code": "E_ESC_BAD_PIN", "message": "Invalid PIN"}}
        token = self.customer_sessions.get(customer_id, f"sess_{customer_id}")
        return {"session_token": token, "role": "customer", "expires_in": 3600}

    async def get_customer_escalations(
        self, session_token: str, status: str = "PENDING"
    ) -> dict[str, Any]:
        self.recorded_calls.append(
            ("get_customer_escalations", {"session_token": session_token, "status": status})
        )
        return {"items": list(self.escalation_responses.values()), "next_cursor": None}

    async def decide_escalation(
        self,
        session_token: str,
        escalation_id: str,
        decision: str,
        pin: str | None = None,
        suspend_agent: bool = False,
    ) -> dict[str, Any]:
        self.recorded_calls.append(
            (
                "decide_escalation",
                {
                    "session_token": session_token,
                    "escalation_id": escalation_id,
                    "decision": decision,
                    "pin": pin,
                    "suspend_agent": suspend_agent,
                },
            )
        )
        if decision == "APPROVE" and pin != "4821":
            return {"error": {"code": "E_ESC_BAD_PIN", "message": "Invalid customer PIN"}}
        updated = {
            "escalation_id": escalation_id,
            "state": "APPROVED" if decision == "APPROVE" else "REJECTED",
            "decided_at": datetime.now(UTC).isoformat(),
        }
        self.escalation_responses[escalation_id] = updated
        return updated

    async def revoke_mandate(self, session_token: str, mandate_id: str) -> dict[str, Any]:
        self.recorded_calls.append(
            ("revoke_mandate", {"session_token": session_token, "mandate_id": mandate_id})
        )
        return {"mandate_id": mandate_id, "status": "REVOKED"}

    async def ops_login(self, username: str, password: str) -> dict[str, Any]:
        self.recorded_calls.append(("ops_login", {"username": username}))
        token = self.ops_sessions.get(username, "sess_ops")
        return {"session_token": token, "role": "ops", "expires_in": 3600}

    async def suspend_agent(
        self, session_token: str, agent_id: str, reason: str | None = None
    ) -> dict[str, Any]:
        self.recorded_calls.append(
            (
                "suspend_agent",
                {"session_token": session_token, "agent_id": agent_id, "reason": reason},
            )
        )
        return {"agent_id": agent_id, "status": "SUSPENDED"}

    async def reinstate_agent(self, session_token: str, agent_id: str) -> dict[str, Any]:
        self.recorded_calls.append(
            ("reinstate_agent", {"session_token": session_token, "agent_id": agent_id})
        )
        return {"agent_id": agent_id, "status": "ACTIVE"}

    async def get_audit_entries(self, session_token: str) -> dict[str, Any]:
        self.recorded_calls.append(("get_audit_entries", {"session_token": session_token}))
        return {"items": self.simulated_audit_entries}

    async def verify_audit(self, session_token: str) -> dict[str, Any]:
        self.recorded_calls.append(("verify_audit", {"session_token": session_token}))
        return {"valid": True, "checked_count": len(self.simulated_audit_entries)}

    async def get_evidence(self, session_token: str, request_id: str) -> dict[str, Any]:
        self.recorded_calls.append(
            ("get_evidence", {"session_token": session_token, "request_id": request_id})
        )
        return {
            "request_id": request_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "events": self.simulated_audit_entries,
            "signer_key_id": "key_test",
        }
