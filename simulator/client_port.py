"""Member C ScenarioClient abstraction and fixture double."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from contracts.core_models import CanonicalAction


@dataclass
class ScenarioResult:
    """Standardized result envelope for deterministic scenario evaluation."""

    scenario_id: str
    name: str
    passed: bool
    http_verified: bool
    ledger_verified: bool
    usage_verified: bool
    audit_verified: bool
    skipped: bool = False
    skip_reason: str | None = None
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


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

    async def get_balance(
        self, account_id: str, token: str | None = None
    ) -> dict[str, Any]: ...

    async def get_customer_mandates(self, session_token: str) -> dict[str, Any]: ...

    async def reset_state(self) -> dict[str, Any]: ...


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

    # State tracking: Bank ledger & Mandate usage
    ledger_balances: dict[str, int] = field(
        default_factory=lambda: {"ACC-001": 12_500_000}
    )
    mandate_usages: dict[str, dict[str, int]] = field(
        default_factory=lambda: {
            "mnd_demo_1": {
                "committed_paise": 0,
                "lifetime_budget": 1_000_000,
                "remaining_paise": 1_000_000,
            }
        }
    )
    mandate_caps: dict[str, int] = field(
        default_factory=lambda: {"mnd_demo_1": 500_000}
    )
    mandate_payees: dict[str, set[str]] = field(
        default_factory=lambda: {"mnd_demo_1": {"BILL-ELEC"}}
    )
    mandate_actions: dict[str, set[str]] = field(
        default_factory=lambda: {"mnd_demo_1": {"bill_pay", "ActionType.BILL_PAY"}}
    )
    mandate_windows: dict[str, tuple[datetime, datetime]] = field(
        default_factory=dict
    )

    _lock: asyncio.Lock | None = field(default=None, init=False, repr=False)

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def request_token(self, agent_id: str, mandate_id: str) -> dict[str, Any]:
        async with self.lock:
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
        async with self.lock:
            self.recorded_calls.append(
                ("send_action", {"request_id": action.request_id, "token": token})
            )

            if action.request_id in self.action_responses:
                resp = self.action_responses[action.request_id]
                # If simulated success, apply ledger & usage decrements
                if resp.get("status") == "EXECUTED":
                    amt = action.params.amount_paise
                    acc = action.params.from_account
                    self.ledger_balances[acc] = self.ledger_balances.get(acc, 12_500_000) - amt
                    mnd = action.mandate_id
                    if mnd in self.mandate_usages:
                        self.mandate_usages[mnd]["committed_paise"] += amt
                        self.mandate_usages[mnd]["remaining_paise"] -= amt
                return resp

            # Default simulated policy/risk/bank behavior
            amount = action.params.amount_paise
            from_acc = action.params.from_account
            payee = action.params.payee_id
            action_type = str(action.action_type)
            mnd = action.mandate_id

            # 1. Mandate validity window check
            if mnd in self.mandate_windows:
                valid_from, valid_until = self.mandate_windows[mnd]
                req_time = action.received_at
                if req_time < valid_from:
                    err_resp = {
                        "error": {
                            "code": "E_MANDATE_REVOKED",
                            "message": (
                                f"Mandate {mnd} not yet valid (valid from {valid_from.isoformat()})"
                            ),
                            "request_id": action.request_id,
                        }
                    }
                    self.simulated_audit_entries.append({
                        "event_type": "policy.denied",
                        "request_id": action.request_id,
                        "reason_code": "E_MANDATE_REVOKED",
                    })
                    return err_resp
                if req_time > valid_until:
                    err_resp = {
                        "error": {
                            "code": "E_MANDATE_REVOKED",
                            "message": f"Mandate {mnd} expired at {valid_until.isoformat()}",
                            "request_id": action.request_id,
                        }
                    }
                    self.simulated_audit_entries.append({
                        "event_type": "policy.denied",
                        "request_id": action.request_id,
                        "reason_code": "E_MANDATE_REVOKED",
                    })
                    return err_resp

            # 2. Check allowed action types
            if mnd in self.mandate_actions:
                allowed_actions = self.mandate_actions[mnd]
                act_val = action_type.split(".")[-1].lower()
                allowed_norm = {a.split(".")[-1].lower() for a in allowed_actions}
                if act_val not in allowed_norm:
                    err_resp = {
                        "error": {
                            "code": "E_POLICY_ACTION_NOT_ALLOWED",
                            "message": f"Action {action_type} not permitted by mandate",
                            "request_id": action.request_id,
                        }
                    }
                    self.simulated_audit_entries.append({
                        "event_type": "policy.denied",
                        "request_id": action.request_id,
                        "reason_code": "E_POLICY_ACTION_NOT_ALLOWED",
                    })
                    return err_resp

            # 3. Check per-transaction limit
            cap = self.mandate_caps.get(mnd, 500_000)
            if amount > cap:
                err_resp = {
                    "error": {
                        "code": "E_POLICY_AMOUNT_EXCEEDS_CAP",
                        "message": f"Amount {amount} exceeds per-transaction limit of {cap}",
                        "request_id": action.request_id,
                    }
                }
                self.simulated_audit_entries.append({
                    "event_type": "policy.denied",
                    "request_id": action.request_id,
                    "reason_code": "E_POLICY_AMOUNT_EXCEEDS_CAP",
                })
                return err_resp

            # 4. Check allowed payees
            if mnd in self.mandate_payees:
                allowed_payees = self.mandate_payees[mnd]
                if payee not in allowed_payees:
                    err_resp = {
                        "error": {
                            "code": "E_POLICY_PAYEE_NOT_ALLOWED",
                            "message": f"Payee {payee} outside allowed scope",
                            "request_id": action.request_id,
                        }
                    }
                    self.simulated_audit_entries.append({
                        "event_type": "policy.denied",
                        "request_id": action.request_id,
                        "reason_code": "E_POLICY_PAYEE_NOT_ALLOWED",
                    })
                    return err_resp

            # 5. Check remaining budget
            if mnd in self.mandate_usages:
                if amount > self.mandate_usages[mnd]["remaining_paise"]:
                    err_resp = {
                        "error": {
                            "code": "E_BANK_INSUFFICIENT_FUNDS",
                            "message": "Insufficient mandate budget remaining",
                            "request_id": action.request_id,
                        }
                    }
                    self.simulated_audit_entries.append({
                        "event_type": "policy.denied",
                        "request_id": action.request_id,
                        "reason_code": "E_BANK_INSUFFICIENT_FUNDS",
                    })
                    return err_resp

            # Successful execution
            self.ledger_balances[from_acc] = (
                self.ledger_balances.get(from_acc, 12_500_000) - amount
            )
            if mnd in self.mandate_usages:
                self.mandate_usages[mnd]["committed_paise"] += amount
                self.mandate_usages[mnd]["remaining_paise"] -= amount

            seq = len(self.simulated_audit_entries) + 1
            self.simulated_audit_entries.extend([
                {
                    "event_type": "identity.verified",
                    "agent_id": action.agent_id,
                    "request_id": action.request_id,
                },
                {
                    "event_type": "policy.evaluated",
                    "verdict": "ALLOW",
                    "request_id": action.request_id,
                },
                {
                    "event_type": "risk.scored",
                    "score": 10,
                    "level": "LOW",
                    "request_id": action.request_id,
                },
                {
                    "event_type": "bank.executed",
                    "status": "EXECUTED",
                    "amount_paise": amount,
                    "request_id": action.request_id,
                },
            ])

            return {
                "request_id": action.request_id,
                "decision": "ALLOW",
                "status": "EXECUTED",
                "audit_ref": {"seq": seq, "entry_hash": f"{seq:064x}"},
            }

    async def get_action(self, request_id: str, token: str | None = None) -> dict[str, Any]:
        async with self.lock:
            self.recorded_calls.append(("get_action", {"request_id": request_id, "token": token}))
            return self.action_responses.get(
                request_id, {"request_id": request_id, "status": "NOT_FOUND"}
            )

    async def get_escalation(
        self, escalation_id: str, token: str | None = None
    ) -> dict[str, Any]:
        async with self.lock:
            self.recorded_calls.append(
                ("get_escalation", {"escalation_id": escalation_id, "token": token})
            )
            return self.escalation_responses.get(
                escalation_id,
                {"escalation_id": escalation_id, "state": "PENDING"},
            )

    async def customer_login(self, customer_id: str, pin: str) -> dict[str, Any]:
        async with self.lock:
            self.recorded_calls.append(
                ("customer_login", {"customer_id": customer_id, "pin": pin})
            )
            if pin != "4821":
                return {"error": {"code": "E_ESC_BAD_PIN", "message": "Invalid PIN"}}
            token = self.customer_sessions.get(customer_id, f"sess_{customer_id}")
            return {"session_token": token, "role": "customer", "expires_in": 3600}

    async def get_customer_escalations(
        self, session_token: str, status: str = "PENDING"
    ) -> dict[str, Any]:
        async with self.lock:
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
        async with self.lock:
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
        async with self.lock:
            self.recorded_calls.append(
                ("revoke_mandate", {"session_token": session_token, "mandate_id": mandate_id})
            )
            return {"mandate_id": mandate_id, "status": "REVOKED"}

    async def ops_login(self, username: str, password: str) -> dict[str, Any]:
        async with self.lock:
            self.recorded_calls.append(("ops_login", {"username": username}))
            token = self.ops_sessions.get(username, "sess_ops")
            return {"session_token": token, "role": "ops", "expires_in": 3600}

    async def suspend_agent(
        self, session_token: str, agent_id: str, reason: str | None = None
    ) -> dict[str, Any]:
        async with self.lock:
            self.recorded_calls.append(
                (
                    "suspend_agent",
                    {"session_token": session_token, "agent_id": agent_id, "reason": reason},
                )
            )
            return {"agent_id": agent_id, "status": "SUSPENDED"}

    async def reinstate_agent(self, session_token: str, agent_id: str) -> dict[str, Any]:
        async with self.lock:
            self.recorded_calls.append(
                ("reinstate_agent", {"session_token": session_token, "agent_id": agent_id})
            )
            return {"agent_id": agent_id, "status": "ACTIVE"}

    async def get_audit_entries(self, session_token: str) -> dict[str, Any]:
        async with self.lock:
            self.recorded_calls.append(("get_audit_entries", {"session_token": session_token}))
            return {"items": list(self.simulated_audit_entries)}

    async def verify_audit(self, session_token: str) -> dict[str, Any]:
        async with self.lock:
            self.recorded_calls.append(("verify_audit", {"session_token": session_token}))
            return {"valid": True, "checked_count": len(self.simulated_audit_entries)}

    async def get_evidence(self, session_token: str, request_id: str) -> dict[str, Any]:
        async with self.lock:
            self.recorded_calls.append(
                ("get_evidence", {"session_token": session_token, "request_id": request_id})
            )
            return {
                "request_id": request_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "events": list(self.simulated_audit_entries),
                "signer_key_id": "key_test",
            }

    async def get_balance(
        self, account_id: str, token: str | None = None
    ) -> dict[str, Any]:
        async with self.lock:
            self.recorded_calls.append(("get_balance", {"account_id": account_id, "token": token}))
            balance = self.ledger_balances.get(account_id, 0)
            return {
                "account_id": account_id,
                "balance_paise": balance,
                "currency": "INR",
            }

    async def get_customer_mandates(self, session_token: str) -> dict[str, Any]:
        async with self.lock:
            self.recorded_calls.append(
                ("get_customer_mandates", {"session_token": session_token})
            )
            items = [
                {"mandate_id": mnd_id, **usage}
                for mnd_id, usage in self.mandate_usages.items()
            ]
            return {"items": items}

    async def reset_state(self) -> dict[str, Any]:
        async with self.lock:
            self.recorded_calls.append(("reset_state", {}))
            self.ledger_balances = {"ACC-001": 12_500_000}
            self.mandate_usages = {
                "mnd_demo_1": {
                    "committed_paise": 0,
                    "lifetime_budget": 1_000_000,
                    "remaining_paise": 1_000_000,
                }
            }
            self.mandate_caps = {"mnd_demo_1": 500_000}
            self.mandate_payees = {"mnd_demo_1": {"BILL-ELEC"}}
            self.mandate_actions = {"mnd_demo_1": {"bill_pay", "ActionType.BILL_PAY"}}
            self.mandate_windows.clear()
            self.simulated_audit_entries.clear()
            self.action_responses.clear()
            self.escalation_responses.clear()
            return {"status": "RESET"}


__all__ = ["FixtureScenarioClient", "ScenarioClient", "ScenarioResult"]
