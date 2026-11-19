"""Member C ScenarioClient abstraction and fixture double."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from contracts.core_models import CanonicalAction

_ZERO_HASH = "sha256:" + "0" * 64


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
    mandate_statuses: dict[str, str] = field(
        default_factory=lambda: {"mnd_demo_1": "ACTIVE"}
    )

    # Advanced security tracking (S2d, S5, S6, S7, S8, S10)
    suspended_agents: set[str] = field(default_factory=set)
    recent_denials: list[tuple[str, datetime]] = field(default_factory=list)
    structuring_attempts: list[tuple[str, datetime, int]] = field(default_factory=list)
    tamper_audit: bool = False
    auth_error_map: dict[str, tuple[str, int]] = field(
        default_factory=lambda: {
            "bad_missing": ("E_AUTH_MISSING", 401),
            "bad_replay": ("E_AUTH_REPLAY", 401),
            "bad_key": ("E_AUTH_KEY_MISMATCH", 401),
            "bad_suspended": ("E_AUTH_AGENT_SUSPENDED", 403),
            "bad_unknown_agent": ("E_AUTH_INVALID_TOKEN", 401),
            "bad_binding": ("E_AUTH_AGENT_MANDATE_MISMATCH", 403),
            "bad_expired": ("E_AUTH_EXPIRED", 401),
            "bad_tampered_sig": ("E_AUTH_PROOF_INVALID", 401),
            "bad_human_endpoint": ("E_AUTH_AGENT_SUSPENDED", 403),
        }
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
            # Check mandate status
            if self.mandate_statuses.get(mandate_id) == "REVOKED":
                return {
                    "error": {
                        "code": "E_MANDATE_REVOKED",
                        "message": f"Mandate {mandate_id} is REVOKED",
                    }
                }
            if agent_id in self.suspended_agents:
                return {
                    "error": {
                        "code": "E_AUTH_AGENT_SUSPENDED",
                        "message": f"Agent {agent_id} is SUSPENDED",
                    }
                }
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

            # 0a. Check auth errors from injected token
            if token and token in self.auth_error_map:
                code, _status = self.auth_error_map[token]
                self.simulated_audit_entries.append({
                    "event_type": "auth.failed",
                    "request_id": action.request_id,
                    "reason_code": code,
                })
                return {
                    "error": {
                        "code": code,
                        "message": f"Authentication rejected: {code}",
                        "request_id": action.request_id,
                    }
                }

            # 0b. Check suspended agent
            if action.agent_id in self.suspended_agents:
                self.simulated_audit_entries.append({
                    "event_type": "auth.agent_suspended",
                    "request_id": action.request_id,
                    "reason_code": "E_AUTH_AGENT_SUSPENDED",
                })
                return {
                    "error": {
                        "code": "E_AUTH_AGENT_SUSPENDED",
                        "message": f"Agent {action.agent_id} is currently suspended",
                        "request_id": action.request_id,
                    }
                }

            # 0c. Check mandate status
            mnd = action.mandate_id
            if self.mandate_statuses.get(mnd) == "REVOKED":
                self.simulated_audit_entries.append({
                    "event_type": "policy.denied",
                    "request_id": action.request_id,
                    "reason_code": "E_MANDATE_REVOKED",
                })
                return {
                    "error": {
                        "code": "E_MANDATE_REVOKED",
                        "message": f"Mandate {mnd} has been revoked",
                        "request_id": action.request_id,
                    }
                }

            if action.request_id in self.action_responses:
                resp = self.action_responses[action.request_id]
                if resp.get("status") == "EXECUTED":
                    amt = action.params.amount_paise
                    acc = action.params.from_account
                    self.ledger_balances[acc] = self.ledger_balances.get(acc, 12_500_000) - amt
                    if mnd in self.mandate_usages:
                        self.mandate_usages[mnd]["committed_paise"] += amt
                        self.mandate_usages[mnd]["remaining_paise"] -= amt
                return resp

            amount = action.params.amount_paise
            from_acc = action.params.from_account
            payee = action.params.payee_id
            action_type = str(action.action_type)

            # 1. Mandate validity window check
            if mnd in self.mandate_windows:
                valid_from, valid_until = self.mandate_windows[mnd]
                req_time = action.received_at
                if req_time < valid_from:
                    self.simulated_audit_entries.append({
                        "event_type": "policy.denied",
                        "request_id": action.request_id,
                        "reason_code": "E_MANDATE_REVOKED",
                    })
                    return {
                        "error": {
                            "code": "E_MANDATE_REVOKED",
                            "message": (
                                f"Mandate {mnd} not yet valid (valid from {valid_from.isoformat()})"
                            ),
                            "request_id": action.request_id,
                        }
                    }
                if req_time > valid_until:
                    self.simulated_audit_entries.append({
                        "event_type": "policy.denied",
                        "request_id": action.request_id,
                        "reason_code": "E_MANDATE_REVOKED",
                    })
                    return {
                        "error": {
                            "code": "E_MANDATE_REVOKED",
                            "message": f"Mandate {mnd} expired at {valid_until.isoformat()}",
                            "request_id": action.request_id,
                        }
                    }

            # 2. S7 Prompt Injection / Poisoned bill (₹40,000 to PAYEE-X)
            if payee == "PAYEE-X" and amount >= 4_000_000:
                self.suspended_agents.add(action.agent_id)
                self.simulated_audit_entries.extend([
                    {
                        "event_type": "risk.scored",
                        "score": 85,
                        "level": "HIGH",
                        "request_id": action.request_id,
                        "signals": ["intent_mismatch", "new_payee", "amount_outlier"],
                    },
                    {
                        "event_type": "breaker.recommended",
                        "reason_code": "E_RISK_BREAKER_OPEN",
                        "request_id": action.request_id,
                        "subject": action.agent_id,
                    },
                    {
                        "event_type": "agent.suspended",
                        "agent_id": action.agent_id,
                        "reason_code": "E_RISK_BREAKER_OPEN",
                    },
                ])
                return {
                    "decision": "DENY",
                    "status": "REJECTED",
                    "error": {
                        "code": "E_RISK_BREAKER_OPEN",
                        "message": (
                            "Circuit breaker opened: prompt injection and "
                            "unverified payee intent mismatch"
                        ),
                        "request_id": action.request_id,
                    },
                    "risk": {
                        "score": 85,
                        "level": "HIGH",
                        "signals": ["intent_mismatch", "new_payee", "amount_outlier"],
                        "breaker_recommended": True,
                    },
                }

            # 3. S8 Structuring detection (sequence of ₹1,990 = 199,000 paise attempts)
            if amount == 199_000:
                att_num = (
                    len([
                        1 for a_id, _, amt in self.structuring_attempts
                        if a_id == action.agent_id and amt == 199_000
                    ])
                    + 1
                )
                self.structuring_attempts.append(
                    (action.agent_id, action.received_at, amount)
                )

                if att_num == 1:
                    # Attempt 1: LOW risk, allowed
                    self.ledger_balances[from_acc] = (
                        self.ledger_balances.get(from_acc, 12_500_000) - amount
                    )
                    if mnd in self.mandate_usages:
                        self.mandate_usages[mnd]["committed_paise"] += amount
                        self.mandate_usages[mnd]["remaining_paise"] -= amount
                    seq = len(self.simulated_audit_entries) + 1
                    self.simulated_audit_entries.extend([
                        {
                            "event_type": "risk.scored",
                            "score": 15,
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
                        "risk": {"score": 15, "level": "LOW"},
                        "audit_ref": {"seq": seq, "entry_hash": f"{seq:064x}"},
                    }
                elif att_num in (2, 3):
                    # Attempts 2 & 3: MEDIUM risk, step-up escalation
                    score = 50 if att_num == 2 else 65
                    esc_id = f"esc_structuring_{action.request_id}"
                    self.escalation_responses[esc_id] = {
                        "escalation_id": esc_id,
                        "request_id": action.request_id,
                        "action": action,
                        "action_hash": _ZERO_HASH,
                        "state": "PENDING",
                        "mandate_id": mnd,
                        "customer_id": action.customer_id,
                        "agent_id": action.agent_id,
                        "amount_paise": amount,
                        "expires_at": (action.received_at + timedelta(seconds=300)).isoformat(),
                    }
                    self.simulated_audit_entries.append({
                        "event_type": "risk.scored",
                        "score": score,
                        "level": "MEDIUM",
                        "request_id": action.request_id,
                        "signals": ["near_limit_structuring", "velocity"],
                    })
                    return {
                        "request_id": action.request_id,
                        "decision": "ESCALATE",
                        "status": "PENDING_APPROVAL",
                        "escalation_id": esc_id,
                        "risk": {"score": score, "level": "MEDIUM"},
                    }
                elif att_num == 4:
                    # Attempt 4: HIGH risk, breaker trip and cancel pending
                    self.suspended_agents.add(action.agent_id)
                    for esc in self.escalation_responses.values():
                        if esc.get("agent_id") == action.agent_id and esc.get("state") == "PENDING":
                            esc["state"] = "CANCELLED"

                    self.simulated_audit_entries.extend([
                        {
                            "event_type": "risk.scored",
                            "score": 90,
                            "level": "HIGH",
                            "request_id": action.request_id,
                            "signals": ["probing", "velocity"],
                        },
                        {
                            "event_type": "breaker.recommended",
                            "reason_code": "E_RISK_BREAKER_OPEN",
                            "request_id": action.request_id,
                        },
                        {
                            "event_type": "agent.suspended",
                            "agent_id": action.agent_id,
                            "reason_code": "E_RISK_BREAKER_OPEN",
                        },
                    ])
                    return {
                        "decision": "DENY",
                        "status": "REJECTED",
                        "error": {
                            "code": "E_RISK_HIGH",
                            "message": (
                                "High-risk structuring threshold exceeded; "
                                "circuit breaker suspended agent"
                            ),
                            "request_id": action.request_id,
                        },
                        "risk": {"score": 90, "level": "HIGH", "breaker_recommended": True},
                    }
                else:
                    # Attempts 5 & 6: Denied by suspension
                    return {
                        "error": {
                            "code": "E_AUTH_AGENT_SUSPENDED",
                            "message": "Agent suspended after structuring threshold violation",
                            "request_id": action.request_id,
                        }
                    }

            # 4. S9a Step-Up (₹4,800 = 480,000 paise)
            if amount == 480_000:
                pending_count = len([
                    1 for e in self.escalation_responses.values()
                    if e.get("mandate_id") == mnd and e.get("state") == "PENDING"
                ])
                if pending_count >= 3:
                    self.simulated_audit_entries.append({
                        "event_type": "escalation.limit_reached",
                        "request_id": action.request_id,
                        "reason_code": "E_ESC_LIMIT_REACHED",
                    })
                    return {
                        "error": {
                            "code": "E_ESC_LIMIT_REACHED",
                            "message": "Maximum 3 pending approvals allowed per mandate",
                            "request_id": action.request_id,
                        }
                    }

                esc_id = f"esc_{action.request_id}"
                self.escalation_responses[esc_id] = {
                    "escalation_id": esc_id,
                    "request_id": action.request_id,
                    "action": action,
                    "action_hash": _ZERO_HASH,
                    "state": "PENDING",
                    "mandate_id": mnd,
                    "customer_id": action.customer_id,
                    "agent_id": action.agent_id,
                    "amount_paise": amount,
                    "expires_at": (action.received_at + timedelta(seconds=300)).isoformat(),
                }
                self.simulated_audit_entries.append({
                    "event_type": "risk.scored",
                    "score": 50,
                    "level": "MEDIUM",
                    "request_id": action.request_id,
                    "signals": ["amount_outlier"],
                })
                return {
                    "request_id": action.request_id,
                    "decision": "ESCALATE",
                    "status": "PENDING_APPROVAL",
                    "escalation_id": esc_id,
                    "risk": {"score": 50, "level": "MEDIUM"},
                }

            # 5. Check allowed action types
            if mnd in self.mandate_actions:
                allowed_actions = self.mandate_actions[mnd]
                act_val = action_type.split(".")[-1].lower()
                allowed_norm = {a.split(".")[-1].lower() for a in allowed_actions}
                if act_val not in allowed_norm:
                    self._record_denial(action, "E_POLICY_ACTION_NOT_ALLOWED")
                    return {
                        "error": {
                            "code": "E_POLICY_ACTION_NOT_ALLOWED",
                            "message": f"Action {action_type} not permitted by mandate",
                            "request_id": action.request_id,
                        }
                    }

            # 6. Check per-transaction limit
            cap = self.mandate_caps.get(mnd, 500_000)
            if amount > cap:
                self._record_denial(action, "E_POLICY_AMOUNT_EXCEEDS_CAP")
                return {
                    "error": {
                        "code": "E_POLICY_AMOUNT_EXCEEDS_CAP",
                        "message": f"Amount {amount} exceeds per-transaction limit of {cap}",
                        "request_id": action.request_id,
                    }
                }

            # 7. Check allowed payees
            if mnd in self.mandate_payees:
                allowed_payees = self.mandate_payees[mnd]
                if payee not in allowed_payees:
                    self._record_denial(action, "E_POLICY_PAYEE_NOT_ALLOWED")
                    return {
                        "error": {
                            "code": "E_POLICY_PAYEE_NOT_ALLOWED",
                            "message": f"Payee {payee} outside allowed scope",
                            "request_id": action.request_id,
                        }
                    }

            # 8. Check remaining budget
            if mnd in self.mandate_usages:
                if amount > self.mandate_usages[mnd]["remaining_paise"]:
                    self.simulated_audit_entries.append({
                        "event_type": "policy.denied",
                        "request_id": action.request_id,
                        "reason_code": "E_BANK_INSUFFICIENT_FUNDS",
                    })
                    return {
                        "error": {
                            "code": "E_BANK_INSUFFICIENT_FUNDS",
                            "message": "Insufficient mandate budget remaining",
                            "request_id": action.request_id,
                        }
                    }

            # 9. Normal successful execution
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

    def _record_denial(self, action: CanonicalAction, code: str) -> None:
        """Record policy denial and trip breaker if 5 denials occur within 10 minutes (S2d)."""
        self.simulated_audit_entries.append({
            "event_type": "policy.denied",
            "request_id": action.request_id,
            "reason_code": code,
        })
        self.recent_denials.append((action.agent_id, action.received_at))
        cutoff = action.received_at - timedelta(minutes=10)
        recent = [
            t for a_id, t in self.recent_denials
            if a_id == action.agent_id and t >= cutoff
        ]
        if len(recent) >= 5:
            self.suspended_agents.add(action.agent_id)
            self.simulated_audit_entries.extend([
                {
                    "event_type": "breaker.recommended",
                    "reason_code": "E_RISK_BREAKER_OPEN",
                    "request_id": action.request_id,
                    "agent_id": action.agent_id,
                    "message": "Suspended after 5 policy denials within 10 minutes",
                },
                {
                    "event_type": "agent.suspended",
                    "agent_id": action.agent_id,
                    "reason_code": "E_RISK_BREAKER_OPEN",
                },
            ])

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
            items = [
                e for e in self.escalation_responses.values()
                if status == "ALL" or e.get("state") == status
            ]
            return {"items": items, "next_cursor": None}

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
            # Human route check: Agent DPoP token cannot access customer endpoints
            is_cust = (
                session_token.startswith("sess_cust")
                or session_token in self.customer_sessions.values()
            )
            if not is_cust:
                return {
                    "error": {
                        "code": "E_AUTH_AGENT_SUSPENDED",
                        "message": "Agent credentials forbidden on human customer escalation route",
                    }
                }
            if decision == "APPROVE" and pin != "4821":
                self.simulated_audit_entries.append({
                    "event_type": "auth.pin_failed",
                    "escalation_id": escalation_id,
                    "reason_code": "E_ESC_BAD_PIN",
                })
                return {"error": {"code": "E_ESC_BAD_PIN", "message": "Invalid customer PIN"}}

            record = self.escalation_responses.get(escalation_id)
            if not record:
                record = {
                    "escalation_id": escalation_id,
                    "state": "PENDING",
                    "action": None,
                    "amount_paise": 480_000,
                    "mandate_id": "mnd_demo_1",
                }
                self.escalation_responses[escalation_id] = record

            curr_state = record.get("state")
            if curr_state in ("APPROVED", "EXECUTED", "REJECTED", "EXPIRED", "CANCELLED"):
                return {
                    "error": {
                        "code": "E_ESC_ALREADY_CONSUMED",
                        "message": f"Escalation already resolved in state {curr_state}",
                    }
                }

            if decision == "APPROVE":
                record["state"] = "APPROVED"
                record["decided_at"] = datetime.now(UTC).isoformat()

                # Execution handoff simulation
                act: CanonicalAction | None = record.get("action")
                amt = record.get("amount_paise", 480_000)
                from_acc = act.params.from_account if act else "ACC-001"
                mnd = record.get("mandate_id", "mnd_demo_1")

                self.ledger_balances[from_acc] = (
                    self.ledger_balances.get(from_acc, 12_500_000) - amt
                )
                if mnd in self.mandate_usages:
                    self.mandate_usages[mnd]["committed_paise"] += amt
                    self.mandate_usages[mnd]["remaining_paise"] -= amt

                record["receipt"] = {
                    "operation_id": f"bank_op_{escalation_id}",
                    "status": "EXECUTED",
                    "amount_paise": amt,
                    "executed_at": datetime.now(UTC).isoformat(),
                }
                self.simulated_audit_entries.extend([
                    {"event_type": "escalation.approved", "escalation_id": escalation_id},
                    {"event_type": "bank.executed", "status": "EXECUTED", "amount_paise": amt},
                ])
                return record

            elif decision == "REJECT":
                record["state"] = "REJECTED"
                record["decided_at"] = datetime.now(UTC).isoformat()
                if suspend_agent:
                    agt = record.get("agent_id", "agt_billpal")
                    self.suspended_agents.add(agt)
                    self.simulated_audit_entries.append({
                        "event_type": "agent.suspended",
                        "agent_id": agt,
                        "reason_code": "E_AUTH_AGENT_SUSPENDED",
                    })
                self.simulated_audit_entries.append({
                    "event_type": "escalation.rejected",
                    "escalation_id": escalation_id,
                })
                return record

            return {"error": {"code": "E_REQ_INVALID", "message": f"Unknown decision {decision}"}}

    async def expire_due(self, now: datetime) -> int:
        """Sweep pending records past their expiration deadline (S9c)."""
        async with self.lock:
            count = 0
            for esc in self.escalation_responses.values():
                if esc.get("state") == "PENDING":
                    exp_str = esc.get("expires_at")
                    if exp_str:
                        exp_dt = datetime.fromisoformat(exp_str)
                        if now >= exp_dt:
                            esc["state"] = "EXPIRED"
                            count += 1
                            self.simulated_audit_entries.append({
                                "event_type": "escalation.expired",
                                "escalation_id": esc["escalation_id"],
                            })
            return count

    async def revoke_mandate(self, session_token: str, mandate_id: str) -> dict[str, Any]:
        async with self.lock:
            self.recorded_calls.append(
                ("revoke_mandate", {"session_token": session_token, "mandate_id": mandate_id})
            )
            self.mandate_statuses[mandate_id] = "REVOKED"
            # Cancel all pending escalations for this mandate
            cancelled_count = 0
            for esc in self.escalation_responses.values():
                if esc.get("mandate_id") == mandate_id and esc.get("state") == "PENDING":
                    esc["state"] = "CANCELLED"
                    cancelled_count += 1

            self.simulated_audit_entries.append({
                "event_type": "mandate.revoked",
                "mandate_id": mandate_id,
                "cancelled_escalations": cancelled_count,
            })
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
            self.suspended_agents.add(agent_id)
            self.simulated_audit_entries.append({
                "event_type": "agent.suspended",
                "agent_id": agent_id,
                "reason": reason,
            })
            return {"agent_id": agent_id, "status": "SUSPENDED"}

    async def reinstate_agent(self, session_token: str, agent_id: str) -> dict[str, Any]:
        async with self.lock:
            self.recorded_calls.append(
                ("reinstate_agent", {"session_token": session_token, "agent_id": agent_id})
            )
            self.suspended_agents.discard(agent_id)
            return {"agent_id": agent_id, "status": "ACTIVE"}

    async def get_audit_entries(self, session_token: str) -> dict[str, Any]:
        async with self.lock:
            self.recorded_calls.append(("get_audit_entries", {"session_token": session_token}))
            return {"items": list(self.simulated_audit_entries)}

    async def verify_audit(self, session_token: str) -> dict[str, Any]:
        async with self.lock:
            self.recorded_calls.append(("verify_audit", {"session_token": session_token}))
            if self.tamper_audit:
                return {
                    "valid": False,
                    "error": "Audit hash chain broken: entry 3 signature mismatch",
                    "checked_count": len(self.simulated_audit_entries),
                }
            return {"valid": True, "checked_count": len(self.simulated_audit_entries)}

    async def get_evidence(self, session_token: str, request_id: str) -> dict[str, Any]:
        async with self.lock:
            self.recorded_calls.append(
                ("get_evidence", {"session_token": session_token, "request_id": request_id})
            )
            return {
                "request_id": request_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "mandate": {
                    "mandate_id": "mnd_demo_1",
                    "mandate_hash": _ZERO_HASH,
                    "terms": {
                        "customer_id": "cust_001",
                        "agent_id": "agt_billpal",
                        "version": 1,
                    },
                },
                "agent_identity": {
                    "agent_id": "agt_billpal",
                    "name": "BillPal",
                    "jkt": "jkt_billpal_001",
                    "status": "ACTIVE",
                },
                "proof_evidence": {
                    "proof_jti": "prf_test_jti",
                    "token_jti": "tok_test_jti",
                    "sig_valid": True,
                },
                "risk_assessment": {
                    "score": 10,
                    "level": "LOW",
                    "recommended": "ALLOW",
                    "model_version": "rules_v1",
                },
                "approval_metadata": None,
                "bank_result": {
                    "bank_operation_id": "bank_op_001",
                    "status": "EXECUTED",
                    "amount_paise": 185_000,
                    "executed_at": datetime.now(UTC).isoformat(),
                },
                "verification_scope": "FULL",
                "bundle_sig": "sig_evidence_bundle_ed25519",
                "events": list(self.simulated_audit_entries),
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
                {
                    "mandate_id": mnd_id,
                    "status": self.mandate_statuses.get(mnd_id, "ACTIVE"),
                    **usage,
                }
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
            self.mandate_statuses = {"mnd_demo_1": "ACTIVE"}
            self.mandate_windows.clear()
            self.suspended_agents.clear()
            self.recent_denials.clear()
            self.structuring_attempts.clear()
            self.tamper_audit = False
            self.simulated_audit_entries.clear()
            self.action_responses.clear()
            self.escalation_responses.clear()
            return {"status": "RESET"}


__all__ = ["FixtureScenarioClient", "ScenarioClient", "ScenarioResult"]
