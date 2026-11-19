"""Scenario S3: High-concurrency race under strict budget ceiling.

Verifies the four mandatory dimensions under parallel load:
- Mandate lifetime budget: 400,000 paise.
- Per-transaction cap: 200,000 paise.
- Five concurrent requests of 150,000 paise each with unique idempotency keys.
- Exactly two execute (consuming 300,000 paise).
- Exactly three fail with E_BANK_INSUFFICIENT_FUNDS.
- Repetition: Proven across 100 randomized iterations.
- 4 dimensions:
  1. HTTP responses: 2 successes, 3 budget denials per run.
  2. Bank ledger: Decrements by exactly 300,000 paise (12,500,000 -> 12,200,000).
  3. Mandate usage: Exactly 300,000 committed, 100,000 remaining.
  4. Audit trail: Exactly 2 bank.executed events recorded per run.
"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime
from typing import Any

from contracts.common import ActionType
from contracts.core_models import ActionParams, CanonicalAction, Intent
from simulator.client_port import FixtureScenarioClient, ScenarioClient, ScenarioResult
from simulator.personas import (
    ACCOUNT_INITIAL_BALANCE_PAISE,
    ACCOUNT_PRIMARY,
    AGENT_BILLPAL,
    CUSTOMER_ID_PRIMARY,
    CUSTOMER_PIN_PRIMARY,
    PAYEE_ELECTRICITY,
)

S3_MANDATE_ID = "mnd_s3_concurrency"
S3_TOTAL_BUDGET_PAISE = 400_000
S3_PER_TXN_CAP_PAISE = 200_000
S3_REQUEST_AMOUNT_PAISE = 150_000
S3_CONCURRENT_REQUESTS = 5


async def _setup_s3_mandate(client: ScenarioClient) -> None:
    """Configure client with S3 specific mandate constraints."""
    if isinstance(client, FixtureScenarioClient):
        client.mandate_usages[S3_MANDATE_ID] = {
            "committed_paise": 0,
            "lifetime_budget": S3_TOTAL_BUDGET_PAISE,
            "remaining_paise": S3_TOTAL_BUDGET_PAISE,
        }
        client.mandate_caps[S3_MANDATE_ID] = S3_PER_TXN_CAP_PAISE
        client.mandate_payees[S3_MANDATE_ID] = {PAYEE_ELECTRICITY.payee_id}
        client.mandate_actions[S3_MANDATE_ID] = {"bill_pay", "ActionType.BILL_PAY"}


async def _run_single_s3_iteration(
    client: ScenarioClient,
    run_idx: int,
    rng: random.Random,
    now: datetime,
) -> tuple[bool, bool, bool, bool, dict[str, Any]]:
    """Execute one 5-request race and return verification flags for all 4 dimensions."""
    await client.reset_state()
    await _setup_s3_mandate(client)

    cust_res = await client.customer_login(CUSTOMER_ID_PRIMARY, CUSTOMER_PIN_PRIMARY)
    session_token = cust_res.get("session_token", f"sess_{CUSTOMER_ID_PRIMARY}")

    tok_res = await client.request_token(AGENT_BILLPAL.agent_id, S3_MANDATE_ID)
    token = tok_res.get("access_token")

    # Build 5 concurrent requests with unique IDs and keys
    actions: list[CanonicalAction] = []
    for i in range(S3_CONCURRENT_REQUESTS):
        req_id = f"req_s3_r{run_idx:03d}_{i}"
        act = CanonicalAction(
            request_id=req_id,
            customer_id=CUSTOMER_ID_PRIMARY,
            agent_id=AGENT_BILLPAL.agent_id,
            mandate_id=S3_MANDATE_ID,
            mandate_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
            action_type=ActionType.BILL_PAY,
            params=ActionParams(
                from_account=ACCOUNT_PRIMARY.account_id,
                payee_id=PAYEE_ELECTRICITY.payee_id,
                amount_paise=S3_REQUEST_AMOUNT_PAISE,
                currency="INR",
                reference=f"Concurrent Bill {run_idx}-{i}",
            ),
            intent=Intent(
                task="Pay electricity bill",
                category="utilities",
                note=f"Parallel load test item {i}",
            ),
            idempotency_key=f"idemp_s3_r{run_idx:03d}_{i}",
            received_at=now,
        )
        actions.append(act)

    # Randomize dispatch order
    rng.shuffle(actions)

    # Dispatch all 5 concurrently
    tasks = [client.send_action(action, token=token) for action in actions]
    responses = await asyncio.gather(*tasks)

    # Dimension 1: HTTP Responses (exactly 2 EXECUTED, 3 INSUFFICIENT_FUNDS)
    executed = [r for r in responses if r.get("status") == "EXECUTED"]
    budget_denials = [
        r for r in responses
        if r.get("error", {}).get("code") == "E_BANK_INSUFFICIENT_FUNDS"
    ]
    http_verified = len(executed) == 2 and len(budget_denials) == 3

    # Dimension 2: Bank Ledger (decremented by exactly 2 * 150,000 = 300,000 paise)
    bal_res = await client.get_balance(ACCOUNT_PRIMARY.account_id)
    post_balance = bal_res.get("balance_paise", 0)
    expected_balance = ACCOUNT_INITIAL_BALANCE_PAISE - (2 * S3_REQUEST_AMOUNT_PAISE)
    ledger_verified = post_balance == expected_balance

    # Dimension 3: Mandate Usage (committed 300,000, remaining 100,000)
    mnds_res = await client.get_customer_mandates(session_token)
    mnd_items = {m["mandate_id"]: m for m in mnds_res.get("items", [])}
    mnd = mnd_items.get(S3_MANDATE_ID, {})
    committed = mnd.get("committed_paise", 0)
    remaining = mnd.get("remaining_paise", 0)
    expected_committed = 2 * S3_REQUEST_AMOUNT_PAISE  # 300,000
    expected_remaining = S3_TOTAL_BUDGET_PAISE - expected_committed  # 100,000
    usage_verified = committed == expected_committed and remaining == expected_remaining

    # Dimension 4: Audit Trail (exactly 2 bank.executed events)
    audit_res = await client.get_audit_entries(session_token)
    req_ids = {a.request_id for a in actions}
    relevant_events = [
        e for e in audit_res.get("items", [])
        if e.get("request_id") in req_ids
    ]
    bank_executed_events = [
        e for e in relevant_events
        if e.get("event_type") == "bank.executed"
    ]
    audit_verified = len(bank_executed_events) == 2

    iter_details = {
        "run_idx": run_idx,
        "executed_count": len(executed),
        "denied_count": len(budget_denials),
        "post_balance": post_balance,
        "remaining_budget": remaining,
    }

    return http_verified, ledger_verified, usage_verified, audit_verified, iter_details


async def run_s03(
    client: ScenarioClient,
    runs: int = 100,
    now: datetime | None = None,
    seed: int = 42,
) -> ScenarioResult:
    """Run S3 concurrency scenario across multiple randomized iterations."""
    current_time = now or datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)
    rng = random.Random(seed)

    all_http = True
    all_ledger = True
    all_usage = True
    all_audit = True
    failed_runs: list[dict[str, Any]] = []

    for run_idx in range(runs):
        h_ok, l_ok, u_ok, a_ok, details = await _run_single_s3_iteration(
            client=client,
            run_idx=run_idx,
            rng=rng,
            now=current_time,
        )
        if not (h_ok and l_ok and u_ok and a_ok):
            all_http = all_http and h_ok
            all_ledger = all_ledger and l_ok
            all_usage = all_usage and u_ok
            all_audit = all_audit and a_ok
            failed_runs.append(details)
            break

    passed = all_http and all_ledger and all_usage and all_audit
    failed_idx = failed_runs[0].get("run_idx") if failed_runs else "unknown"
    msg = (
        f"S3 passed across all {runs} randomized iterations"
        if passed
        else f"S3 concurrency failed on iteration {failed_idx}"
    )

    return ScenarioResult(
        scenario_id="S3",
        name=f"Concurrency Under Budget Constraint ({runs} runs)",
        passed=passed,
        http_verified=all_http,
        ledger_verified=all_ledger,
        usage_verified=all_usage,
        audit_verified=all_audit,
        message=msg,
        details={"runs_executed": runs, "failed_runs": failed_runs},
    )


__all__ = ["run_s03"]
