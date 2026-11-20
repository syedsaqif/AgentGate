"""Member A request pipeline and decision combiner for money-out actions."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from contracts.audit_models import AuditEvent, SignedRequestEvidence
from contracts.canonical import action_hash, sha256_base64url
from contracts.common import (
    ActionType,
    FinalDecision,
    HumanRole,
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
from contracts.identity_models import AgentPublicIdentity, AgentStatus, AuthContext
from contracts.reason_codes import HTTP_STATUS_BY_CODE, ReasonCode
from contracts.risk_models import HistorySnapshot, RiskInput, RiskResult
from gateway.audit.evidence import build_request_evidence_payload
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


async def authenticate_agent(
    request: Request, raw_bytes: bytes, deps: Dependencies
) -> AuthContext:
    """Authenticate agent DPoP proof and token with write-ahead failure auditing."""
    if deps.identity_verifier is None:
        raise AgentGateError(
            code=ReasonCode.E_SERVICE_UNAVAILABLE,
            message="Identity verifier is unconfigured",
            status_code=503,
        )

    try:
        return await deps.identity_verifier.authenticate(
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
        if hasattr(exc, "code") and isinstance(exc.code, ReasonCode):
            st = getattr(exc, "http_status", 401)
            raise AgentGateError(
                code=exc.code, message=str(exc), status_code=st
            ) from exc
        code_str = str(exc)
        if code_str in ReasonCode.__members__:
            reason = ReasonCode(code_str)
            st = HTTP_STATUS_BY_CODE.get(reason, 401)
            raise AgentGateError(
                code=reason, message="Authentication failed", status_code=st
            ) from exc
        raise AgentGateError(
            code=ReasonCode.E_AUTH_PROOF_INVALID,
            message=f"Agent identity authentication failed: {exc}",
            status_code=401,
        ) from exc


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
    auth_ctx = await authenticate_agent(request, raw_bytes, deps)

    # Check agent status in AgentRegistry if configured
    agent_pub: AgentPublicIdentity | None = None
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
                exec_result = await deps.execution_port.execute_stored(record.request_id)
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

    # 5. History snapshot & Pure Policy Evaluation
    history_snap = HistorySnapshot()
    history_store = getattr(deps.risk_engine, "history_store", None)
    if history_store is not None and hasattr(history_store, "get_history_snapshot"):
        history_snap = history_store.get_history_snapshot(
            customer_id=canonical_action.customer_id,
            agent_id=canonical_action.agent_id,
            now=deps.clock.now(),
        )

    usage_snapshots = await deps.usage_reader.get(canonical_action.mandate_id)
    policy_result = deps.policy_evaluator.evaluate(
        action=canonical_action,
        mandate=snapshot,
        usages=usage_snapshots,
        now=deps.clock.now(),
        known_paid_payees=history_snap.previously_paid_payees,
    )

    # 6. Risk Engine scoring & attempt recording
    # Section 7.1: Always record & score attempt, even on hard policy DENY
    risk_input = RiskInput(
        auth=auth_ctx,
        action=canonical_action,
        policy=policy_result,
        mandate=snapshot,
        history=history_snap,
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

    evidence_payload: dict[str, Any] = {}
    if agent_pub is None and deps.agent_registry is not None:
        try:
            agent_pub = await deps.agent_registry.get(canonical_action.agent_id)
        except Exception:
            pass

    if agent_pub is not None and snapshot is not None:
        dpop_header = request.headers.get("DPoP") or request.headers.get("dpop") or ""
        signed_req = SignedRequestEvidence(
            proof_jws=dpop_header,
            proof_jti=auth_ctx.proof_jti or "",
            body_sha256=sha256_base64url(raw_bytes),
            action_hash=action_hash(canonical_action),
        )
        evidence_payload = build_request_evidence_payload(
            mandate=snapshot,
            agent=agent_pub,
            action=canonical_action,
            signed_request=signed_req,
        )

    audit_event = AuditEvent(
        event_type="policy.decision",
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
            **evidence_payload,
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

        try:
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
        except ValueError as v_err:
            if ReasonCode.E_ESC_LIMIT_REACHED.value in str(v_err):
                raise AgentGateError(
                    code=ReasonCode.E_ESC_LIMIT_REACHED,
                    message="Pending escalation limit reached for mandate",
                    status_code=429,
                ) from v_err
            raise AgentGateError(
                code=ReasonCode.E_REQ_INVALID,
                message=str(v_err),
                status_code=422,
            ) from v_err

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


@router.get("/accounts/{id}/balance")
async def get_balance(id: str, request: Request) -> JSONResponse:
    """Agent read of account balance scoped to active mandate."""
    account_id = id
    deps: Dependencies = request.app.state.deps
    raw_bytes = await request.body()
    auth = await authenticate_agent(request, raw_bytes, deps)

    if deps.mandate_reader is None or deps.policy_evaluator is None or deps.usage_reader is None:
        raise AgentGateError(
            code=ReasonCode.E_SERVICE_UNAVAILABLE,
            message="Mandate services not configured",
            status_code=503,
        )

    # Fetch and validate mandate
    mandate_snapshot = await deps.mandate_reader.get(auth.mandate_id)
    if str(mandate_snapshot.status) != str(MandateStatus.ACTIVE):
        raise AgentGateError(
            code=ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH,
            message="Mandate is not active",
            status_code=403,
        )
    if mandate_snapshot.terms.agent_id != auth.agent_id:
        raise AgentGateError(
            code=ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH,
            message="Agent does not match mandate",
            status_code=403,
        )
    if mandate_snapshot.terms.customer_id != auth.customer_id:
        raise AgentGateError(
            code=ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH,
            message="Customer does not match mandate",
            status_code=403,
        )

    request_id = get_or_create_request_id(request)
    now = deps.clock.now()

    canonical_action = CanonicalAction(
        request_id=request_id,
        customer_id=auth.customer_id,
        agent_id=auth.agent_id,
        mandate_id=auth.mandate_id,
        mandate_hash=auth.mandate_hash,
        action_type=ActionType.BALANCE_READ,
        params=ActionParams(
            from_account=account_id,
            amount_paise=0,
            currency="INR",
        ),
        intent=Intent(
            task="balance_inquiry",
            category="account.read",
        ),
        idempotency_key=request_id,
        received_at=now,
    )

    # Evaluate policy
    usage = await deps.usage_reader.get(auth.mandate_id)
    policy_result = deps.policy_evaluator.evaluate(canonical_action, mandate_snapshot, usage, now)

    if policy_result.verdict == PolicyVerdict.DENY:
        deny_code = (
            policy_result.reason_codes[0]
            if policy_result.reason_codes
            else ReasonCode.E_REQ_INVALID.value
        )
        raise AgentGateError(
            code=deny_code,
            message=f"Policy denied balance read: {deny_code}",
            status_code=403,
        )

    # Score risk & record attempt
    if deps.risk_engine is not None:
        risk_input = RiskInput(
            auth=auth,
            action=canonical_action,
            policy=policy_result,
            mandate=mandate_snapshot,
            history=HistorySnapshot(),
            now=now,
            attempt_id=f"att_{request_id}",
        )
        risk_result = deps.risk_engine.score(risk_input)
        if deps.unit_of_work is not None:
            async with deps.unit_of_work as tx:
                await deps.risk_engine.record_attempt(risk_input, tx)
        if risk_result.breaker_recommended or risk_result.level == RiskLevel.HIGH:
            code = (
                ReasonCode.E_RISK_BREAKER_OPEN
                if risk_result.breaker_recommended
                else ReasonCode.E_RISK_HIGH
            )
            raise AgentGateError(
                code=code,
                message="Balance read blocked by risk engine",
                status_code=403,
            )

    # Write-ahead audit
    audit_ref = None
    if deps.audit_log is not None:
        audit_event = AuditEvent(
            event_type="action.balance_read",
            request_id=request_id,
            customer_id=auth.customer_id,
            agent_id=auth.agent_id,
            mandate_id=auth.mandate_id,
            decision="ALLOW",
            occurred_at=now,
            safe_payload={"account_id": account_id},
        )
        audit_ref = await deps.audit_log.append(audit_event)

    # Call BankClient
    if deps.bank_client is None:
        raise AgentGateError(
            code=ReasonCode.E_BANK_UNAVAILABLE,
            message="Bank client not configured",
            status_code=502,
        )

    if hasattr(deps.bank_client, "get_balance"):
        bank_res = await deps.bank_client.get_balance(account_id, auth.customer_id)
    elif hasattr(deps.bank_client, "balance"):
        bank_res = await deps.bank_client.balance(account_id, auth.customer_id)
    else:
        raise AgentGateError(
            code=ReasonCode.E_BANK_UNAVAILABLE,
            message="Bank client does not support get_balance",
            status_code=502,
        )

    balance_paise = (
        bank_res.get("balance_paise")
        if isinstance(bank_res, dict)
        else getattr(bank_res, "balance_paise", 0)
    )

    return JSONResponse(
        status_code=200,
        content={
            "account_id": account_id,
            "customer_id": auth.customer_id,
            "balance_paise": balance_paise,
            "currency": "INR",
            "audit_ref": audit_ref.model_dump(mode="json") if audit_ref else None,
        },
    )


@router.get("/accounts/{id}/transactions")
async def get_transactions(
    id: str,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> JSONResponse:
    """Agent read of account transaction history scoped to active mandate."""
    account_id = id
    deps: Dependencies = request.app.state.deps
    raw_bytes = await request.body()
    auth = await authenticate_agent(request, raw_bytes, deps)

    if deps.mandate_reader is None or deps.policy_evaluator is None or deps.usage_reader is None:
        raise AgentGateError(
            code=ReasonCode.E_SERVICE_UNAVAILABLE,
            message="Mandate services not configured",
            status_code=503,
        )

    # Fetch and validate mandate
    mandate_snapshot = await deps.mandate_reader.get(auth.mandate_id)
    if str(mandate_snapshot.status) != str(MandateStatus.ACTIVE):
        raise AgentGateError(
            code=ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH,
            message="Mandate is not active",
            status_code=403,
        )
    if mandate_snapshot.terms.agent_id != auth.agent_id:
        raise AgentGateError(
            code=ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH,
            message="Agent does not match mandate",
            status_code=403,
        )
    if mandate_snapshot.terms.customer_id != auth.customer_id:
        raise AgentGateError(
            code=ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH,
            message="Customer does not match mandate",
            status_code=403,
        )

    request_id = get_or_create_request_id(request)
    now = deps.clock.now()

    canonical_action = CanonicalAction(
        request_id=request_id,
        customer_id=auth.customer_id,
        agent_id=auth.agent_id,
        mandate_id=auth.mandate_id,
        mandate_hash=auth.mandate_hash,
        action_type=ActionType.TRANSACTIONS_READ,
        params=ActionParams(
            from_account=account_id,
            amount_paise=0,
            currency="INR",
        ),
        intent=Intent(
            task="transactions_inquiry",
            category="account.read",
        ),
        idempotency_key=request_id,
        received_at=now,
    )

    # Evaluate policy
    usage = await deps.usage_reader.get(auth.mandate_id)
    policy_result = deps.policy_evaluator.evaluate(canonical_action, mandate_snapshot, usage, now)

    if policy_result.verdict == PolicyVerdict.DENY:
        deny_code = (
            policy_result.reason_codes[0]
            if policy_result.reason_codes
            else ReasonCode.E_REQ_INVALID.value
        )
        raise AgentGateError(
            code=deny_code,
            message=f"Policy denied transactions read: {deny_code}",
            status_code=403,
        )

    # Score risk & record attempt
    if deps.risk_engine is not None:
        risk_input = RiskInput(
            auth=auth,
            action=canonical_action,
            policy=policy_result,
            mandate=mandate_snapshot,
            history=HistorySnapshot(),
            now=now,
            attempt_id=f"att_{request_id}",
        )
        risk_result = deps.risk_engine.score(risk_input)
        if deps.unit_of_work is not None:
            async with deps.unit_of_work as tx:
                await deps.risk_engine.record_attempt(risk_input, tx)
        if risk_result.breaker_recommended or risk_result.level == RiskLevel.HIGH:
            code = (
                ReasonCode.E_RISK_BREAKER_OPEN
                if risk_result.breaker_recommended
                else ReasonCode.E_RISK_HIGH
            )
            raise AgentGateError(
                code=code,
                message="Transactions read blocked by risk engine",
                status_code=403,
            )

    # Write-ahead audit
    audit_ref = None
    if deps.audit_log is not None:
        audit_event = AuditEvent(
            event_type="action.transactions_read",
            request_id=request_id,
            customer_id=auth.customer_id,
            agent_id=auth.agent_id,
            mandate_id=auth.mandate_id,
            decision="ALLOW",
            occurred_at=now,
            safe_payload={"account_id": account_id, "limit": limit},
        )
        audit_ref = await deps.audit_log.append(audit_event)

    # Call BankClient
    if deps.bank_client is None:
        raise AgentGateError(
            code=ReasonCode.E_BANK_UNAVAILABLE,
            message="Bank client not configured",
            status_code=502,
        )

    if hasattr(deps.bank_client, "get_transactions"):
        bank_res = await deps.bank_client.get_transactions(
            account_id, auth.customer_id, limit=limit, cursor=cursor
        )
    elif hasattr(deps.bank_client, "transactions"):
        bank_res = await deps.bank_client.transactions(
            account_id, auth.customer_id, limit=limit, cursor=cursor
        )
    else:
        raise AgentGateError(
            code=ReasonCode.E_BANK_UNAVAILABLE,
            message="Bank client does not support get_transactions",
            status_code=502,
        )

    items = (
        bank_res.get("items", [])
        if isinstance(bank_res, dict)
        else getattr(bank_res, "items", ())
    )
    next_cursor = (
        bank_res.get("next_cursor")
        if isinstance(bank_res, dict)
        else getattr(bank_res, "next_cursor", None)
    )

    return JSONResponse(
        status_code=200,
        content={
            "items": items,
            "next_cursor": next_cursor,
            "audit_ref": audit_ref.model_dump(mode="json") if audit_ref else None,
        },
    )


@router.get("/actions/{request_id}")
async def get_action_status(request_id: str, request: Request) -> JSONResponse:
    """Read-only action status lookup for safe retry and uncertain recovery.

    Verifies caller customer/agent binding and returns 404 for unowned actions.
    Never executes a bank call or mutates budget.
    """
    deps: Dependencies = request.app.state.deps

    caller_customer_id: str | None = None
    caller_agent_id: str | None = None

    auth_hdr = (
        request.headers.get("Authorization")
        or request.headers.get("authorization")
        or ""
    )

    # 1. Check for human customer session if Bearer or X-Session-Token is present
    if (
        auth_hdr.startswith("Bearer ")
        or "x-session-token" in request.headers
        or "X-Session-Token" in request.headers
    ):
        if deps.human_auth is not None:
            token = (
                request.headers.get("X-Session-Token")
                or request.headers.get("x-session-token")
                or auth_hdr[7:].strip()
            )
            human = await deps.human_auth.authenticate(
                token, required_role=HumanRole.CUSTOMER
            )
            caller_customer_id = human.subject_id

    # 2. Otherwise check for agent proof / DPoP
    elif (
        auth_hdr.startswith("DPoP ")
        or "dpop" in request.headers
        or "DPoP" in request.headers
    ):
        raw_bytes = await request.body()
        auth = await authenticate_agent(request, raw_bytes, deps)
        caller_customer_id = auth.customer_id
        caller_agent_id = auth.agent_id

    if caller_customer_id is None:
        raise AgentGateError(
            code=ReasonCode.E_AUTH_MISSING,
            message="Authentication required to lookup action status",
            status_code=401,
        )

    session_factory = getattr(request.app.state, "session_factory", None)
    action_store = getattr(deps, "action_reader", None)
    if (
        not session_factory
        or not action_store
        or not hasattr(action_store, "get_record")
    ):
        raise AgentGateError(
            code=ReasonCode.E_REQ_INVALID,
            message=f"Action '{request_id}' not found",
            status_code=404,
        )

    async with session_factory() as session:
        rec = await action_store.get_record(session, request_id)

    if not rec or rec.customer_id != caller_customer_id:
        raise AgentGateError(
            code=ReasonCode.E_REQ_INVALID,
            message=f"Action '{request_id}' not found",
            status_code=404,
        )

    if caller_agent_id is not None and rec.agent_id != caller_agent_id:
        raise AgentGateError(
            code=ReasonCode.E_REQ_INVALID,
            message=f"Action '{request_id}' not found",
            status_code=404,
        )

    bank_result = None
    if rec.bank_operation_id:
        bank_result = {
            "status": "EXECUTED" if rec.status == "EXECUTED" else rec.status,
            "bank_operation_id": rec.bank_operation_id,
            "bank_idempotency_key": rec.bank_idempotency_key,
        }

    audit_ref = None
    if rec.audit_seq and rec.audit_hash:
        audit_ref = {
            "seq": rec.audit_seq,
            "entry_hash": rec.audit_hash,
        }

    return JSONResponse(
        status_code=200,
        content={
            "request_id": rec.request_id,
            "status": rec.status,
            "customer_id": rec.customer_id,
            "agent_id": rec.agent_id,
            "action_type": rec.action_type,
            "escalation_id": rec.escalation_id,
            "bank_result": bank_result,
            "audit_ref": audit_ref,
        },
    )
