"""Member A pure mandate policy evaluator."""

from __future__ import annotations

from collections.abc import Callable, Collection
from datetime import datetime, time, timedelta, timezone
from typing import Any

try:
    from zoneinfo import ZoneInfo

    KOLKATA_TZ = ZoneInfo("Asia/Kolkata")
except Exception:
    KOLKATA_TZ = timezone(timedelta(hours=5, minutes=30), name="Asia/Kolkata")

from contracts.common import ActionType, PolicyVerdict
from contracts.core_models import (
    CanonicalAction,
    MandateSnapshot,
    MandateStatus,
    PolicyResult,
    UsageSnapshot,
)
from contracts.identity_models import AuthContext
from contracts.reason_codes import ReasonCode


def _get_action_str(action: Any) -> str:
    """Normalize enum or string action representation."""
    if hasattr(action, "value"):
        return str(action.value)
    return str(action)


class PolicyEvaluator:
    """Pure, deterministic policy evaluator for mandate constraints and step-up rules.

    Conforms to contracts.core_ports.PolicyEvaluator.
    ZERO database queries, ZERO network calls, ZERO usage mutations.
    """

    def evaluate(
        self,
        *args: Any,
        auth: AuthContext | None = None,
        action: CanonicalAction | None = None,
        snapshot: MandateSnapshot | None = None,
        mandate: MandateSnapshot | None = None,
        usage: tuple[UsageSnapshot, ...] | list[UsageSnapshot] | None = None,
        now: datetime | None = None,
        known_paid_payees: set[str] | frozenset[str] | list[str] | None = None,
        **kwargs: Any,
    ) -> PolicyResult:
        """Evaluate an action against a mandate snapshot and usage counters.

        Supports both calling conventions:
        - evaluate(auth, action, snapshot, usage, now, *, known_paid_payees=...)
        - evaluate(action, mandate, usage, now, *, auth=..., known_paid_payees=...)
        """
        # Resolve positional arguments
        resolved_auth: AuthContext | None = auth
        resolved_action: CanonicalAction | None = action
        resolved_snapshot: MandateSnapshot | None = snapshot or mandate
        resolved_usage: tuple[UsageSnapshot, ...] = ()
        resolved_now: datetime | None = now

        if len(args) == 5:
            # (auth, action, snapshot, usage, now)
            resolved_auth = args[0]
            resolved_action = args[1]
            resolved_snapshot = args[2]
            resolved_usage = tuple(args[3])
            resolved_now = args[4]
        elif len(args) == 4:
            # (action, snapshot, usage, now)
            resolved_action = args[0]
            resolved_snapshot = args[1]
            resolved_usage = tuple(args[2])
            resolved_now = args[3]
        elif usage is not None:
            resolved_usage = tuple(usage)

        if resolved_action is None or resolved_snapshot is None or resolved_now is None:
            raise ValueError("action, snapshot/mandate, and now are required for policy evaluation")

        return evaluate_policy(
            resolved_action,
            resolved_snapshot,
            resolved_usage,
            resolved_now,
            known_paid_payees or (),
            resolved_auth,
            kwargs.get('_check_observer'),
        )


CheckObserver = Callable[[str, bool, bool, bool, int | None, dict[str, int]], None]


def evaluate_policy(
    action: CanonicalAction,
    mandate: MandateSnapshot,
    usage: tuple[UsageSnapshot, ...],
    now: datetime,
    known_paid_payees: Collection[str],
    auth: AuthContext | None,
    observe: CheckObserver | None = None,
) -> PolicyResult:
    """Apply the mandate in policy order, optionally reporting the same comparisons."""
    terms = mandate.terms
    reasons: list[str] = []
    grant_index: int | None = None
    soft = False

    def check(
        name: str, passed: bool, reason: str = ReasonCode.E_REQ_INVALID.value, **detail: int
    ) -> None:
        if observe is not None:
            observe(name, passed, soft, not passed and not reasons, grant_index, detail)
        if not passed and reason not in reasons:
            reasons.append(reason)

    status = str(mandate.status).upper()
    status_reason = {
        MandateStatus.REVOKED.value: ReasonCode.E_MANDATE_REVOKED,
        MandateStatus.SUSPENDED.value: ReasonCode.E_AUTH_AGENT_SUSPENDED,
        MandateStatus.EXPIRED.value: ReasonCode.E_AUTH_EXPIRED,
    }.get(status, ReasonCode.E_REQ_INVALID)
    check("mandate_status", status == MandateStatus.ACTIVE.value, status_reason.value)
    binding = (terms.customer_id, terms.agent_id, terms.mandate_id, mandate.mandate_hash)
    for name, subject in (("auth_binding", auth), ("action_binding", action)):
        if subject is not None:
            check(
                name,
                (subject.customer_id, subject.agent_id, subject.mandate_id, subject.mandate_hash)
                == binding,
                ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH.value,
            )

    check(
        "validity",
        terms.valid_from <= now < terms.valid_until,
        ReasonCode.E_AUTH_EXPIRED.value
        if now >= terms.valid_until
        else ReasonCode.E_REQ_INVALID.value,
    )
    kind = _get_action_str(action.action_type)
    grants = [g for g in terms.grants if _get_action_str(g.action) == kind]
    check("action_grant", bool(grants))
    money = kind in (ActionType.BILL_PAY.value, ActionType.TRANSFER.value)
    read = kind in (ActionType.BALANCE_READ.value, ActionType.TRANSACTIONS_READ.value)
    params = action.params
    amount = params.amount_paise
    if grants and money:
        check("payment_parameters", params.currency == "INR" and amount > 0)
    elif grants and read:
        check("read_amount", amount == 0)

    counters = {u.grant_id: u for u in usage if hasattr(u, "grant_id")}
    for index, grant in enumerate(grants):
        grant_index = index
        check("account_allowlist", params.from_account in grant.account_ids)
        if grant.time_window:
            window = grant.time_window
            local = now.astimezone(KOLKATA_TZ)
            allowed = local.isoweekday() in window.weekdays
            if allowed and (window.start_local, window.end_local) not in (
                ("00:00", "23:59"),
                ("00:00", "23:59:59"),
            ):
                current = time(local.hour, local.minute, local.second)
                start = time(*map(int, window.start_local.split(":")))
                end = time(*map(int, window.end_local.split(":")))
                allowed = start <= current < end
            check("time_window", allowed)

        prior = counters.get(grant.grant_id)
        if money:
            check("grant_currency", grant.currency == "INR")
            check(
                "payee_allowlist",
                bool(params.payee_id and params.payee_id in grant.payee_ids),
                ReasonCode.E_POLICY_PAYEE_NOT_ALLOWED.value,
            )
            if grant.max_per_txn_paise is not None:
                check(
                    "per_txn_cap",
                    amount <= grant.max_per_txn_paise,
                    requested_paise=amount,
                    limit_paise=grant.max_per_txn_paise,
                )
            if grant.max_total_paise is not None:
                total = amount + (prior.committed_paise + prior.reserved_paise if prior else 0)
                check(
                    "total_cap",
                    total <= grant.max_total_paise,
                    projected_paise=total,
                    limit_paise=grant.max_total_paise,
                )
        if (money or read) and grant.max_count is not None:
            count = 1 + (prior.committed_count + prior.reserved_count if prior else 0)
            check(
                "count_cap", count <= grant.max_count, projected_count=count, limit=grant.max_count
            )

    grant_index = None
    if reasons:
        verdict = PolicyVerdict.DENY
    else:
        soft = True
        rules = terms.step_up
        if money and rules.amount_above_paise is not None:
            check(
                "amount_above_threshold",
                amount <= rules.amount_above_paise,
                "amount_above_threshold",
                requested_paise=amount,
                threshold_paise=rules.amount_above_paise,
            )
        if money and rules.new_payee_requires_approval and params.payee_id:
            check("new_payee", params.payee_id in known_paid_payees, "new_payee")
        if rules.always_ask_actions:
            check(
                "always_ask_action",
                kind not in map(_get_action_str, rules.always_ask_actions),
                "always_ask_action",
            )
        verdict = PolicyVerdict.STEP_UP if reasons else PolicyVerdict.ALLOW

    return PolicyResult(
        verdict=verdict,
        reason_codes=tuple(reasons) or ("ALLOW",),
        matched_grant_ids=tuple(g.grant_id for g in grants),
        step_up_reasons=tuple(reasons) if soft else (),
        usage_snapshot=usage,
    )
