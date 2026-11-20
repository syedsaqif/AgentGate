"""Member C escalation HTTP routes conforming to contracts/http/c.yaml."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request, status
from pydantic import BaseModel, ConfigDict

from contracts.audit_models import AuditEvent
from contracts.audit_ports import AuditLog
from contracts.common import HumanRole, Tx
from contracts.core_ports import ExecutionPort, Lifecycle
from contracts.escalation_models import Escalation, EscalationDecision, EscalationState
from contracts.identity_models import AuthContext
from contracts.identity_ports import HumanAuth
from contracts.reason_codes import HTTP_STATUS_BY_CODE, ReasonCode
from gateway.errors import AgentGateError
from gateway.escalation.service import PersistentEscalationService


class DecisionRequest(BaseModel):
    """Customer decision payload strictly forbidding replacement action parameters."""

    model_config = ConfigDict(extra="forbid")

    decision: EscalationDecision
    pin: str | None = None
    suspend_agent: bool = False


class EscalationStatusResponse(BaseModel):
    """Agent read-only view with status/result only (no detailed risk diagnostics)."""

    escalation_id: str
    request_id: str
    state: str
    expires_at: str
    result: dict[str, Any] | None = None


class EscalationListResponse(BaseModel):
    """Customer paginated escalation list envelope."""

    items: list[dict[str, Any]]
    next_cursor: str | None = None


def make_error_response(
    code: ReasonCode | str, message: str, request_id: str | None = None
) -> dict[str, Any]:
    """Standard error envelope matching Section 6.1."""
    req_id = request_id or f"req_err_{uuid.uuid4().hex[:12]}"
    code_val = code.value if hasattr(code, "value") else str(code)
    return {
        "error": {
            "code": code_val,
            "message": message,
            "request_id": req_id,
        }
    }


def _extract_bearer_token(auth_header: str | None) -> str:
    if not auth_header:
        raise AgentGateError(
            code=ReasonCode.E_AUTH_MISSING,
            message="Missing Authorization header",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    parts = auth_header.split()
    if len(parts) == 2 and parts[0].lower() in ("bearer", "dpop"):
        return parts[1]
    return auth_header


async def authorize_escalation_agent(request: Request) -> AuthContext:
    from gateway.pipeline import authenticate_agent, get_dependencies

    return await authenticate_agent(request, await request.body(), get_dependencies(request))


def create_escalation_router(
    service: PersistentEscalationService,
    human_auth: HumanAuth | None = None,
    lifecycle: Lifecycle | None = None,
    execution_port: ExecutionPort | None = None,
    audit_log: AuditLog | None = None,
    unit_of_work: Any = None,
) -> APIRouter:
    """Create FastAPI APIRouter for Member C escalation endpoints."""
    router = APIRouter()

    @router.get(
        "/v1/escalations/{id}",
        response_model=EscalationStatusResponse,
        summary="Agent read-only escalation status lookup",
    )
    async def get_escalation_agent(
        id: str,
        auth: Annotated[AuthContext, Depends(authorize_escalation_agent)],
    ) -> EscalationStatusResponse:
        """Agent read-only status query with exact ownership binding."""
        try:
            esc = await service.get_owned(id, auth.agent_id)
            if (
                esc.agent_id != auth.agent_id
                or esc.customer_id != auth.customer_id
                or esc.mandate_id != auth.mandate_id
                or esc.action.mandate_hash != auth.mandate_hash
            ):
                raise KeyError(id)
        except KeyError:
            # Wrong owner returns exact same 404 to avoid enumeration
            raise AgentGateError(
                code=ReasonCode.E_REQ_INVALID,
                message=f"Escalation {id} not found",
                status_code=status.HTTP_404_NOT_FOUND,
            ) from None

        res_dict = esc.result.model_dump(mode="json") if esc.result else None
        return EscalationStatusResponse(
            escalation_id=str(esc.escalation_id),
            request_id=str(esc.request_id),
            state=esc.state.value if hasattr(esc.state, "value") else str(esc.state),
            expires_at=esc.expires_at.isoformat(),
            result=res_dict,
        )

    @router.get(
        "/v1/customer/escalations",
        response_model=EscalationListResponse,
        summary="Customer approval inbox",
    )
    async def list_customer_escalations(
        status_filter: EscalationState | None = Query(None, alias="status"),  # noqa: B008
        limit: int = Query(50, ge=1, le=100),  # noqa: B008
        cursor: str | None = Query(None),  # noqa: B008
        authorization: str | None = Header(None),
    ) -> EscalationListResponse:
        """List own customer escalations, optionally filtered by status."""
        token = _extract_bearer_token(authorization)
        if human_auth is None:
            raise AgentGateError(
                code=ReasonCode.E_SERVICE_UNAVAILABLE,
                message="Human auth service unavailable",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            ctx = await human_auth.authenticate(token, HumanRole.CUSTOMER)
        except Exception as e:
            if hasattr(e, "code") and isinstance(e.code, ReasonCode):
                code_str = e.code.value
                http_status = getattr(e, "http_status", status.HTTP_401_UNAUTHORIZED)
            else:
                code_str = str(e)
                http_status = (
                    HTTP_STATUS_BY_CODE.get(ReasonCode(code_str), status.HTTP_401_UNAUTHORIZED)
                    if code_str in ReasonCode.__members__
                    else status.HTTP_401_UNAUTHORIZED
                )
            raise AgentGateError(
                code=code_str,
                message="Authentication failed",
                status_code=http_status,
            ) from e

        items, next_cursor = await service.list_for_customer(
            customer_id=ctx.subject_id,
            status=status_filter,
            limit=limit,
            cursor=cursor,
        )

        serialized_items = [item.model_dump(mode="json") for item in items]
        return EscalationListResponse(items=serialized_items, next_cursor=next_cursor)

    @router.post(
        "/v1/customer/escalations/{id}/decision",
        summary="Customer decision on escalation",
    )
    async def submit_decision(
        id: str,
        body: DecisionRequest,
        authorization: str | None = Header(None),
    ) -> dict[str, Any]:
        """Customer decision endpoint approving or rejecting a pending escalation."""
        token = _extract_bearer_token(authorization)
        if human_auth is None:
            raise AgentGateError(
                code=ReasonCode.E_SERVICE_UNAVAILABLE,
                message="Human auth service unavailable",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            ctx = await human_auth.authenticate(token, HumanRole.CUSTOMER)
        except Exception as e:
            if hasattr(e, "code") and isinstance(e.code, ReasonCode):
                code_str = e.code.value
                http_status = getattr(e, "http_status", status.HTTP_401_UNAUTHORIZED)
            else:
                code_str = str(e)
                http_status = (
                    HTTP_STATUS_BY_CODE.get(ReasonCode(code_str), status.HTTP_401_UNAUTHORIZED)
                    if code_str in ReasonCode.__members__
                    else status.HTTP_401_UNAUTHORIZED
                )
            raise AgentGateError(
                code=code_str,
                message="Authentication failed",
                status_code=http_status,
            ) from e

        customer_id = ctx.subject_id

        # Verify record exists and is owned by this customer
        try:
            esc = await service.get_owned(id, customer_id)
        except KeyError:
            raise AgentGateError(
                code=ReasonCode.E_REQ_INVALID,
                message=f"Escalation {id} not found",
                status_code=status.HTTP_404_NOT_FOUND,
            ) from None

        # PIN handling: required for APPROVE
        if body.decision == EscalationDecision.APPROVE:
            if not body.pin:
                raise AgentGateError(
                    code=ReasonCode.E_ESC_BAD_PIN,
                    message="Customer PIN required for approval",
                    status_code=status.HTTP_403_FORBIDDEN,
                )
            try:
                await human_auth.verify_pin(customer_id, body.pin)
            except Exception:
                # Wrong PIN: escalation stays PENDING, audited, 403 E_ESC_BAD_PIN
                if audit_log is not None:
                    event = AuditEvent(
                        event_type="auth.pin_failed",
                        request_id=esc.request_id,
                        customer_id=customer_id,
                        agent_id=esc.agent_id,
                        mandate_id=esc.mandate_id,
                        mandate_hash=esc.action.mandate_hash,
                        decision="DENY",
                        reason_codes=(ReasonCode.E_ESC_BAD_PIN.value,),
                        safe_payload={"escalation_id": id},
                        occurred_at=service.clock.now(),
                    )
                    await audit_log.append(event)
                raise AgentGateError(
                    code=ReasonCode.E_ESC_BAD_PIN,
                    message="Invalid customer PIN; attempt recorded and rate limited",
                    status_code=status.HTTP_403_FORBIDDEN,
                ) from None

        try:
            async def decide(tx: Tx | None) -> Escalation:
                return await service.decide(id, body.decision, customer_id, tx)

            if body.decision == EscalationDecision.REJECT and body.suspend_agent:
                if lifecycle is None:
                    raise AgentGateError(
                        code=ReasonCode.E_SERVICE_UNAVAILABLE,
                        message="Delegation lifecycle is unavailable",
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    )
                updated_esc = await lifecycle.suspend_customer_delegation(
                    customer_id, esc.agent_id, (ReasonCode.E_ESC_REJECTED.value,),
                    before_suspend=decide,
                )
                assert updated_esc is not None
            else:
                updated_esc = await decide(None)
        except KeyError:
            raise AgentGateError(
                code=ReasonCode.E_REQ_INVALID,
                message=f"Escalation {id} not found",
                status_code=status.HTTP_404_NOT_FOUND,
            ) from None
        except ValueError as e:
            code_str = str(e)
            http_status = (
                HTTP_STATUS_BY_CODE.get(ReasonCode(code_str), status.HTTP_409_CONFLICT)
                if code_str in ReasonCode.__members__
                else status.HTTP_409_CONFLICT
            )
            raise AgentGateError(
                code=code_str,
                message=f"Decision rejected: {code_str}",
                status_code=http_status,
            ) from e

        # If approved and execution port available, hand off execution
        if (
            body.decision == EscalationDecision.APPROVE
            and execution_port is not None
        ):
            try:
                exec_res = await service.execute_approved(
                    escalation_id=id,
                    tx=None,
                    execution_port=execution_port,
                )
            except ValueError as e:
                code_str = str(e)
                http_status = (
                    HTTP_STATUS_BY_CODE.get(ReasonCode(code_str), status.HTTP_409_CONFLICT)
                    if code_str in ReasonCode.__members__
                    else status.HTTP_409_CONFLICT
                )
                raise AgentGateError(
                    code=code_str,
                    message=f"Execution failed: {code_str}",
                    status_code=http_status,
                ) from e

            updated_esc = await service.get_owned(id, customer_id)
            return {
                "escalation_id": id,
                "request_id": str(esc.request_id),
                "decision": body.decision.value,
                "state": str(updated_esc.state),
                "result": exec_res.model_dump(mode="json"),
            }

        return {
            "escalation_id": id,
            "request_id": str(esc.request_id),
            "decision": body.decision.value,
            "state": str(updated_esc.state),
        }

    return router


__all__ = ["DecisionRequest", "create_escalation_router"]
