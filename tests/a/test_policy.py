"""Member A test suite for pure synchronous mandate policy evaluation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from contracts.common import ActionType, PolicyVerdict
from contracts.core_models import (
    ActionParams,
    CanonicalAction,
    Grant,
    Intent,
    MandateSnapshot,
    MandateStatus,
    MandateTerms,
    PolicyResult,
    StepUpRules,
    TimeWindow,
    UsageSnapshot,
)
from contracts.identity_models import AuthContext
from contracts.reason_codes import ReasonCode
from gateway.demo_trace import explain_policy
from gateway.mandates.policy import PolicyEvaluator


def evaluate_with_parity(
    auth: AuthContext,
    action: CanonicalAction,
    snapshot: MandateSnapshot,
    usage: tuple[UsageSnapshot, ...],
    now: datetime,
    known_paid_payees: list[str] | None = None,
) -> PolicyResult:
    result = PolicyEvaluator().evaluate(
        auth, action, snapshot, usage, now, known_paid_payees=known_paid_payees
    )
    trace = explain_policy(action, snapshot, usage, now, known_paid_payees or (), auth=auth)
    assert (trace.verdict, trace.reason_code) == (result.verdict, result.reason_codes[0])
    triggered = [row for row in trace.checks if row.status != "pass"]
    assert [row for row in trace.checks if row.decisive] == triggered[:1]
    if triggered:
        assert result.verdict == (
            PolicyVerdict.DENY if triggered[0].status == "fail" else PolicyVerdict.STEP_UP
        )
    else:
        assert result.verdict == PolicyVerdict.ALLOW
    return result


def make_policy_fixture(
    customer_id: str = "cust_001",
    agent_id: str = "agt_billpal",
    mandate_id: str = "mnd_test_1",
    mandate_hash: str = "sha256:1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff",
    status: MandateStatus = MandateStatus.ACTIVE,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    grants: tuple[Grant, ...] | None = None,
    step_up: StepUpRules | None = None,
) -> tuple[MandateSnapshot, AuthContext]:
    """Helper creating matching MandateSnapshot and AuthContext."""
    v_from = valid_from or datetime(2026, 11, 1, 0, 0, 0, tzinfo=UTC)
    v_until = valid_until or datetime(2026, 12, 1, 0, 0, 0, tzinfo=UTC)

    if grants is None:
        grants = (
            Grant(
                grant_id="grant_bill_1",
                action=ActionType.BILL_PAY,
                account_ids=("ACC-001",),
                payee_ids=("BILL-ELEC", "BILL-MOBILE"),
                currency="INR",
                max_per_txn_paise=50000,  # ₹500
                max_total_paise=200000,  # ₹2000
                max_count=5,
                window="mandate",
            ),
        )

    terms = MandateTerms(
        mandate_id=mandate_id,
        version=1,
        customer_id=customer_id,
        agent_id=agent_id,
        valid_from=v_from,
        valid_until=v_until,
        grants=grants,
        step_up=step_up or StepUpRules(new_payee_requires_approval=False),
        risk_profile="balanced",
    )

    snapshot = MandateSnapshot(
        terms=terms,
        status=status,
        mandate_hash=mandate_hash,
        sig="sig_test_dummy",
        key_id="key_mandate_issuer_1",
    )

    auth = AuthContext(
        customer_id=customer_id,
        agent_id=agent_id,
        mandate_id=mandate_id,
        mandate_hash=mandate_hash,
        jkt="jkt_test",
        token_jti="tok_test",
        proof_jti="prf_test",
    )

    return snapshot, auth


def make_test_action(
    customer_id: str = "cust_001",
    agent_id: str = "agt_billpal",
    mandate_id: str = "mnd_test_1",
    mandate_hash: str = "sha256:1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff",
    action_type: ActionType = ActionType.BILL_PAY,
    from_account: str = "ACC-001",
    payee_id: str | None = "BILL-ELEC",
    amount_paise: int = 10000,
    currency: str = "INR",
) -> CanonicalAction:
    """Helper creating CanonicalAction DTO."""
    return CanonicalAction(
        request_id="req_test_01",
        customer_id=customer_id,
        agent_id=agent_id,
        mandate_id=mandate_id,
        mandate_hash=mandate_hash,
        action_type=action_type,
        params=ActionParams(
            from_account=from_account,
            payee_id=payee_id,
            amount_paise=amount_paise,
            currency=currency,  # type: ignore[arg-type]
        ),
        intent=Intent(task="pay_electric", category="utilities"),
        idempotency_key="idemp_test_01",
        received_at=datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC),
    )


def test_happy_path_money_out_and_read() -> None:
    snapshot, auth = make_policy_fixture()
    now = datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC)

    # 1. Money-out bill_pay
    act_bill = make_test_action(amount_paise=25000)
    res_bill = evaluate_with_parity(auth, act_bill, snapshot, (), now)
    assert res_bill.verdict == PolicyVerdict.ALLOW
    assert res_bill.reason_codes == ("ALLOW",)
    assert res_bill.matched_grant_ids == ("grant_bill_1",)

    # 2. Read balance_read
    read_grant = Grant(
        grant_id="grant_read_1",
        action=ActionType.BALANCE_READ,
        account_ids=("ACC-001",),
        max_count=10,
    )
    snap_read, auth_read = make_policy_fixture(grants=(read_grant,))
    act_read = make_test_action(
        action_type=ActionType.BALANCE_READ,
        payee_id=None,
        amount_paise=0,
    )
    res_read = evaluate_with_parity(auth_read, act_read, snap_read, (), now)
    assert res_read.verdict == PolicyVerdict.ALLOW
    assert res_read.matched_grant_ids == ("grant_read_1",)


def test_caps_exact_versus_plus_one() -> None:
    snapshot, auth = make_policy_fixture()  # max_per_txn=50000, max_total=200000, max_count=5
    now = datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC)

    # A. Per-transaction cap
    # Exact -> ALLOW
    act_exact_txn = make_test_action(amount_paise=50000)
    res_exact_txn = evaluate_with_parity(auth, act_exact_txn, snapshot, (), now)
    assert res_exact_txn.verdict == PolicyVerdict.ALLOW

    # +1 -> DENY
    act_over_txn = make_test_action(amount_paise=50001)
    res_over_txn = evaluate_with_parity(auth, act_over_txn, snapshot, (), now)
    assert res_over_txn.verdict == PolicyVerdict.DENY
    assert ReasonCode.E_REQ_INVALID.value in res_over_txn.reason_codes

    # B. Cumulative total cap
    usage_snap = UsageSnapshot(
        mandate_id="mnd_test_1",
        grant_id="grant_bill_1",
        window_key="mnd_test_1:grant_bill_1:mandate",
        committed_paise=150000,
        reserved_paise=20000,
        committed_count=2,
        reserved_count=1,
    )
    # Available = 200000 - 170000 = 30000
    # Exact -> 30000 ALLOW
    act_cum_exact = make_test_action(amount_paise=30000)
    res_cum_exact = evaluate_with_parity(auth, act_cum_exact, snapshot, (usage_snap,), now)
    assert res_cum_exact.verdict == PolicyVerdict.ALLOW

    # +1 -> 30001 DENY
    act_cum_over = make_test_action(amount_paise=30001)
    res_cum_over = evaluate_with_parity(auth, act_cum_over, snapshot, (usage_snap,), now)
    assert res_cum_over.verdict == PolicyVerdict.DENY

    # C. Count cap (max_count = 5)
    # committed=2, reserved=2, current=1 -> total=5 (exact) -> ALLOW
    usage_count_exact = UsageSnapshot(
        mandate_id="mnd_test_1",
        grant_id="grant_bill_1",
        window_key="w1",
        committed_paise=0,
        reserved_paise=0,
        committed_count=2,
        reserved_count=2,
    )
    act_bill = make_test_action(amount_paise=1000)
    res_cnt_exact = evaluate_with_parity(auth, act_bill, snapshot, (usage_count_exact,), now)
    assert res_cnt_exact.verdict == PolicyVerdict.ALLOW

    # committed=3, reserved=2, current=1 -> total=6 (over) -> DENY
    usage_count_over = UsageSnapshot(
        mandate_id="mnd_test_1",
        grant_id="grant_bill_1",
        window_key="w1",
        committed_paise=0,
        reserved_paise=0,
        committed_count=3,
        reserved_count=2,
    )
    res_cnt_over = evaluate_with_parity(auth, act_bill, snapshot, (usage_count_over,), now)
    assert res_cnt_over.verdict == PolicyVerdict.DENY


def test_amount_zero_and_invalid_currency() -> None:
    snapshot, auth = make_policy_fixture()
    now = datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC)

    # 1. Money-out with amount 0 -> DENY
    act_zero = make_test_action(amount_paise=0)
    res_zero = evaluate_with_parity(auth, act_zero, snapshot, (), now)
    assert res_zero.verdict == PolicyVerdict.DENY

    # 2. Non-INR currency -> DENY (using model_construct to bypass Pydantic model validation)
    params_usd = ActionParams.model_construct(
        from_account="ACC-001",
        payee_id="BILL-ELEC",
        amount_paise=1000,
        currency="USD",
        reference=None,
    )
    act_usd = CanonicalAction.model_construct(
        request_id="req_test_usd",
        customer_id="cust_001",
        agent_id="agt_billpal",
        mandate_id="mnd_test_1",
        mandate_hash="sha256:1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff",
        action_type=ActionType.BILL_PAY,
        params=params_usd,
        intent=Intent(task="pay_electric", category="utilities"),
        idempotency_key="idemp_test_usd",
        received_at=datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC),
    )
    res_usd = evaluate_with_parity(auth, act_usd, snapshot, (), now)
    assert res_usd.verdict == PolicyVerdict.DENY


def test_account_and_payee_allowlist_boundaries() -> None:
    snapshot, auth = make_policy_fixture()
    now = datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC)

    # 1. Unlisted account -> DENY
    act_wrong_acc = make_test_action(from_account="ACC-999")
    res_acc = evaluate_with_parity(auth, act_wrong_acc, snapshot, (), now)
    assert res_acc.verdict == PolicyVerdict.DENY
    assert ReasonCode.E_REQ_INVALID.value in res_acc.reason_codes

    # 2. Unlisted payee -> hard DENY with E_POLICY_PAYEE_NOT_ALLOWED
    act_bad_payee = make_test_action(payee_id="PAYEE-CRYPTO")
    res_payee = evaluate_with_parity(auth, act_bad_payee, snapshot, (), now)
    assert res_payee.verdict == PolicyVerdict.DENY
    assert ReasonCode.E_POLICY_PAYEE_NOT_ALLOWED.value in res_payee.reason_codes

    # 3. Action not in mandate grants (transfer requested, but mandate only grants bill_pay)
    act_transfer = make_test_action(action_type=ActionType.TRANSFER)
    res_transfer = evaluate_with_parity(auth, act_transfer, snapshot, (), now)
    assert res_transfer.verdict == PolicyVerdict.DENY


def test_validity_window_exact_boundaries() -> None:
    t_start = datetime(2026, 11, 10, 0, 0, 0, tzinfo=UTC)
    t_end = datetime(2026, 11, 20, 0, 0, 0, tzinfo=UTC)
    snapshot, auth = make_policy_fixture(valid_from=t_start, valid_until=t_end)
    act = make_test_action()

    # 1. Future mandate: now < valid_from -> DENY
    res_future = evaluate_with_parity(
        auth, act, snapshot, (), datetime(2026, 11, 9, 23, 59, 59, tzinfo=UTC)
    )
    assert res_future.verdict == PolicyVerdict.DENY

    # 2. Exact start: now == valid_from -> ALLOW (start inclusive)
    res_start = evaluate_with_parity(auth, act, snapshot, (), t_start)
    assert res_start.verdict == PolicyVerdict.ALLOW

    # 3. Inside window -> ALLOW
    res_inside = evaluate_with_parity(
        auth, act, snapshot, (), datetime(2026, 11, 15, 12, 0, 0, tzinfo=UTC)
    )
    assert res_inside.verdict == PolicyVerdict.ALLOW

    # 4. Exact expiry: now == valid_until -> DENY with E_AUTH_EXPIRED (end exclusive)
    res_end = evaluate_with_parity(auth, act, snapshot, (), t_end)
    assert res_end.verdict == PolicyVerdict.DENY
    assert ReasonCode.E_AUTH_EXPIRED.value in res_end.reason_codes

    # 5. After expiry: now > valid_until -> DENY with E_AUTH_EXPIRED
    res_after = evaluate_with_parity(
        auth, act, snapshot, (), datetime(2026, 11, 20, 0, 0, 1, tzinfo=UTC)
    )
    assert res_after.verdict == PolicyVerdict.DENY
    assert ReasonCode.E_AUTH_EXPIRED.value in res_after.reason_codes


def test_mandate_status_verdicts() -> None:
    now = datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC)
    act = make_test_action()

    # 1. REVOKED -> E_MANDATE_REVOKED
    s_rev, a_rev = make_policy_fixture(status=MandateStatus.REVOKED)
    res_rev = evaluate_with_parity(a_rev, act, s_rev, (), now)
    assert res_rev.verdict == PolicyVerdict.DENY
    assert ReasonCode.E_MANDATE_REVOKED.value in res_rev.reason_codes

    # 2. SUSPENDED -> E_AUTH_AGENT_SUSPENDED
    s_susp, a_susp = make_policy_fixture(status=MandateStatus.SUSPENDED)
    res_susp = evaluate_with_parity(a_susp, act, s_susp, (), now)
    assert res_susp.verdict == PolicyVerdict.DENY
    assert ReasonCode.E_AUTH_AGENT_SUSPENDED.value in res_susp.reason_codes

    # 3. EXPIRED -> E_AUTH_EXPIRED
    s_exp, a_exp = make_policy_fixture(status=MandateStatus.EXPIRED)
    res_exp = evaluate_with_parity(a_exp, act, s_exp, (), now)
    assert res_exp.verdict == PolicyVerdict.DENY
    assert ReasonCode.E_AUTH_EXPIRED.value in res_exp.reason_codes


def test_asia_kolkata_time_window_evaluation() -> None:
    # Weekdays: Mon-Fri (1..5), 09:00 - 17:00 IST
    grant_tw = Grant(
        grant_id="grant_tw",
        action=ActionType.BILL_PAY,
        account_ids=("ACC-001",),
        payee_ids=("BILL-ELEC",),
        max_per_txn_paise=100000,
        max_total_paise=500000,
        max_count=10,
        time_window=TimeWindow(
            timezone="Asia/Kolkata",
            weekdays=(1, 2, 3, 4, 5),
            start_local="09:00",
            end_local="17:00",
        ),
    )
    snapshot, auth = make_policy_fixture(grants=(grant_tw,))
    act = make_test_action()

    # Tuesday 2026-11-17:
    # 09:00 IST is 03:30 UTC
    # 17:00 IST is 11:30 UTC

    # A. Exact start_local: 09:00:00 IST (03:30:00 UTC) -> ALLOW
    t_start = datetime(2026, 11, 17, 3, 30, 0, tzinfo=UTC)
    res_start = evaluate_with_parity(auth, act, snapshot, (), t_start)
    assert res_start.verdict == PolicyVerdict.ALLOW

    # B. Inside: 12:00:00 IST (06:30:00 UTC) -> ALLOW
    t_mid = datetime(2026, 11, 17, 6, 30, 0, tzinfo=UTC)
    res_mid = evaluate_with_parity(auth, act, snapshot, (), t_mid)
    assert res_mid.verdict == PolicyVerdict.ALLOW

    # C. Exact end_local: 17:00:00 IST (11:30:00 UTC) -> DENY (end exclusive)
    t_end = datetime(2026, 11, 17, 11, 30, 0, tzinfo=UTC)
    res_end = evaluate_with_parity(auth, act, snapshot, (), t_end)
    assert res_end.verdict == PolicyVerdict.DENY

    # D. Wrong weekday: Sunday 2026-11-22 at 12:00 IST (06:30 UTC) -> DENY
    t_sunday = datetime(2026, 11, 22, 6, 30, 0, tzinfo=UTC)
    res_sun = evaluate_with_parity(auth, act, snapshot, (), t_sunday)
    assert res_sun.verdict == PolicyVerdict.DENY

    full_day = TimeWindow(
        timezone="Asia/Kolkata", weekdays=(2,), start_local="00:00", end_local="23:59"
    )
    full_day_grant = grant_tw.model_copy(update={"time_window": full_day})
    snapshot, auth = make_policy_fixture(grants=(full_day_grant,))
    # 18:29:59 UTC is 23:59:59 IST on Tuesday and Wednesday respectively.
    tuesday = datetime(2026, 11, 17, 18, 29, 59, tzinfo=UTC)
    wednesday = datetime(2026, 11, 18, 18, 29, 59, tzinfo=UTC)
    assert evaluate_with_parity(auth, act, snapshot, (), tuesday).verdict == PolicyVerdict.ALLOW
    assert evaluate_with_parity(auth, act, snapshot, (), wednesday).verdict == PolicyVerdict.DENY


def test_overlapping_grants_intersection_rule() -> None:
    now = datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC)

    # Grant 1: ACC-001, [BILL-ELEC, BILL-WATER], max_per_txn=50000, max_total=200000
    # Grant 2: ACC-001, [BILL-ELEC],              max_per_txn=30000, max_total=100000
    g1 = Grant(
        grant_id="g1",
        action=ActionType.BILL_PAY,
        account_ids=("ACC-001",),
        payee_ids=("BILL-ELEC", "BILL-WATER"),
        max_per_txn_paise=50000,
        max_total_paise=200000,
        max_count=5,
    )
    g2 = Grant(
        grant_id="g2",
        action=ActionType.BILL_PAY,
        account_ids=("ACC-001",),
        payee_ids=("BILL-ELEC",),  # Stricter payee list
        max_per_txn_paise=30000,  # Stricter cap
        max_total_paise=100000,
        max_count=3,
    )
    snapshot, auth = make_policy_fixture(grants=(g1, g2))

    # 1. Payee allowed by g1 but NOT g2 -> DENY
    act_water = make_test_action(payee_id="BILL-WATER", amount_paise=10000)
    res_water = evaluate_with_parity(auth, act_water, snapshot, (), now)
    assert res_water.verdict == PolicyVerdict.DENY
    assert ReasonCode.E_POLICY_PAYEE_NOT_ALLOWED.value in res_water.reason_codes

    # 2. Amount allowed by g1 (<= 50000) but exceeds g2 (35000 > 30000) -> DENY
    act_over_g2 = make_test_action(payee_id="BILL-ELEC", amount_paise=35000)
    res_over = evaluate_with_parity(auth, act_over_g2, snapshot, (), now)
    assert res_over.verdict == PolicyVerdict.DENY

    # 3. Satisfies both (amount=25000, payee=BILL-ELEC) -> ALLOW and returns both grant IDs
    act_both = make_test_action(payee_id="BILL-ELEC", amount_paise=25000)
    res_both = evaluate_with_parity(auth, act_both, snapshot, (), now)
    assert res_both.verdict == PolicyVerdict.ALLOW
    assert set(res_both.matched_grant_ids) == {"g1", "g2"}


def test_soft_step_up_triggers() -> None:
    now = datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC)

    step_up = StepUpRules(
        amount_above_paise=20000,
        new_payee_requires_approval=True,
        always_ask_actions=(ActionType.TRANSFER,),
    )
    grant_bill = Grant(
        grant_id="g_bill",
        action=ActionType.BILL_PAY,
        account_ids=("ACC-001",),
        payee_ids=("BILL-ELEC", "BILL-MOBILE"),
        max_per_txn_paise=50000,
        max_total_paise=200000,
        max_count=5,
    )
    grant_transfer = Grant(
        grant_id="g_trf",
        action=ActionType.TRANSFER,
        account_ids=("ACC-001",),
        payee_ids=("PAYEE-X",),
        max_per_txn_paise=50000,
        max_total_paise=200000,
        max_count=5,
    )
    snapshot, auth = make_policy_fixture(grants=(grant_bill, grant_transfer), step_up=step_up)

    # 1. amount_above_paise:
    # 20000 exact -> ALLOW (already known payee)
    act_exact = make_test_action(amount_paise=20000, payee_id="BILL-ELEC")
    res_exact = evaluate_with_parity(
        auth, act_exact, snapshot, (), now, known_paid_payees=["BILL-ELEC"]
    )
    assert res_exact.verdict == PolicyVerdict.ALLOW

    # 20001 (+1) -> STEP_UP
    act_over = make_test_action(amount_paise=20001, payee_id="BILL-ELEC")
    res_over = evaluate_with_parity(
        auth, act_over, snapshot, (), now, known_paid_payees=["BILL-ELEC"]
    )
    assert res_over.verdict == PolicyVerdict.STEP_UP
    assert "amount_above_threshold" in res_over.step_up_reasons

    # 2. new_payee_requires_approval:
    # Allow-listed payee "BILL-MOBILE", but not in known_paid_payees -> STEP_UP
    act_new_payee = make_test_action(amount_paise=10000, payee_id="BILL-MOBILE")
    res_new_p = evaluate_with_parity(
        auth, act_new_payee, snapshot, (), now, known_paid_payees=["BILL-ELEC"]
    )
    assert res_new_p.verdict == PolicyVerdict.STEP_UP
    assert "new_payee" in res_new_p.step_up_reasons

    # 3. always_ask_actions:
    # Transfer is always ask -> STEP_UP even if payee is known and amount is low
    act_trf = make_test_action(
        action_type=ActionType.TRANSFER,
        payee_id="PAYEE-X",
        amount_paise=5000,
    )
    res_trf = evaluate_with_parity(auth, act_trf, snapshot, (), now, known_paid_payees=["PAYEE-X"])
    assert res_trf.verdict == PolicyVerdict.STEP_UP
    assert "always_ask_action" in res_trf.step_up_reasons


def test_hard_deny_beats_soft_step_up() -> None:
    """Hard DENY ALWAYS wins over STEP_UP; if both apply, verdict MUST be DENY."""
    now = datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC)

    step_up = StepUpRules(
        amount_above_paise=10000,
        new_payee_requires_approval=True,
    )
    snapshot, auth = make_policy_fixture(step_up=step_up)

    # Action has amount 30000 (> 10000 step_up threshold)
    # BUT payee "PAYEE-UNLISTED" is not allow-listed in the mandate
    act_conflict = make_test_action(
        amount_paise=30000,
        payee_id="PAYEE-UNLISTED",
    )
    res_conflict = evaluate_with_parity(auth, act_conflict, snapshot, (), now)

    # Must be hard DENY, not STEP_UP!
    assert res_conflict.verdict == PolicyVerdict.DENY
    assert ReasonCode.E_POLICY_PAYEE_NOT_ALLOWED.value in res_conflict.reason_codes
    assert len(res_conflict.step_up_reasons) == 0


def demo_mandate(first_payee: bool = False) -> tuple[MandateSnapshot, AuthContext]:
    grant = Grant(
        grant_id="demo",
        action=ActionType.BILL_PAY,
        account_ids=("ACC-001",),
        payee_ids=("BILL-ELEC",),
        max_per_txn_paise=500000,
        max_total_paise=1000000,
        max_count=5,
    )
    return make_policy_fixture(
        grants=(grant,),
        step_up=StepUpRules(amount_above_paise=400000, new_payee_requires_approval=first_payee),
    )


def test_policy_contract_fixtures() -> None:
    path = Path(__file__).resolve().parents[2] / "contracts/fixtures/a/policy_cases.json"
    mandate, auth = demo_mandate()
    for case in json.loads(path.read_text(encoding="utf8"))["cases"]:
        if case["name"] == "dependency_failure":
            continue  # Audit availability is checked downstream of pure policy.
        action = make_test_action(amount_paise=case["amount_paise"], payee_id=case["payee_id"])
        assert (
            evaluate_with_parity(auth, action, mandate, (), action.received_at).verdict
            == case["expected"]
        )


@pytest.mark.parametrize(
    ("amount", "first_payee", "verdict", "deciding"),
    [
        (400000, False, "ALLOW", None),
        (400000, True, "STEP_UP", "new_payee"),
        (400001, True, "STEP_UP", "amount_above_threshold"),
        (500000, False, "STEP_UP", "amount_above_threshold"),
        (500001, False, "DENY", "per_txn_cap"),
    ],
)
def test_demo_thresholds(amount, first_payee, verdict, deciding):
    mandate, auth = demo_mandate(first_payee)
    action = make_test_action(amount_paise=amount)
    assert evaluate_with_parity(auth, action, mandate, (), action.received_at).verdict == verdict
    trace = explain_policy(action, mandate, (), action.received_at)
    assert [row.check for row in trace.checks if row.decisive] == ([deciding] if deciding else [])


def test_multiple_failed_constraints_keep_first_reason() -> None:
    mandate, auth = demo_mandate()
    action = make_test_action(amount_paise=500001, payee_id="unlisted")
    prior = UsageSnapshot(
        mandate_id=mandate.terms.mandate_id,
        grant_id="demo",
        window_key="w",
        committed_paise=400000,
        reserved_paise=100000,
        committed_count=4,
        reserved_count=1,
    )
    evaluate_with_parity(auth, action, mandate, (prior,), action.received_at)
    trace = explain_policy(action, mandate, (prior,), action.received_at)
    failed = [row for row in trace.checks if row.status == "fail"]
    assert trace.reason_code == "E_POLICY_PAYEE_NOT_ALLOWED"
    assert [(row.check, row.decisive) for row in failed] == [
        ("payee_allowlist", True),
        ("per_txn_cap", False),
        ("total_cap", False),
        ("count_cap", False),
    ]
    assert failed[1].detail == {"requested_paise": 500001, "limit_paise": 500000}
    assert failed[2].detail == {"projected_paise": 1000001, "limit_paise": 1000000}
    assert failed[3].detail == {"projected_count": 6, "limit": 5}
