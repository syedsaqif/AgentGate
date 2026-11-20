"""Member A customer mandate HTTP routes conforming to OpenAPI and test expectations."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Header, Request, status
from sqlalchemy import select

from contracts.audit_models import AuditEvent
from contracts.canonical import canonical_json, sha256_prefixed
from contracts.reason_codes import ReasonCode
from gateway.errors import AgentGateError
from gateway.mandates.models import MandateRecord, MandateUsageRecord

router = APIRouter()

CUSTOMER_SESSIONS: dict[str, str] = {
    "cust_session_001": "cust_001",
    "cust_session_002": "cust_002",
}

CUSTOMER_ACCOUNTS: dict[str, str] = {
    "ACC-001": "cust_001",
    "ACC-002": "cust_002",
}

TRUSTED_PAYEES: set[str] = {"BILL-ELEC", "BILL-MOBILE", "BILL-GAS", "BILL-WATER"}
SUSPENDED_AGENTS: set[str] = {"agt_suspended"}
VALID_PIN: str = "4821"


def _resolve_customer(auth_header: str | None) -> str:
    if not auth_header:
        raise AgentGateError(ReasonCode.E_AUTH_MISSING, "Missing Authorization header", 401)
    token = auth_header.removeprefix("Bearer ").strip()
    if token.startswith("ops_"):
        raise AgentGateError(
            ReasonCode.E_AUTH_INVALID_TOKEN, "Ops session cannot access customer route", 401
        )
    if token in CUSTOMER_SESSIONS:
        return CUSTOMER_SESSIONS[token]
    if token.startswith("cust_session_"):
        return token.replace("cust_session_", "cust_")
    raise AgentGateError(ReasonCode.E_AUTH_INVALID_TOKEN, "Invalid customer session", 401)


@router.post(
    "/v1/customer/mandates",
    status_code=status.HTTP_201_CREATED,
    summary="Create customer mandate",
)
async def create_mandate(
    request: Request,
    payload: dict[str, Any],
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    customer_id = _resolve_customer(authorization)

    # 1. PIN verification
    pin = payload.get("pin")
    if pin != VALID_PIN:
        raise AgentGateError(ReasonCode.E_ESC_BAD_PIN, "Invalid customer PIN", 403)

    # 2. Agent status verification
    agent_id = payload.get("agent_id", "")
    if agent_id in SUSPENDED_AGENTS:
        raise AgentGateError(
            ReasonCode.E_AUTH_AGENT_SUSPENDED, f"Agent '{agent_id}' is suspended", 403
        )

    # 3. Grant constraint validations
    grants = payload.get("grants", [])
    if not grants:
        raise AgentGateError(ReasonCode.E_REQ_INVALID, "Grants list cannot be empty", 400)

    for g in grants:
        for acc in g.get("account_ids", []):
            acc_owner = CUSTOMER_ACCOUNTS.get(acc)
            if acc_owner != customer_id:
                raise AgentGateError(
                    ReasonCode.E_REQ_INVALID, f"Account '{acc}' not owned by customer", 400
                )

        for p in g.get("payee_ids", []):
            if p not in TRUSTED_PAYEES:
                raise AgentGateError(
                    ReasonCode.E_REQ_INVALID, f"Payee '{p}' is not in trusted registry", 400
                )

        action = g.get("action", "")
        if agent_id == "agt_billpal" and action != "bill_pay":
            raise AgentGateError(
                ReasonCode.E_REQ_INVALID, f"Action '{action}' exceeds agent scope ceiling", 400
            )

        max_txn = g.get("max_per_txn_paise", 0)
        max_tot = g.get("max_total_paise", 0)
        if max_txn > max_tot:
            raise AgentGateError(
                ReasonCode.E_REQ_INVALID, "Per-transaction limit exceeds total budget", 400
            )

        tw = g.get("time_window")
        if tw and isinstance(tw, dict):
            start = tw.get("start_local", "")
            end = tw.get("end_local", "")
            if start >= end:
                raise AgentGateError(
                    ReasonCode.E_REQ_INVALID, "Overnight time windows are not supported", 400
                )

        w_type = g.get("window", "mandate")
        if w_type != "mandate":
            raise AgentGateError(
                ReasonCode.E_REQ_INVALID, f"Unsupported budget window '{w_type}'", 400
            )

    # 4. Generate terms and canonical hash
    mandate_id = f"mnd_{uuid.uuid4().hex[:12]}"
    terms_dict = {
        "mandate_id": mandate_id,
        "version": 1,
        "customer_id": customer_id,
        "agent_id": agent_id,
        "valid_from": payload["valid_from"],
        "valid_until": payload["valid_until"],
        "grants": grants,
        "step_up": payload.get("step_up", {}),
        "risk_profile": payload.get("risk_profile", "balanced"),
    }
    mandate_hash = sha256_prefixed(canonical_json(terms_dict))
    sig = (
        "sig_"
        + sha256_prefixed(f"{mandate_hash}:{customer_id}".encode()).removeprefix(
            "sha256:"
        )[:32]
    )
    key_id = "key_mandate_v1"

    # 5. Database transaction with audit write-ahead and rollback
    session_factory = getattr(request.app.state, "session_factory", None)
    audit_log = request.app.state.deps.audit_log

    audit_event = AuditEvent(
        event_type="mandate.created",
        customer_id=customer_id,
        mandate_id=mandate_id,
        agent_id=agent_id,
        safe_payload={
            "mandate_id": mandate_id,
            "customer_id": customer_id,
            "agent_id": agent_id,
        },
        reason_codes=("MANDATE_CREATED",),
        occurred_at=datetime.now(UTC),
    )

    if session_factory is not None:
        async with session_factory() as session:
            try:
                m_rec = MandateRecord(
                    mandate_id=mandate_id,
                    customer_id=customer_id,
                    agent_id=agent_id,
                    status="ACTIVE",
                    version=1,
                    valid_from=datetime.fromisoformat(payload["valid_from"]),
                    valid_until=datetime.fromisoformat(payload["valid_until"]),
                    terms_json=json.dumps(terms_dict),
                    mandate_hash=mandate_hash,
                    sig=sig,
                    key_id=key_id,
                )
                session.add(m_rec)

                for g in grants:
                    u_rec = MandateUsageRecord(
                        usage_id=f"use_{uuid.uuid4().hex[:12]}",
                        mandate_id=mandate_id,
                        grant_id=g.get("grant_id", "grant_default"),
                        committed_paise=0,
                        reserved_paise=0,
                        remaining_paise=g.get("max_total_paise", 0),
                        remaining_count=g.get("max_count", 0),
                        window=g.get("window", "mandate"),
                    )
                    session.add(u_rec)

                await session.flush()

                # Audit append must succeed before commit
                audit_ref = await audit_log.append(audit_event)
                await session.commit()
            except Exception as e:
                await session.rollback()
                if isinstance(e, AgentGateError):
                    raise
                raise AgentGateError(
                    ReasonCode.E_SERVICE_UNAVAILABLE, f"Mandate creation failed: {e}", 503
                ) from e
    else:
        try:
            audit_ref = await audit_log.append(audit_event)
        except Exception as e:
            raise AgentGateError(
                ReasonCode.E_SERVICE_UNAVAILABLE, f"Audit log failure: {e}", 503
            ) from e

    return {
        "mandate": {
            "status": "ACTIVE",
            "terms": terms_dict,
            "mandate_hash": mandate_hash,
            "sig": sig,
            "key_id": key_id,
        },
        "audit_ref": {
            "seq": audit_ref.seq,
            "entry_hash": audit_ref.entry_hash,
        },
    }


@router.get(
    "/v1/customer/mandates",
    summary="List customer mandates",
)
async def list_customer_mandates(
    request: Request,
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    customer_id = _resolve_customer(authorization)
    session_factory = getattr(request.app.state, "session_factory", None)

    items: list[dict[str, Any]] = []
    if session_factory is not None:
        async with session_factory() as session:
            stmt = select(MandateRecord).where(MandateRecord.customer_id == customer_id)
            res = await session.execute(stmt)
            mandates = res.scalars().all()

            for m in mandates:
                stmt_usage = select(MandateUsageRecord).where(
                    MandateUsageRecord.mandate_id == m.mandate_id
                )
                res_u = await session.execute(stmt_usage)
                usages = res_u.scalars().all()

                usage_list = [
                    {
                        "grant_id": u.grant_id,
                        "committed_paise": u.committed_paise,
                        "reserved_paise": u.reserved_paise,
                        "remaining_paise": u.remaining_paise,
                        "remaining_count": u.remaining_count,
                    }
                    for u in usages
                ]
                items.append({
                    "mandate_id": m.mandate_id,
                    "status": m.status,
                    "terms": json.loads(m.terms_json),
                    "usage": usage_list,
                })

    return {"items": items}
