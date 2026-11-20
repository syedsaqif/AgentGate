"""Member A pure mandate policy evaluator."""

from __future__ import annotations

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
    Grant,
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

        terms = resolved_snapshot.terms
        deny_reasons: list[str] = []

        # -------------------------------------------------------------
        # 1. Hard DENY: Mandate status
        # -------------------------------------------------------------
        status_str = str(resolved_snapshot.status).upper()
        if status_str == MandateStatus.REVOKED.value:
            deny_reasons.append(ReasonCode.E_MANDATE_REVOKED.value)
        elif status_str == MandateStatus.SUSPENDED.value:
            deny_reasons.append(ReasonCode.E_AUTH_AGENT_SUSPENDED.value)
        elif status_str == MandateStatus.EXPIRED.value:
            deny_reasons.append(ReasonCode.E_AUTH_EXPIRED.value)
        elif status_str != MandateStatus.ACTIVE.value:
            deny_reasons.append(ReasonCode.E_REQ_INVALID.value)

        # -------------------------------------------------------------
        # 2. Hard DENY: Ownership & Binding verification
        # -------------------------------------------------------------
        if resolved_auth is not None:
            if (
                resolved_auth.customer_id != terms.customer_id
                or resolved_auth.agent_id != terms.agent_id
                or resolved_auth.mandate_id != terms.mandate_id
                or resolved_auth.mandate_hash != resolved_snapshot.mandate_hash
            ):
                deny_reasons.append(ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH.value)

        if (
            resolved_action.customer_id != terms.customer_id
            or resolved_action.agent_id != terms.agent_id
            or resolved_action.mandate_id != terms.mandate_id
            or resolved_action.mandate_hash != resolved_snapshot.mandate_hash
        ):
            if ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH.value not in deny_reasons:
                deny_reasons.append(ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH.value)

        # -------------------------------------------------------------
        # 3. Hard DENY: Validity window (valid_from <= now < valid_until)
        # -------------------------------------------------------------
        if resolved_now < terms.valid_from:
            deny_reasons.append(ReasonCode.E_REQ_INVALID.value)
        elif resolved_now >= terms.valid_until:
            deny_reasons.append(ReasonCode.E_AUTH_EXPIRED.value)

        # -------------------------------------------------------------
        # 4. Action allow-list & Overlapping Grants (Intersection Rule)
        # -------------------------------------------------------------
        action_type_str = _get_action_str(resolved_action.action_type)
        matching_grants: list[Grant] = [
            g for g in terms.grants if _get_action_str(g.action) == action_type_str
        ]

        if not matching_grants:
            deny_reasons.append(ReasonCode.E_REQ_INVALID.value)
            return PolicyResult(
                verdict=PolicyVerdict.DENY,
                reason_codes=tuple(dict.fromkeys(deny_reasons)),
                matched_grant_ids=(),
                step_up_reasons=(),
                usage_snapshot=resolved_usage,
            )

        matched_grant_ids = tuple(g.grant_id for g in matching_grants)

        # Map existing usage by grant_id
        usage_by_grant: dict[str, UsageSnapshot] = {
            u.grant_id: u for u in resolved_usage if hasattr(u, "grant_id")
        }

        is_money_out = action_type_str in (
            ActionType.BILL_PAY.value,
            ActionType.TRANSFER.value,
        )
        is_read = action_type_str in (
            ActionType.BALANCE_READ.value,
            ActionType.TRANSACTIONS_READ.value,
        )

        amount_paise = resolved_action.params.amount_paise
        currency = resolved_action.params.currency
        from_acc = resolved_action.params.from_account
        payee_id = resolved_action.params.payee_id

        # Basic money-out parameter validation
        if is_money_out:
            if currency != "INR" or amount_paise <= 0:
                deny_reasons.append(ReasonCode.E_REQ_INVALID.value)
        elif is_read and amount_paise != 0:
            deny_reasons.append(ReasonCode.E_REQ_INVALID.value)

        # Enforce intersection across ALL matching grants
        # (every matching grant must permit the action)
        for grant in matching_grants:
            # Account allow-list
            if from_acc not in grant.account_ids:
                deny_reasons.append(ReasonCode.E_REQ_INVALID.value)

            # TimeWindow check (in Asia/Kolkata)
            if grant.time_window:
                now_kolkata = resolved_now.astimezone(KOLKATA_TZ)
                # ISO weekday: Monday=1, Sunday=7
                if now_kolkata.isoweekday() not in grant.time_window.weekdays:
                    deny_reasons.append(ReasonCode.E_REQ_INVALID.value)
                elif not (
                    grant.time_window.start_local == "00:00"
                    and grant.time_window.end_local in ("23:59", "23:59:59")
                ):
                    cur_t = time(now_kolkata.hour, now_kolkata.minute, now_kolkata.second)
                    start_h, start_m = map(int, grant.time_window.start_local.split(":"))
                    end_h, end_m = map(int, grant.time_window.end_local.split(":"))
                    start_t = time(start_h, start_m, 0)
                    end_t = time(end_h, end_m, 0)
                    # start inclusive, end exclusive
                    if cur_t < start_t or cur_t >= end_t:
                        deny_reasons.append(ReasonCode.E_REQ_INVALID.value)

            # Usage counters for this grant
            u_snap = usage_by_grant.get(grant.grant_id)
            committed_paise = u_snap.committed_paise if u_snap else 0
            reserved_paise = u_snap.reserved_paise if u_snap else 0
            committed_count = u_snap.committed_count if u_snap else 0
            reserved_count = u_snap.reserved_count if u_snap else 0

            if is_money_out:
                if grant.currency != "INR":
                    deny_reasons.append(ReasonCode.E_REQ_INVALID.value)

                # Payee allow-list check: unlisted payee is a hard DENY
                if not payee_id or payee_id not in grant.payee_ids:
                    deny_reasons.append(ReasonCode.E_POLICY_PAYEE_NOT_ALLOWED.value)

                # Per-transaction cap
                if grant.max_per_txn_paise is not None and amount_paise > grant.max_per_txn_paise:
                    deny_reasons.append(ReasonCode.E_REQ_INVALID.value)

                # Cumulative total cap
                if grant.max_total_paise is not None:
                    if committed_paise + reserved_paise + amount_paise > grant.max_total_paise:
                        deny_reasons.append(ReasonCode.E_REQ_INVALID.value)

                # Count cap
                if grant.max_count is not None:
                    if committed_count + reserved_count + 1 > grant.max_count:
                        deny_reasons.append(ReasonCode.E_REQ_INVALID.value)

            elif is_read:
                # Count cap for reads if specified
                if grant.max_count is not None:
                    if committed_count + reserved_count + 1 > grant.max_count:
                        deny_reasons.append(ReasonCode.E_REQ_INVALID.value)

        # If any hard DENY reason was triggered, return DENY immediately
        # (Hard DENY ALWAYS beats STEP_UP)
        if deny_reasons:
            return PolicyResult(
                verdict=PolicyVerdict.DENY,
                reason_codes=tuple(dict.fromkeys(deny_reasons)),
                matched_grant_ids=matched_grant_ids,
                step_up_reasons=(),
                usage_snapshot=resolved_usage,
            )

        # -------------------------------------------------------------
        # 5. Soft STEP_UP Rules (Only if NO hard DENY)
        # -------------------------------------------------------------
        step_up_reasons: list[str] = []
        step_up = terms.step_up

        if is_money_out:
            # 5a. Amount above threshold
            if (
                step_up.amount_above_paise is not None
                and amount_paise > step_up.amount_above_paise
            ):
                step_up_reasons.append("amount_above_threshold")

            # 5b. New payee approval (for allow-listed payees never paid before)
            if step_up.new_payee_requires_approval and payee_id:
                paid_set = set(known_paid_payees) if known_paid_payees else set()
                if payee_id not in paid_set:
                    step_up_reasons.append("new_payee")

        # 5c. Always ask actions
        always_ask = [_get_action_str(a) for a in step_up.always_ask_actions]
        if action_type_str in always_ask:
            step_up_reasons.append("always_ask_action")

        if step_up_reasons:
            return PolicyResult(
                verdict=PolicyVerdict.STEP_UP,
                reason_codes=tuple(dict.fromkeys(step_up_reasons)),
                matched_grant_ids=matched_grant_ids,
                step_up_reasons=tuple(dict.fromkeys(step_up_reasons)),
                usage_snapshot=resolved_usage,
            )

        # -------------------------------------------------------------
        # 6. All checks passed: ALLOW
        # -------------------------------------------------------------
        return PolicyResult(
            verdict=PolicyVerdict.ALLOW,
            reason_codes=("ALLOW",),
            matched_grant_ids=matched_grant_ids,
            step_up_reasons=(),
            usage_snapshot=resolved_usage,
        )
