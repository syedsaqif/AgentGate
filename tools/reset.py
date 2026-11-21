"""Member A developer reset utility to clear disposable test state."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import httpx

from bank_mock.schema import (
    BankBase,
    create_bank_db_engine,
    create_bank_session_factory,
)
from bank_mock.service import seed_bank_data
from gateway.config import AgentGateConfig
from gateway.platform.db import Base, create_db_engine
from gateway.platform.dev import DEFAULT_DEV_KEY
from gateway.platform.startup import init_database


async def reset_local_databases(profile: str) -> None:
    """Perform clean offline database reset and seed."""
    config = AgentGateConfig.load(profile=profile)

    # 1. Reset gateway DB
    gw_engine = create_db_engine(config.database.gateway_url)
    async with gw_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_database(gw_engine, Base.metadata)
    await gw_engine.dispose()

    # 2. Reset bank DB
    bank_engine = create_bank_db_engine(config.database.bank_url)
    async with bank_engine.begin() as conn:
        await conn.run_sync(BankBase.metadata.drop_all)
        await conn.run_sync(BankBase.metadata.create_all)

    session_factory = create_bank_session_factory(bank_engine)
    async with session_factory() as session:
        await seed_bank_data(session)
    await bank_engine.dispose()

    print(
        f"AgentGate local databases cleanly reset and seeded for profile '{profile}'."
    )


async def reset_dev(base_url: str | None, credential: str, profile: str) -> None:
    headers = {"X-AgentGate-Dev-Credential": credential}
    connected = False
    if base_url:
        try:
            async with httpx.AsyncClient(base_url=base_url, timeout=0.5) as client:
                res = await client.post(
                    "/v1/dev/reset", headers=headers, json={"seed": True}
                )
                if res.status_code == 200:
                    print(f"AgentGate dev state reset successfully via HTTP at {base_url}.")
                    connected = True
                else:
                    print(f"Server at {base_url} returned HTTP {res.status_code}: {res.text}")
        except Exception:
            pass

    if not connected:
        await reset_local_databases(profile)


def main() -> None:
    Path("runtime").mkdir(parents=True, exist_ok=True)
    parser = argparse.ArgumentParser(description="Reset AgentGate disposable test state")
    parser.add_argument(
        "--profile",
        default=os.getenv("AGENTGATE_PROFILE", "scripted"),
        help="Runtime profile (test, scripted, real)",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("AGENTGATE_BASE_URL", "http://127.0.0.1:8000"),
        help="Gateway base URL",
    )
    parser.add_argument(
        "--credential",
        default=os.getenv("AGENTGATE_DEV_CREDENTIAL", DEFAULT_DEV_KEY),
        help="Dev control credential",
    )
    args = parser.parse_args()
    asyncio.run(reset_dev(args.base_url, args.credential, args.profile))


if __name__ == "__main__":
    main()
