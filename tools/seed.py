"""Member A developer seed utility for test and demo data."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from bank_mock.schema import (
    BankBase,
    create_bank_db_engine,
    create_bank_session_factory,
)
from bank_mock.service import seed_bank_data
from gateway.config import AgentGateConfig


async def seed_data(profile: str) -> None:
    config = AgentGateConfig.load(profile=profile)

    # Seed bank database
    bank_engine = create_bank_db_engine(config.database.bank_url)
    async with bank_engine.begin() as conn:
        await conn.run_sync(BankBase.metadata.create_all)

    session_factory = create_bank_session_factory(bank_engine)
    async with session_factory() as session:
        await seed_bank_data(session)

    await bank_engine.dispose()
    print(
        f"AgentGate seed complete for profile '{profile}': "
        "cust_001, ACC-001 (12,500,000 paise), demo payees seeded."
    )


def main() -> None:
    Path("runtime").mkdir(parents=True, exist_ok=True)
    parser = argparse.ArgumentParser(description="Seed AgentGate test/demo data")
    parser.add_argument(
        "--profile",
        default=os.getenv("AGENTGATE_PROFILE", "test"),
        help="Runtime profile (test, scripted, real)",
    )
    args = parser.parse_args()
    asyncio.run(seed_data(args.profile))


if __name__ == "__main__":
    main()
