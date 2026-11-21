"""Deterministic BillPal agent brain and execution engine.

Follows plan.md sections 6, 10, 10A, 10B, and 10C.
Uses AgentGateClient.pay_bill and transfer for real signed requests.
Maintains ordered agent steps with run-scoped replay buffer for SSE clients.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException

from agent_sdk.client import AgentGateClient
from agent_sdk.keys import AgentKeyPair, generate_agent_keypair, load_private_key_pem
from agent_sdk.tokens import AgentAPIError
from demo_server.config import DemoServerConfig
from demo_server.gateway_client import GatewayClient
from demo_server.parser import ParsedPaymentInstruction, parse_prompt
from demo_server.scenarios import ScenarioCard, get_scenario_by_id


def _payment_verdict(status: str | None) -> str:
    return {"PENDING_APPROVAL": "ESCALATE", "EXECUTED": "ALLOW"}.get(status or "", "ERROR")


def _error_verdict(error: AgentAPIError) -> str:
    prefixes = ("E_POLICY_", "E_AUTH_", "E_REQ_", "E_MANDATE_", "E_RISK_")
    if 400 <= error.status_code < 500 and (error.error_code or "").startswith(prefixes):
        return "DENY"
    return "ERROR"


@dataclass
class AgentRunRecord:
    """Record of an agent run containing metadata and ordered step events."""

    run_id: str
    scenario_id: str | None
    prompt: str
    attachment: str | None
    agent_mode: str
    overrides: dict[str, Any] | None = None
    status: str = "running"  # "running" | "completed" | "refused" | "failed"
    verdict: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    request_ids: list[str] = field(default_factory=list)
    receipts: list[dict[str, Any]] = field(default_factory=list)
    subscribers: set[asyncio.Queue[dict[str, Any]]] = field(default_factory=set)
    created_at: float = field(default_factory=lambda: datetime.now(UTC).timestamp())
    finished: bool = False
    observation: dict[str, Any] = field(default_factory=dict)

    def append_event(self, event: dict[str, Any]) -> None:
        """Append event to ordered replay log and notify live subscribers."""
        self.events.append(event)
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass


class RunManager:
    """In-memory manager for agent scenario runs and retry caching."""

    def __init__(self) -> None:
        self._runs: dict[str, AgentRunRecord] = {}
        self._last_payment: dict[str, Any] | None = None
        self.execution_lock = asyncio.Lock()

    def create_run(
        self,
        scenario_id: str | None,
        prompt: str,
        attachment: str | None,
        agent_mode: str = "naive",
        overrides: dict[str, Any] | None = None,
    ) -> AgentRunRecord:
        run_id = f"run_{secrets.token_urlsafe(12)}"
        record = AgentRunRecord(
            run_id=run_id,
            scenario_id=scenario_id,
            prompt=prompt,
            attachment=attachment,
            agent_mode=agent_mode,
            overrides=overrides,
        )
        self._runs[run_id] = record
        return record

    def get_run(self, run_id: str) -> AgentRunRecord | None:
        return self._runs.get(run_id)

    def set_last_payment(self, payment_info: dict[str, Any]) -> None:
        self._last_payment = payment_info

    def get_last_payment(self) -> dict[str, Any] | None:
        return self._last_payment

    def clear(self) -> None:
        self._runs.clear()
        self._last_payment = None

    def clear_retry(self) -> None:
        self._last_payment = None


global_run_manager = RunManager()


class AgentBrain:
    """Deterministic agent engine that executes scenarios via AgentGateClient."""

    def __init__(
        self,
        config: DemoServerConfig,
        gateway_client: GatewayClient,
        run_manager: RunManager | None = None,
    ) -> None:
        self.config = config
        self.gateway_client = gateway_client
        self.run_manager = run_manager or global_run_manager
        self._keypair: AgentKeyPair | None = None

    def _get_keypair(self) -> AgentKeyPair:
        if self._keypair is not None:
            return self._keypair

        if self.config.billpal_key_path and Path(self.config.billpal_key_path).exists():
            self._keypair = load_private_key_pem(self.config.billpal_key_path)
        else:
            self._keypair = generate_agent_keypair()
        return self._keypair

    async def _resolve_active_mandate(self) -> str:
        """Resolve active mandate ID for BillPal from Gateway, or prepare default demo mandate."""
        # 1. Login as customer to list mandates
        pin = self.config.customer_pin or "4821"
        login_data = await self.gateway_client.customer_login(self.config.customer_id, pin)
        token = login_data["token"]
        mandates = await self.gateway_client.list_customer_mandates(token, limit=20)
        for item in mandates.get("items", []):
            if (
                item.get("status") == "ACTIVE"
                and item.get("terms", {}).get("agent_id") == "agt_billpal"
            ):
                return item["terms"]["mandate_id"]

        # 2. If no active mandate, create the exact default mandate from §10A.3
        now = datetime.now(UTC)
        valid_until = now + timedelta(days=7)
        payload = {
            "agent_id": "agt_billpal",
            "valid_from": now.isoformat().replace("+00:00", "Z"),
            "valid_until": valid_until.isoformat().replace("+00:00", "Z"),
            "grants": [
                {
                    "grant_id": "grant_billpal_elec",
                    "action": "bill_pay",
                    "account_ids": [self.config.account_id],
                    "payee_ids": ["BILL-ELEC"],
                    "currency": "INR",
                    "max_per_txn_paise": 500000,
                    "max_total_paise": 1000000,
                    "max_count": 5,
                    "window": "mandate",
                    "category": "utilities.electricity",
                }
            ],
            "step_up": {
                "amount_above_paise": 400000,
                "new_payee_requires_approval": False,
                "always_ask_actions": [],
            },
            "risk_profile": "balanced",
            "pin": pin,
        }
        res = await self.gateway_client.create_customer_mandate(token, payload)
        return res["mandate"]["terms"]["mandate_id"]

    async def execute_run(self, record: AgentRunRecord) -> None:
        """Execute scenario run asynchronously, streaming ordered events."""
        run_id = record.run_id
        scenario: ScenarioCard | None = (
            get_scenario_by_id(record.scenario_id) if record.scenario_id else None
        )
        prompt = record.prompt or (scenario.prompt if scenario else "")
        attachment = record.attachment or (scenario.attachment if scenario else None)

        try:
            # 1. Step: Thinking
            record.append_event(
                {
                    "step": "thinking",
                    "message": f"Analyzing user instruction: '{prompt}'",
                    "at": datetime.now(UTC).isoformat(),
                }
            )
            await asyncio.sleep(0.05)

            # 2. Step: Reading document if present
            if attachment:
                record.append_event(
                    {
                        "step": "reading_document",
                        "attachment": attachment,
                        "message": "Parsed invoice attachment document.",
                        "at": datetime.now(UTC).isoformat(),
                    }
                )
                await asyncio.sleep(0.05)

            # 3. Step: Parsing instruction
            instruction: ParsedPaymentInstruction = parse_prompt(
                prompt=prompt,
                attachment=attachment,
                agent_mode=record.agent_mode,  # type: ignore[arg-type]
            )
            if record.overrides and not instruction.is_refused:
                custom = record.overrides
                instruction = replace(
                    instruction, action=custom["action"], payee_id=custom["payee_id"],
                    amount_paise=custom["amount_paise"], declared_category=custom["category"],
                    is_clarification_needed=False,
                )

            # Cautious agent refusal
            if instruction.is_refused:
                record.status = "completed"
                record.verdict = "REFUSED"
                record.append_event(
                    {
                        "step": "refusal",
                        "reason": instruction.refusal_reason,
                        "message": instruction.refusal_reason,
                        "gateway_calls": 0,
                        "at": datetime.now(UTC).isoformat(),
                    }
                )
                record.append_event(
                    {
                        "step": "completed",
                        "verdict": "REFUSED",
                        "summary": instruction.refusal_reason,
                        "at": datetime.now(UTC).isoformat(),
                    }
                )
                return

            # Fixture-backed scenarios (C1-C6, T1)
            if scenario and scenario.source in ("defensive_test", "offline_verifier"):
                record.append_event(
                    {
                        "step": "plan",
                        "action": "fixture_demonstration",
                        "description": scenario.description,
                        "source": scenario.source,
                        "at": datetime.now(UTC).isoformat(),
                    }
                )
                await asyncio.sleep(0.05)
                record.append_event(
                    {
                        "step": "tool_result",
                        "verdict": scenario.expected_verdict,
                        "reason_code": scenario.expected_reason_code,
                        "fixture": scenario.fixture,
                        "message": (
                            f"Verified defensive test result: {scenario.expected_stop_point}"
                        ),
                        "at": datetime.now(UTC).isoformat(),
                    }
                )
                record.status = "completed"
                record.verdict = scenario.expected_verdict
                record.append_event(
                    {
                        "step": "completed",
                        "verdict": scenario.expected_verdict,
                        "summary": (
                            f"Stopped at {scenario.expected_stop_point} "
                            f"({scenario.expected_reason_code})"
                        ),
                        "at": datetime.now(UTC).isoformat(),
                    }
                )
                return

            # Unknown payee clarification (e.g. P8)
            if instruction.is_clarification_needed:
                record.status = "completed"
                record.verdict = "CLARIFY"
                record.append_event(
                    {
                        "step": "clarification",
                        "message": instruction.clarification_message,
                        "gateway_calls": 0,
                        "at": datetime.now(UTC).isoformat(),
                    }
                )
                record.append_event(
                    {
                        "step": "completed",
                        "verdict": "CLARIFY",
                        "summary": instruction.clarification_message,
                        "at": datetime.now(UTC).isoformat(),
                    }
                )
                return

            # Retry only a known executed bill with the exact submitted payload and mandate.
            if scenario and scenario.id == "P6" and not self.run_manager.get_last_payment():
                record.status = "completed"
                record.verdict = "REFUSED"
                record.append_event({
                    "step": "refusal", "message": "Run a successful bill payment before retrying.",
                })
                record.append_event({"step": "completed", "verdict": "REFUSED"})
                return

            # 4. Ordinary Live Signed SDK Calls
            keypair = self._get_keypair()

            # Handle P6 retry scenario: reuse last payment body & key
            if scenario and scenario.id.upper() in ("P6",) and self.run_manager.get_last_payment():
                last_p = self.run_manager.get_last_payment()
                assert last_p is not None
                record.append_event(
                    {
                        "step": "plan",
                        "action": "pay_bill",
                        "payee_id": last_p["payee_id"],
                        "amount_paise": last_p["amount_paise"],
                        "category": last_p["category"],
                        "idempotency_key": last_p["idempotency_key"],
                        "message": (
                            "Reusing original request body and Idempotency-Key for safe retry."
                        ),
                        "at": datetime.now(UTC).isoformat(),
                    }
                )
                trace_id = None

                async def remember_attempt(response: httpx.Response) -> None:
                    nonlocal trace_id
                    candidate = response.headers.get("X-Request-ID", "")
                    if response.request.url.path == "/v1/payments/bills" and re.fullmatch(
                        r"req_[a-f0-9]{16}", candidate
                    ):
                        trace_id = candidate

                async with (
                    httpx.AsyncClient(event_hooks={"response": [remember_attempt]}) as http,
                    AgentGateClient(
                        gateway_url=self.config.gateway_origin, agent_id="agt_billpal",
                        mandate_id=last_p["mandate_id"], keypair=keypair, http_client=http,
                    ) as client,
                ):
                    record.append_event(
                        {
                            "step": "tool_call",
                            "tool": "pay_bill",
                            "args": last_p,
                            "at": datetime.now(UTC).isoformat(),
                        }
                    )
                    res = await client.pay_bill(
                        from_account=last_p["from_account"],
                        payee_id=last_p["payee_id"],
                        amount_paise=last_p["amount_paise"],
                        task=last_p["task"],
                        category=last_p["category"],
                        idempotency_key=last_p["idempotency_key"],
                    )
                    req_id = res.get("request_id")
                    if res.get("status") != "EXECUTED" or req_id != last_p["request_id"]:
                        raise RuntimeError("Retry did not return the original execution.")
                    receipt = res.get("receipt", {})
                    record.request_ids.append(req_id)
                    record.receipts.append(receipt)
                    record.append_event(
                        {
                            "step": "tool_result",
                            "verdict": "ALLOW",
                            "status": "EXECUTED",
                            "cached": True,
                            "trace_request_id": trace_id,
                            "request_id": req_id,
                            "receipt": receipt,
                            "message": (
                                "Gateway recognized idempotency key; returned cached receipt. "
                                "Money moved once."
                            ),
                            "at": datetime.now(UTC).isoformat(),
                        }
                    )
                record.status = "completed"
                record.verdict = "ALLOW"
                record.append_event(
                    {
                        "step": "completed",
                        "verdict": "ALLOW",
                        "summary": "Retry returned original receipt; exactly one debit.",
                        "at": datetime.now(UTC).isoformat(),
                    }
                )
                return

            if scenario and scenario.id == "L7":
                login = await self.gateway_client.customer_login(
                    self.config.customer_id, self.config.customer_pin or ""
                )
                mandates = await self.gateway_client.list_customer_mandates(
                    login["token"], limit=50
                )
                mandate_id = next((
                    m["terms"]["mandate_id"] for m in mandates["items"]
                    if m["status"] == "REVOKED" and m["terms"]["agent_id"] == "agt_billpal"
                ), None)
                if not mandate_id:
                    record.status, record.verdict = "completed", "REFUSED"
                    record.append_event({
                        "step": "refusal", "message": "Revoke a BillPal mandate before this run.",
                    })
                    record.append_event({"step": "completed", "verdict": "REFUSED"})
                    return
            else:
                try:
                    mandate_id = await self._resolve_active_mandate()
                except HTTPException:
                    record.status, record.verdict = "failed", "ERROR"
                    record.append_event({
                        "step": "error", "reason_code": "DEMO_MANDATE_SETUP_FAILED",
                    })
                    record.append_event({
                        "step": "completed", "verdict": "ERROR",
                        "reason_code": "DEMO_MANDATE_SETUP_FAILED",
                    })
                    return

            # Handle L2 (Budget drain: 3 sequential calls of ₹4,000)
            if scenario and scenario.id.upper() in ("L2",):
                calls = [
                    (400000, "BILL-ELEC", "utilities.electricity", "call_1"),
                    (400000, "BILL-ELEC", "utilities.electricity", "call_2"),
                    (400000, "BILL-ELEC", "utilities.electricity", "call_3"),
                ]
                verdict = "ERROR"
                async with AgentGateClient(
                    gateway_url=self.config.gateway_origin,
                    agent_id="agt_billpal",
                    mandate_id=mandate_id,
                    keypair=keypair,
                ) as client:
                    for idx, (amt, pid, cat, _label) in enumerate(calls, start=1):
                        record.append_event(
                            {
                                "step": "tool_call",
                                "tool": "pay_bill",
                                "attempt": idx,
                                "amount_paise": amt,
                                "payee_id": pid,
                                "category": cat,
                                "from_account": self.config.account_id,
                                "message": f"Executing slice payment {idx}/3 (₹{amt // 100})",
                                "at": datetime.now(UTC).isoformat(),
                            }
                        )
                        try:
                            res = await client.pay_bill(
                                from_account=self.config.account_id,
                                payee_id=pid,
                                amount_paise=amt,
                                task="Budget slice payment",
                                category=cat,
                            )
                            req_id = res.get("request_id", f"req_l2_{idx}")
                            record.request_ids.append(req_id)
                            record.receipts.append(res.get("receipt", {}))
                            verdict = _payment_verdict(res.get("status"))
                            record.append_event(
                                {
                                    "step": "tool_result",
                                    "attempt": idx,
                                    "verdict": verdict,
                                    "status": res.get("status"),
                                    "request_id": req_id,
                                    "receipt": res.get("receipt"),
                                    "at": datetime.now(UTC).isoformat(),
                                }
                            )
                        except AgentAPIError as exc:
                            verdict = _error_verdict(exc)
                            req_id = exc.request_id or f"req_l2_{idx}"
                            record.request_ids.append(req_id)
                            record.append_event(
                                {
                                    "step": "tool_result",
                                    "attempt": idx,
                                    "verdict": verdict,
                                    "reason_code": exc.error_code,
                                    "message": f"Slice {idx} denied: {exc.message}",
                                    "request_id": req_id,
                                    "at": datetime.now(UTC).isoformat(),
                                }
                            )
                record.status = "completed"
                record.verdict = verdict
                record.append_event(
                    {
                        "step": "completed",
                        "verdict": verdict,
                        "at": datetime.now(UTC).isoformat(),
                    }
                )
                return

            # Handle L5 (Probing with spoofed category: 4 calls)
            if scenario and scenario.id.upper() in ("L5",):
                probe_payees = ["PAYEE-X", "PAYEE-CRYPTO", "BILL-INS", "PAYEE-X"]
                verdict = "ERROR"
                async with AgentGateClient(
                    gateway_url=self.config.gateway_origin,
                    agent_id="agt_billpal",
                    mandate_id=mandate_id,
                    keypair=keypair,
                ) as client:
                    for idx, pid in enumerate(probe_payees, start=1):
                        record.append_event(
                            {
                                "step": "tool_call",
                                "tool": "pay_bill",
                                "attempt": idx,
                                "payee_id": pid,
                                "category": "utilities.electricity",
                                "amount_paise": 50000,
                                "from_account": self.config.account_id,
                                "message": f"Probing attempt {idx}/4 to {pid}",
                                "at": datetime.now(UTC).isoformat(),
                            }
                        )
                        try:
                            result = await client.pay_bill(
                                from_account=self.config.account_id,
                                payee_id=pid,
                                amount_paise=50000,
                                task="Probing probe",
                                category="utilities.electricity",
                            )
                            verdict = _payment_verdict(result.get("status"))
                            if result.get("request_id"):
                                record.request_ids.append(result["request_id"])
                            record.append_event({
                                "step": "tool_result", "attempt": idx, "verdict": verdict,
                                "status": result.get("status"),
                                "request_id": result.get("request_id"),
                            })
                        except AgentAPIError as exc:
                            verdict = _error_verdict(exc)
                            req_id = exc.request_id or f"req_l5_{idx}"
                            record.request_ids.append(req_id)
                            record.append_event(
                                {
                                    "step": "tool_result",
                                    "attempt": idx,
                                    "verdict": verdict,
                                    "reason_code": exc.error_code,
                                    "request_id": req_id,
                                    "message": f"Attempt {idx} denied: {exc.message}",
                                    "at": datetime.now(UTC).isoformat(),
                                }
                            )
                record.status = "completed"
                record.verdict = verdict
                record.append_event(
                    {
                        "step": "completed",
                        "verdict": verdict,
                        "at": datetime.now(UTC).isoformat(),
                    }
                )
                return

            # Standard Single Payment / Transfer Execution
            action = instruction.action
            target_payee = instruction.payee_id or "BILL-ELEC"
            target_amount = instruction.amount_paise or 185000
            target_cat = instruction.declared_category
            payment_key = f"demo-{run_id}"

            record.append_event(
                {
                    "step": "plan",
                    "action": action,
                    "payee_id": target_payee,
                    "amount_paise": target_amount,
                    "category": target_cat,
                    "message": f"Planning {action} of ₹{target_amount // 100} to {target_payee}",
                    "at": datetime.now(UTC).isoformat(),
                }
            )

            async with AgentGateClient(
                gateway_url=self.config.gateway_origin,
                agent_id="agt_billpal",
                mandate_id=mandate_id,
                keypair=keypair,
            ) as client:
                record.append_event(
                    {
                        "step": "tool_call",
                        "tool": action,
                        "args": {
                            "from_account": self.config.account_id,
                            "payee_id": target_payee,
                            "amount_paise": target_amount,
                            "category": target_cat,
                        },
                        "at": datetime.now(UTC).isoformat(),
                    }
                )

                try:
                    if action == "transfer":
                        res = await client.transfer(
                            from_account=self.config.account_id,
                            payee_id=target_payee,
                            amount_paise=target_amount,
                            task="Demo transfer",
                            category=target_cat,
                            idempotency_key=payment_key,
                        )
                    else:
                        res = await client.pay_bill(
                            from_account=self.config.account_id,
                            payee_id=target_payee,
                            amount_paise=target_amount,
                            task="Demo payment",
                            category=target_cat,
                            idempotency_key=payment_key,
                        )

                    req_id = res.get("request_id", "req_live")
                    status_str = res.get("status")
                    receipt = res.get("receipt", {})
                    record.request_ids.append(req_id)
                    if receipt:
                        record.receipts.append(receipt)

                    # Store for P6 retry scenario
                    if status_str == "EXECUTED" and action == "pay_bill" and res.get("request_id"):
                        self.run_manager.set_last_payment({
                            "from_account": self.config.account_id,
                            "payee_id": target_payee,
                            "amount_paise": target_amount,
                            "task": "Demo payment",
                            "category": target_cat,
                            "idempotency_key": payment_key,
                            "mandate_id": mandate_id,
                            "request_id": req_id,
                        })

                    verdict = _payment_verdict(status_str)
                    record.append_event(
                        {
                            "step": "tool_result",
                            "verdict": verdict,
                            "status": status_str,
                            "request_id": req_id,
                            "receipt": receipt,
                            "escalation_id": res.get("escalation_id"),
                            "message": (
                                "Action requires customer step-up approval."
                                if verdict == "ESCALATE"
                                else (
                                    "Payment executed successfully. "
                                    f"Receipt {receipt.get('receipt_seq')}."
                                )
                            ),
                            "at": datetime.now(UTC).isoformat(),
                        }
                    )
                    record.status = "completed"
                    record.verdict = verdict
                    record.append_event(
                        {
                            "step": "completed",
                            "verdict": verdict,
                            "request_id": req_id,
                            "summary": f"{action} completed with verdict {verdict}.",
                            "at": datetime.now(UTC).isoformat(),
                        }
                    )

                except AgentAPIError as exc:
                    verdict = _error_verdict(exc)
                    req_id = exc.request_id or "req_err"
                    record.request_ids.append(req_id)
                    record.append_event(
                        {
                            "step": "tool_result",
                            "verdict": verdict,
                            "status_code": exc.status_code,
                            "reason_code": exc.error_code,
                            "message": exc.message,
                            "request_id": req_id,
                            "at": datetime.now(UTC).isoformat(),
                        }
                    )
                    record.status = "completed"
                    record.verdict = verdict
                    record.append_event(
                        {
                            "step": "completed",
                            "verdict": verdict,
                            "reason_code": exc.error_code,
                            "request_id": req_id,
                            "summary": f"Blocked by AgentGate: {exc.message}",
                            "at": datetime.now(UTC).isoformat(),
                        }
                    )

        except Exception:
            record.status = "failed"
            record.verdict = "ERROR"
            record.append_event(
                {
                    "step": "error",
                    "message": "The demo could not confirm the outcome.",
                    "at": datetime.now(UTC).isoformat(),
                }
            )
            record.append_event(
                {
                    "step": "completed",
                    "verdict": "ERROR",
                    "at": datetime.now(UTC).isoformat(),
                }
            )
