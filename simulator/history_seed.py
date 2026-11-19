"""Member C deterministic 60-day synthetic history baseline generator."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from gateway.risk.history import RiskHistoryStore
from simulator.personas import (
    CUSTOMER_ID_PRIMARY,
    PAYEE_ELECTRICITY,
    PAYEE_MOBILE,
)

DEFAULT_SEED = 42


def seed_synthetic_history(
    store: RiskHistoryStore,
    *,
    customer_id: str = CUSTOMER_ID_PRIMARY,
    agent_id: str = "agt_billpal",
    mandate_id: str = "man_demo_60d",
    base_time: datetime | None = None,
    now: datetime | None = None,
    seed: int = DEFAULT_SEED,
) -> RiskHistoryStore:
    """Generate a reproducible, deterministic 60-day synthetic history.

    - Uses fixed random seed.
    - Every row is marked is_synthetic=True.
    - Generates 16 historical utility payments (BILL-ELEC, BILL-MOBILE)
      clustered around ₹1,850 (185,000 paise) so ₹1,850 is low-risk
      and ₹4,800 is an approvable step-up.
    - These rows do not count against current mandate usage.
    """
    rng = random.Random(seed)
    end_time = now or base_time or datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)

    # 16 payments spread roughly every 3-4 days over 60 days
    payees = [PAYEE_ELECTRICITY.payee_id, PAYEE_MOBILE.payee_id]

    for i in range(16):
        # Days back: from 58 days ago to 2 days ago
        days_ago = 58 - (i * 3) - rng.randint(0, 1)
        # Hour: 04:00 to 10:00 UTC (09:30 to 15:30 IST)
        hour = rng.randint(4, 10)
        minute = rng.randint(0, 59)
        second = rng.randint(0, 59)
        tx_time = end_time - timedelta(days=days_ago)
        tx_time = tx_time.replace(hour=hour, minute=minute, second=second)

        # Amounts around ₹1,850: 180,000 to 192,000 paise (mean ~185,000 paise)
        amount_paise = rng.randint(180_000, 192_000)
        payee_id = payees[i % len(payees)]

        attempt_id = f"synth_att_{i:03d}"
        request_id = f"synth_req_{i:03d}"

        # Record attempt
        store.record_attempt(
            attempt_id=attempt_id,
            request_id=request_id,
            customer_id=customer_id,
            agent_id=agent_id,
            mandate_id=mandate_id,
            amount_paise=amount_paise,
            payee_id=payee_id,
            verdict="ALLOW",
            decision="ALLOW",
            attempted_at=tx_time,
            is_synthetic=True,
        )

        # Record confirmed execution outcome
        store.record_outcome(
            request_id=request_id,
            customer_id=customer_id,
            agent_id=agent_id,
            mandate_id=mandate_id,
            payee_id=payee_id,
            amount_paise=amount_paise,
            status="EXECUTED",
            executed_at=tx_time,
            is_synthetic=True,
        )

    return store


__all__ = ["DEFAULT_SEED", "seed_synthetic_history"]
