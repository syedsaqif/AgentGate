"""Member A developer seed utility for test and demo data."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from secrets import token_urlsafe

from sqlalchemy import select

from agent_sdk.keys import (
    AgentKeyPair,
    generate_agent_keypair,
    load_private_key_pem,
    public_key_to_jwk,
    save_private_key_pem,
)
from bank_mock.schema import (
    BankBase,
    create_bank_db_engine,
    create_bank_session_factory,
)
from bank_mock.service import seed_bank_data
from contracts.common import ActionType, HumanRole
from gateway.config import AgentGateConfig
from gateway.identity.human_auth import HumanSessionKeyring, SqlHumanAuthService
from gateway.identity.models import AgentRecord, CustomerRecord, OperatorRecord
from gateway.identity.registry import SqlAgentRegistry
from gateway.platform.clock import SystemClock
from gateway.platform.db import (
    Base,
    create_db_engine,
    create_session_factory,
)
from gateway.platform.startup import init_database


def _load_or_create_key(path: Path) -> AgentKeyPair:
    if path.exists():
        return load_private_key_pem(path)
    pair = generate_agent_keypair()
    save_private_key_pem(pair, path)
    return pair


def _prepare_keys(config: AgentGateConfig) -> AgentKeyPair:
    _load_or_create_key(Path(config.mandate_signing_key_path or "runtime/mandate.pem"))
    _load_or_create_key(Path(config.audit_signing_key_path or "runtime/audit.pem"))
    _load_or_create_key(Path(config.token_signing_key_path or "runtime/token.pem"))
    return _load_or_create_key(Path(os.getenv("D06_BILLPAL_KEY") or "runtime/billpal.pem"))


async def seed_data(profile: str) -> None:
    config = AgentGateConfig.load(profile=profile)
    customer_pin = os.getenv("D06_CUSTOMER_PIN", "4821")
    ops_id = os.getenv("D06_OPS_ID", "ops_admin")
    ops_password = os.getenv("D06_OPS_PASSWORD")
    if profile == "real" and not ops_password:
        raise SystemExit("Set D06_OPS_PASSWORD for the disposable real-profile seed.")
    pair = await asyncio.to_thread(_prepare_keys, config)

    # 1. Seed bank database
    bank_engine = create_bank_db_engine(config.bank_database_url)
    async with bank_engine.begin() as conn:
        await conn.run_sync(BankBase.metadata.create_all)

    bank_sf = create_bank_session_factory(bank_engine)
    async with bank_sf() as session:
        await seed_bank_data(session)

    await bank_engine.dispose()

    # 2. Seed gateway database
    gw_engine = create_db_engine(config.gateway_database_url)
    await init_database(gw_engine, Base.metadata)
    gw_sf = create_session_factory(gw_engine)

    clock = SystemClock()
    keyring = HumanSessionKeyring.generate()
    human_auth = SqlHumanAuthService(
        session_factory=gw_sf,
        clock=clock,
        keyring=keyring,
    )
    registry = SqlAgentRegistry(session_factory=gw_sf, clock=clock)

    # Seed Customer
    async with gw_sf() as session:
        cust_res = await session.execute(
            select(CustomerRecord).where(CustomerRecord.customer_id == "cust_001")
        )
        cust_exists = cust_res.scalar_one_or_none() is not None

    if not cust_exists:
        await human_auth.seed_customer(
            customer_id="cust_001",
            display_name="Demo Customer",
            pin=customer_pin,
        )

    # Seed Operator
    async with gw_sf() as session:
        existing_ops = await session.get(OperatorRecord, ops_id)
    if existing_ops is None:
        await human_auth.seed_operator(
            operator_id=ops_id,
            display_name="Ops Administrator",
            role=HumanRole.OPS,
            password=ops_password or token_urlsafe(24),
        )

    # Seed Registrar
    async with gw_sf() as session:
        reg_res = await session.execute(
            select(OperatorRecord).where(OperatorRecord.operator_id == "reg_001")
        )
        reg_exists = reg_res.scalar_one_or_none() is not None

    if not reg_exists:
        await human_auth.seed_operator(
            operator_id="reg_001",
            display_name="Agent Registrar",
            role=HumanRole.REGISTRAR,
            password=os.getenv("D06_REGISTRAR_PASSWORD") or token_urlsafe(24),
        )

    # Seed BillPal Agent
    async with gw_sf() as session:
        agent_res = await session.execute(
            select(AgentRecord).where(AgentRecord.agent_id == "agt_billpal")
        )
        existing_agent = agent_res.scalar_one_or_none()
    if existing_agent is None:
        public_jwk = public_key_to_jwk(pair.public_key)
        await registry.register(
            name="BillPal Agent",
            vendor="BillPal Inc",
            public_jwk=public_jwk,
            max_scopes=[e.value for e in ActionType],
            agent_id="agt_billpal",
        )
    await gw_engine.dispose()
    if existing_agent is not None and existing_agent.jkt != pair.jkt:
        raise SystemExit("The BillPal key does not match the registered agent in this database.")

    print(
        f"AgentGate seed complete for profile '{profile}': "
        f"cust_001, ACC-001 (12,500,000 paise), demo payees, {ops_id}, and agt_billpal seeded."
    )


def main() -> None:
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
