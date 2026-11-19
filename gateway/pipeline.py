"""Member A request pipeline and decision combiner for money-out actions."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from contracts.audit_models import AuditEvent
from contracts.common import (
    ActionType,
    FinalDecision,
    OpaqueId,
    Paise,
    PolicyVerdict,
    RiskLevel,
)
from contracts.core_models import (
    ActionParams,
    ActionStatus,
    BankStatus,
    CanonicalAction,
    Intent,
    MandateStatus,
)
from contracts.identity_models import AgentStatus, AuthContext
from contracts.reason_codes import HTTP_STATUS_BY_CODE, ReasonCode
from contracts.risk_models import HistorySnapshot, RiskInput, RiskResult
from gateway.deps import Dependencies
from gateway.errors import AgentGateError, get_or_create_request_id

router = APIRouter(prefix="/v1", tags=["agent"])


class IntentRequest(BaseModel):
    """Structured intent accompanying a money action."""

    model_config = ConfigDict(extra="forbid")

    task: OpaqueId = Field(min_length=1)
    category: OpaqueId = Field(min_length=1)
    note: Annotated[str, Field(max_length=500)] | None = None


class MoneyActionRequest(BaseModel):
    """External request payload for payments and transfers per OpenAPI."""

    model_config = ConfigDict(extra="forbid")

    from_account: OpaqueId = Field(min_length=1)
    payee_id: OpaqueId = Field(min_length=1)
    amount_paise: Paise = Field(gt=0)
    currency: Literal["INR"] = "INR"
    reference: Annotated[str, Field(max_length=120)] | None = None
    intent: IntentRequest


def get_dependencies(request: Request) -> Dependencies:
    """Retrieve injected dependencies from application state."""
    deps: Dependencies | None = getattr(request.app.state, "deps", None)
    if deps is None:
        raise AgentGateError(
            code=ReasonCode.E_SERVICE_UNAVAILABLE,
            message="Application dependencies not initialized",
            status_code=503,
        )
    return deps


async def process_money_action(
    request: Request,
    action_type: ActionType,
) -> JSONResponse:
    """Execute the canonical 8-step request pipeline and decision combiner."""
    deps = get_dependencies(request)
    request_id = get_or_create_request_id(request)

    # 1. Edge: Validate Idempotency-Key header and raw request body
    idempotency_key = (
        request.headers.get("Idempotency-Key") or request.headers.get("idempotency-key")
    )
    if not idempotency_key or not idempotency_key.strip():
        raise AgentGateError(
            code=ReasonCode.E_REQ_INVALID,
            message="Header 'Idempotency-Key' is required for money actions",
            status_code=400,
        )
    idempotency_key = idempotency_key.strip()

    raw_bytes = await request.body()
    if not raw_bytes:
        raise AgentGateError(
            code=ReasonCode.E_REQ_INVALID,
            message="Request body cannot be empty",
            status_code=400,
        )

    try:
        parsed = MoneyActionRequest.model_validate_json(raw_bytes)
    except Exception as exc:
        raise AgentGateError(
            code=ReasonCode.E_REQ_INVALID,
            message=f"Invalid request body: {exc}",
            status_code=400,
        ) from exc

    # 2. Identity: Authenticate cryptographic proof and DPoP token
    if deps.identity_verifier is None:
        raise AgentGateError(
            code=ReasonCode.E_SERVICE_UNAVAILABLE,
            message="Identity verifier is unconfigured",
            status_code=503,
        )

    try:
        auth_ctx: AuthContext = await deps.identity_verifier.authenticate(
            request_bytes=raw_bytes,
            headers=dict(request.headers),
            method=request.method,
            url=str(request.url),
        )
    except AgentGateError:
        if deps.audit_log is not None:
            try:
                await deps.audit_log.append(
                    AuditEvent(
                        event_type="auth.failed",
                        occurred_at=deps.clock.now(),
                        safe_payload={"method": request.method, "url": str(request.url)},
                    )
                )
            except Exception:
                pass
        raise
    except Exception as exc:
        if deps.audit_log is not None:
            try:
                await deps.audit_log.append(
                    AuditEvent(
                        event_type="auth.failed",
                        occurred_at=deps.clock.now(),
                        safe_payload={"method": request.method, "url": str(request.url)},
                    )
                )
            except Exception:
                pass
        code_str = str(exc)
        if code_str in ReasonCode.__members__:
            reason = ReasonCode(code_str)
            st = HTTP_STATUS_BY_CODE.get(reason, 401)
            raise AgentGateError(
                code=reason, message="Authentication failed", status_code=st
            ) from exc
        raise AgentGateError(
            code=ReasonCode.E_AUTH_INVALID_TOKEN,
            message=f"Authentication failed: {exc}",
            status_code=401,
        ) from exc

    # Check agent status in AgentRegistry if configured
    if deps.agent_registry is not None:
        try:
            agent_pub = await deps.agent_registry.get(auth_ctx.agent_id)
            if agent_pub.status == AgentStatus.SUSPENDED:
                raise AgentGateError(
                    code=ReasonCode.E_AUTH_AGENT_SUSPENDED,
                    message=f"Agent '{auth_ctx.agent_id}' is suspended",
                    status_code=403,
                )
            if agent_pub.status == AgentStatus.DISABLED:
                raise AgentGateError(
                    code=ReasonCode.E_AUTH_AGENT_SUSPENDED,
                    message=f"Agent '{auth_ctx.agent_id}' is disabled",
                    status_code=403,
                )
        except AgentGateError:
            raise
        except Exception:
            pass

    # 3. Action Store: Construct canonical action from trusted identity bindings
    canonical_action = CanonicalAction(
        request_id=request_id,
        customer_id=auth_ctx.customer_id,
        agent_id=auth_ctx.agent_id,
        mandate_id=auth_ctx.mandate_id,
        mandate_hash=auth_ctx.mandate_hash,
        action_type=action_type,
        params=ActionParams(
            from_account=parsed.from_account,
            payee_id=parsed.payee_id,
            amount_paise=parsed.amount_paise,
            currency="INR",
            reference=parsed.reference,
        ),
        intent=Intent(
            task=parsed.intent.task,
            category=parsed.intent.category,
            note=parsed.intent.note,
        ),
        idempotency_key=idempotency_key,
        received_at=deps.clock.now(),
    )

    # 4. Idempotency claim and deduplication check
    exec_srv = getattr(deps, "execution_service", None)
    if exec_srv is not None:
        record, is_new = await exec_srv.prepare_action(
            method=request.method,
            path=request.url.path,
            action=canonical_action,
        )
        if not is_new:
            # Replay existing result
            if record.status == ActionStatus.EXECUTED.value:
                exec_result = await deps.execution_port.execute_stored(canonical_action.request_id)
                return JSONResponse(
                    status_code=200,
                    content=exec_result.model_dump(mode="json"),
                )
            if record.status == ActionStatus.PENDING_APPROVAL.value:
                return JSONResponse(
                    status_code=202,
                    headers={"Retry-After": "2"},
                    content={
                        "request_id": record.request_id,
                        "decision": "ESCALATE",
                        "status": "PENDING_APPROVAL",
                        "escalation_id": record.escalation_id,
                        "audit_ref": None,
                    },
                )
            if record.status == ActionStatus.EXECUTION_UNKNOWN.value:
                return JSONResponse(
                    status_code=202,
                    content={
                        "request_id": record.request_id,
                        "status": "EXECUTION_UNKNOWN",
                    },
                )
            if record.status in (ActionStatus.REJECTED.value, ActionStatus.FAILED.value):
                raise AgentGateError(
                    code=ReasonCode.E_POLICY_PAYEE_NOT_ALLOWED,
                    message="Action was previously rejected",
                    status_code=403,
                )

    # 5. Mandate Snapshot & Pure Policy Evaluation
    if deps.mandate_reader is None or deps.usage_reader is None or deps.policy_evaluator is None:
        raise AgentGateError(
            code=ReasonCode.E_SERVICE_UNAVAILABLE,
            message="Mandate policy readers are unconfigured",
            status_code=503,
        )

    snapshot = await deps.mandate_reader.get(canonical_action.mandate_id)
    if (
        snapshot.terms.customer_id != auth_ctx.customer_id
        or snapshot.terms.agent_id != auth_ctx.agent_id
    ):
        raise AgentGateError(
            code=ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH,
            message="Mandate does not match customer or agent",
            status_code=403,
        )

    status_str = str(snapshot.status).upper()
    if status_str == MandateStatus.REVOKED.value:
        raise AgentGateError(
            code=ReasonCode.E_MANDATE_REVOKED,
            message="Mandate has been revoked",
            status_code=403,
        )
    if status_str != MandateStatus.ACTIVE.value:
        raise AgentGateError(
            code=ReasonCode.E_AUTH_AGENT_SUSPENDED,
            message=f"Mandate status is {snapshot.status}",
            status_code=403,
        )

    usage_snapshots = await deps.usage_reader.get(canonical_action.mandate_id)
    policy_result = deps.policy_evaluator.evaluate(
        action=canonical_action,
        mandate=snapshot,
        usages=usage_snapshots,
        now=deps.clock.now(),
    )

    # 6. Risk Engine scoring & attempt recording
    # Section 7.1: Always record & score attempt, even on hard policy DENY
    risk_input = RiskInput(
        auth=auth_ctx,
        action=canonical_action,
        policy=policy_result,
        mandate=snapshot,
        history=HistorySnapshot(),
        now=deps.clock.now(),
        attempt_id=f"att_{canonical_action.request_id}",
    )

    if deps.risk_engine is None:
        raise AgentGateError(
            code=ReasonCode.E_SERVICE_UNAVAILABLE,
            message="Risk engine is unconfigured",
            status_code=503,
        )

    try:
        risk_result: RiskResult = deps.risk_engine.score(risk_input)
    except Exception:
        # Fallback per Section 7.1: score 50, explicit escalation, never downgrades DENY
        risk_result = RiskResult(
            score=50,
            level=RiskLevel.MEDIUM,
            recommended=FinalDecision.ESCALATE,
            signals=(),
            breaker_recommended=False,
            breaker_reasons=(),
            model_version="fallback_v1",
        )

    if deps.unit_of_work is not None:
        async with deps.unit_of_work as tx:
            await deps.risk_engine.record_attempt(risk_input, tx)

    # 7. Decision Combiner
    # Priority: Hard DENY > Risk DENY/Breaker > ESCALATE/Step-up > ALLOW
    deny_code: str | None = None
    if policy_result.verdict == PolicyVerdict.DENY:
        final_decision = FinalDecision.DENY
        deny_code = (
            policy_result.reason_codes[0]
            if policy_result.reason_codes
            else ReasonCode.E_POLICY_PAYEE_NOT_ALLOWED.value
        )
    elif (
        risk_result.recommended == FinalDecision.DENY
        or risk_result.level == RiskLevel.HIGH
        or risk_result.breaker_recommended
    ):
        final_decision = FinalDecision.DENY
        deny_code = (
            ReasonCode.E_RISK_BREAKER_OPEN.value
            if risk_result.breaker_recommended
            else ReasonCode.E_RISK_HIGH.value
        )
    elif (
        policy_result.verdict == PolicyVerdict.STEP_UP
        or risk_result.recommended == FinalDecision.ESCALATE
        or risk_result.level == RiskLevel.MEDIUM
    ):
        final_decision = FinalDecision.ESCALATE
    else:
        final_decision = FinalDecision.ALLOW

    # 8. Breaker Triggered Handling: Atomic agent suspension
    if risk_result.breaker_recommended and deps.lifecycle is not None:
        reasons = risk_result.breaker_reasons or (ReasonCode.E_RISK_BREAKER_OPEN.value,)
        await deps.lifecycle.suspend_agent(
            canonical_action.agent_id,
            reason_codes=tuple(reasons),
            reason="Circuit breaker triggered by risk engine",
        )

    # 9. Pre-dispatch Write-Ahead Audit
    if deps.audit_log is None:
        raise AgentGateError(
            code=ReasonCode.E_SERVICE_UNAVAILABLE,
            message="Audit log is unconfigured",
            status_code=503,
        )

    audit_event = AuditEvent(
        event_type=f"payment.{final_decision.value.lower()}",
        request_id=canonical_action.request_id,
        customer_id=canonical_action.customer_id,
        agent_id=canonical_action.agent_id,
        mandate_id=canonical_action.mandate_id,
        mandate_hash=canonical_action.mandate_hash,
        decision=final_decision.value,
        reason_codes=policy_result.reason_codes if final_decision == FinalDecision.DENY else (),
        risk={
            "score": risk_result.score,
            "level": str(risk_result.level),
            "recommended": str(risk_result.recommended),
        },
        safe_payload={
            "from_account": canonical_action.params.from_account,
            "amount_paise": canonical_action.params.amount_paise,
            "action_type": str(canonical_action.action_type),
        },
        occurred_at=deps.clock.now(),
    )

    try:
        audit_ref = await deps.audit_log.append(audit_event)
    except Exception as audit_exc:
        raise AgentGateError(
            code=ReasonCode.E_AUDIT_WRITE_FAILED,
            message=f"Pre-dispatch audit failed: {audit_exc}",
            status_code=503,
        ) from audit_exc

    # 10. Final Decision Branching
    if final_decision == FinalDecision.ALLOW:
        if deps.execution_port is None:
            raise AgentGateError(
                code=ReasonCode.E_SERVICE_UNAVAILABLE,
                message="Execution port is unconfigured",
                status_code=503,
            )

        exec_res = await deps.execution_port.execute_stored(canonical_action.request_id)

        # Publish event to EventBus strictly after execution
        if (
            deps.event_bus is not None
            and exec_res.bank_result
            and exec_res.bank_result.status == BankStatus.EXECUTED
        ):
            await deps.event_bus.publish(
                "bank.executed",
                {
                    "status": "EXECUTED",
                    "request_id": canonical_action.request_id,
                    "customer_id": canonical_action.customer_id,
                    "agent_id": canonical_action.agent_id,
                    "bank_operation_id": exec_res.bank_result.bank_operation_id,
                    "audit_ref": exec_res.audit_ref.model_dump(mode="json")
                    if exec_res.audit_ref
                    else None,
                },
            )

        if exec_res.status == ActionStatus.EXECUTION_UNKNOWN:
            return JSONResponse(
                status_code=202,
                content=exec_res.model_dump(mode="json"),
            )

        if exec_res.status == ActionStatus.FAILED:
            if (
                exec_res.bank_result
                and exec_res.bank_result.reason_code == ReasonCode.E_BANK_INSUFFICIENT_FUNDS.value
            ):
                raise AgentGateError(
                    code=ReasonCode.E_BANK_INSUFFICIENT_FUNDS,
                    message="Insufficient funds",
                    status_code=422,
                )
            raise AgentGateError(
                code=ReasonCode.E_BANK_UNAVAILABLE,
                message="Bank execution failed or was rejected",
                status_code=502,
            )

        return JSONResponse(
            status_code=200,
            content=exec_res.model_dump(mode="json"),
        )

    if final_decision == FinalDecision.ESCALATE:
        if deps.escalation_service is None:
            raise AgentGateError(
                code=ReasonCode.E_SERVICE_UNAVAILABLE,
                message="Escalation service is unconfigured",
                status_code=503,
            )

        if deps.unit_of_work is not None:
            async with deps.unit_of_work as tx:
                session = tx.session
                action_store = getattr(deps, "action_reader", None)
                rec = None
                if action_store is not None and hasattr(action_store, "get_record"):
                    rec = await action_store.get_record(session, canonical_action.request_id)
                    if rec:
                        rec.status = ActionStatus.PENDING_APPROVAL.value

                esc = await deps.escalation_service.create(canonical_action, risk_result, tx)
                if rec:
                    rec.escalation_id = esc.escalation_id
        else:
            esc = await deps.escalation_service.create(canonical_action, risk_result, None)

        if deps.event_bus is not None:
            await deps.event_bus.publish(
                "escalation.created",
                {
                    "request_id": canonical_action.request_id,
                    "escalation_id": esc.escalation_id,
                    "customer_id": canonical_action.customer_id,
                    "agent_id": canonical_action.agent_id,
                    "status": "PENDING_APPROVAL",
                },
            )

        return JSONResponse(
            status_code=202,
            headers={"Retry-After": "2"},
            content={
                "request_id": canonical_action.request_id,
                "decision": "ESCALATE",
                "status": "PENDING_APPROVAL",
                "escalation_id": esc.escalation_id,
                "expires_at": esc.expires_at.isoformat(),
                "audit_ref": audit_ref.model_dump(mode="json") if audit_ref else None,
            },
        )

    # final_decision == FinalDecision.DENY
    if deps.unit_of_work is not None:
        async with deps.unit_of_work as tx:
            session = tx.session
            action_store = getattr(deps, "action_reader", None)
            if action_store is not None and hasattr(action_store, "get_record"):
                rec = await action_store.get_record(session, canonical_action.request_id)
                if rec:
                    rec.status = ActionStatus.REJECTED.value

    if deps.event_bus is not None:
        await deps.event_bus.publish(
            "action.denied",
            {
                "request_id": canonical_action.request_id,
                "customer_id": canonical_action.customer_id,
                "agent_id": canonical_action.agent_id,
                "reason_code": deny_code,
                "status": "REJECTED",
            },
        )

    raise AgentGateError(
        code=deny_code or ReasonCode.E_POLICY_PAYEE_NOT_ALLOWED,
        message="Payment request denied",
        status_code=403,
    )


@router.post("/payments/bills")
async def pay_bill(request: Request) -> JSONResponse:
    """Submit a bill payment request through the policy and risk pipeline."""
    return await process_money_action(request, ActionType.BILL_PAY)


@router.post("/transfers")
async def transfer(request: Request) -> JSONResponse:
    """Submit a transfer request through the policy and risk pipeline."""
    return await process_money_action(request, ActionType.TRANSFER)
