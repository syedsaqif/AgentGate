"""Agent scenario and execution routes."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from demo_server.agent_brain import AgentBrain, AgentRunRecord, RunManager
from demo_server.parser import PAYEES, parse_prompt
from demo_server.scenarios import get_scenario_by_id, get_scenarios

router = APIRouter()
CUSTOM_CATEGORIES = sorted({entry["category"] for entry in PAYEES.values()} | {"investment.stocks"})
TOOL_FIELDS = ("from_account", "payee_id", "amount_paise", "category")


class PolicyOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["pay_bill", "transfer"]
    payee_id: str
    amount_paise: Annotated[int, Field(strict=True, gt=0, le=9007199254740991)]
    category: str
    document: str | None = None


class AgentRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str | None = Field(default=None, max_length=32)
    prompt: str | None = Field(default=None, max_length=1000)
    attachment: str | None = Field(default=None, max_length=2000)
    agent_mode: Literal["naive", "cautious"] = "naive"
    overrides: PolicyOverrides | None = None


class AgentRunResponse(BaseModel):
    run_id: str


def _get_agent_brain(request: Request) -> AgentBrain:
    return request.app.state.agent_brain


def _get_run_manager(request: Request) -> RunManager:
    return request.app.state.run_manager


@router.get("/scenarios")
async def list_scenarios(
    kind: Annotated[Literal["normal", "attack"] | None, Query()] = None,
) -> dict[str, Any]:
    """Retrieve scenario catalogue cards filtered by kind (normal / attack)."""
    cards = get_scenarios(kind=kind)
    return {
        "items": [c.to_dict() for c in cards], "total": len(cards),
        "policy_inputs": {
            "payees": [{"id": key, **value} for key, value in PAYEES.items()],
            "categories": CUSTOM_CATEGORIES,
        },
    }


async def _observe_account(request: Request) -> dict[str, Any]:
    config = request.app.state.config
    client = request.app.state.bank_client
    gateway = request.app.state.gateway_client
    snapshot: dict[str, Any] = {}
    if not config.customer_pin:
        return snapshot
    try:
        async with asyncio.timeout(2):
            bank = await client.get_balance(config.account_id, config.customer_id)
            amount = bank.get("balance_paise")
            if type(amount) is int:
                snapshot["balance"] = amount
    except (HTTPException, TimeoutError, ValueError):
        pass
    if config.ops_password:
        try:
            async with asyncio.timeout(2):
                token = await gateway.get_ops_token()
                agents = await gateway.read_payment_evidence("/v1/ops/agents", token)
                for agent in agents.get("items", []):
                    if agent.get("agent_id") == "agt_billpal":
                        snapshot["agent_status"] = agent.get("status")
                        break
        except (HTTPException, TimeoutError, ValueError):
            pass
    return snapshot


async def _execute(
    request: Request, brain: AgentBrain, manager: RunManager, record: AgentRunRecord,
) -> None:
    async with manager.execution_lock:
        try:
            scenario = get_scenario_by_id(record.scenario_id) if record.scenario_id else None
            live = scenario is None or scenario.source == "live_supported"
            instruction = parse_prompt(record.prompt, record.attachment, record.agent_mode)
            dispatch = not instruction.is_refused and (
                record.overrides or not instruction.is_clarification_needed
            )
            before = await _observe_account(request) if live and dispatch else {}
            await brain.execute_run(record)
            if before and record.request_ids:
                after = await _observe_account(request)
                if "balance" in before and "balance" in after:
                    record.observation["balance_delta_paise"] = after["balance"] - before["balance"]
                if "agent_status" in before and "agent_status" in after:
                    record.observation["agent_status"] = after["agent_status"]
                    record.observation["agent_suspended_during_run"] = (
                        before["agent_status"] == "ACTIVE" and after["agent_status"] == "SUSPENDED"
                    )
        finally:
            record.finished = True
            for queue in record.subscribers:
                if not queue.full():
                    queue.put_nowait({})


@router.post("/agent/run", response_model=AgentRunResponse, status_code=status.HTTP_202_ACCEPTED)
async def run_agent_scenario(
    request: Request,
    payload: AgentRunRequest,
    brain: Annotated[AgentBrain, Depends(_get_agent_brain)],
    manager: Annotated[RunManager, Depends(_get_run_manager)],
) -> AgentRunResponse:
    """Launch an asynchronous agent run for a scenario or custom prompt."""
    if not payload.scenario_id and not payload.prompt and not payload.overrides:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {"code": "E_REQ_INVALID", "message": "Must provide scenario_id or prompt"}
            },
        )

    scenario = get_scenario_by_id(payload.scenario_id) if payload.scenario_id else None
    if payload.scenario_id and scenario is None:
        raise HTTPException(404, "Unknown scenario.")
    prompt = payload.prompt if payload.prompt is not None else (scenario.prompt if scenario else "")
    attachment = (
        payload.attachment if payload.attachment is not None else scenario.attachment
        if scenario else None
    )
    custom = payload.overrides
    if custom:
        document = get_scenario_by_id(custom.document) if custom.document else None
        if (
            scenario or custom.payee_id not in PAYEES or custom.category not in CUSTOM_CATEGORIES
            or (custom.document and (
                not document or document.source != "live_supported" or not document.attachment
            ))
        ):
            raise HTTPException(422, "Select a supported business-policy input.")
        prompt = f"{custom.action} {custom.payee_id}"
        attachment = document.attachment if document else None
    if scenario and scenario.source == "live_supported" and (
        prompt.strip() != scenario.prompt.strip()
        or (attachment or "") != (scenario.attachment or "")
    ):
        scenario = None

    record = manager.create_run(
        scenario_id=scenario.id if scenario else None,
        prompt=prompt,
        attachment=attachment,
        agent_mode=payload.agent_mode,
        overrides=custom.model_dump() if custom else None,
    )

    # Spawn background task
    asyncio.create_task(_execute(request, brain, manager, record))

    return AgentRunResponse(run_id=record.run_id)


@router.get("/agent/stream")
async def stream_agent_steps(
    run_id: Annotated[str, Query(min_length=1)],
    request: Request,
    manager: Annotated[RunManager, Depends(_get_run_manager)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Run-scoped SSE stream of ordered agent execution steps.

    Replays all historical events for this run immediately, so clients that
    subscribe after a fast run completes still receive the full ordered trace.
    """
    record: AgentRunRecord | None = manager.get_run(run_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "E_REQ_INVALID", "message": f"Run '{run_id}' not found"}},
        )

    async def _stream_generator() -> AsyncIterator[str]:
        cursor = (
            int(last_event_id) if last_event_id and last_event_id.isdecimal()
            and len(last_event_id) < 10 else 0
        )
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=128)
        record.subscribers.add(queue)

        try:
            while True:
                while cursor < len(record.events):
                    event = record.events[cursor]
                    if event["step"] == "completed" and not record.finished:
                        break
                    cursor += 1
                    projected = _project_step(event)
                    if event["step"] == "completed":
                        projected.update(record.observation)
                    yield f"id: {cursor}\nevent: agent_step\ndata: {json.dumps(projected)}\n\n"
                if record.finished or await request.is_disconnected():
                    return
                try:
                    await asyncio.wait_for(queue.get(), timeout=1.0)
                except TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ": keepalive\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            record.subscribers.discard(queue)

    return StreamingResponse(
        _stream_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _project_step(event: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "step", "action", "tool", "verdict", "status", "reason_code",
        "request_id", "trace_request_id", "attempt", "cached", "source", "gateway_calls",
    )
    safe = {key: event[key] for key in keys if key in event}
    if event["step"] in {"tool_call", "plan"}:
        args = event.get("args", event)
        safe["args"] = {key: args[key] for key in TOOL_FIELDS if key in args}
    if event["step"] in {"refusal", "clarification"}:
        safe["message"] = event.get("message")
    return safe
