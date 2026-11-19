"""Member C trusted summary builder for human approval workflows."""

from __future__ import annotations

from typing import Any

from contracts.core_models import AccountCard, CanonicalAction, MandateSnapshot, MandateTerms, PayeeCard
from contracts.risk_models import RiskResult


def build_trusted_summary(
    action: CanonicalAction,
    payee: PayeeCard | None = None,
    account: AccountCard | None = None,
    mandate: MandateSnapshot | MandateTerms | None = None,
    assessment: RiskResult | None = None,
) -> dict[str, Any]:
    """Build trusted summary dictionary for customer approval.

    Ground rules:
    - Account label is masked.
    - Payee details come from the trusted registry, not the agent's payload.
    - Free-text note is segregated under 'agent_note', explicitly marked unverified.
    - Currency amounts are formatted in INR paise and display representation.
    """
    account_id = action.params.from_account
    masked_account = (
        account.masked_label
        if account and account.masked_label
        else f"**{account_id[-4:]}"
        if len(account_id) >= 4
        else f"**{account_id}"
    )

    payee_summary = None
    if action.params.payee_id:
        payee_summary = {
            "payee_id": action.params.payee_id,
            "display_name": payee.display_name if payee else action.params.payee_id,
            "category": payee.category if payee else "unknown",
            "verified": payee.verified if payee else False,
        }

    terms = mandate.terms if isinstance(mandate, MandateSnapshot) else mandate
    mandate_summary = None
    if terms:
        mandate_summary = {
            "mandate_id": terms.mandate_id,
            "risk_profile": terms.risk_profile,
        }

    risk_summary = None
    if assessment:
        risk_summary = {
            "score": assessment.score,
            "level": str(assessment.level),
            "reasons": [s.message for s in assessment.signals[:3]],
            "reason_codes": [s.reason_code for s in assessment.signals[:3]],
        }

    # Isolated agent free-text note (explicitly unverified, segregated from trusted fields)
    raw_note = action.intent.note or ""
    agent_note = {
        "text": raw_note,
        "unverified": True,
        "label": "Unverified Agent Note",
        "warning": "This note was supplied by the agent and has not been verified.",
    }

    return {
        "action_type": str(action.action_type),
        "from_account": {
            "account_id": account_id,
            "masked_label": masked_account,
        },
        "payee": payee_summary,
        "amount": {
            "amount_paise": action.params.amount_paise,
            "amount_inr": f"₹{action.params.amount_paise / 100:,.2f}",
            "currency": action.params.currency,
        },
        "reference": action.params.reference,
        "intent": {
            "task": action.intent.task,
            "category": action.intent.category,
        },
        "mandate": mandate_summary,
        "risk": risk_summary,
        "agent_note": agent_note,
    }


__all__ = ["build_trusted_summary"]
