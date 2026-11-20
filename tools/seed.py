"""Member A developer seed utility for test and demo data."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

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


def _resolve_billpal_key() -> tuple[AgentKeyPair, Path]:
    billpal_key_path_env = os.getenv("D06_BILLPAL_KEY")
    if billpal_key_path_env:
        key_path = Path(billpal_key_path_env)
    else:
        key_path = Path("runtime/billpal.pem").resolve()

    if key_path.exists():
        pair = load_private_key_pem(key_path)
    else:
        pair = generate_agent_keypair()
        save_private_key_pem(pair, key_path, overwrite=True)

    if not os.getenv("D06_BILLPAL_KEY"):
        os.environ["D06_BILLPAL_KEY"] = str(key_path.resolve())
    return pair, key_path


def _resolve_db_urls(config: AgentGateConfig) -> tuple[str, str]:
    bank_url = config.database.bank_url
    if config.bank_db and Path(config.bank_db).is_absolute():
        bank_url = f"sqlite+aiosqlite:///{Path(config.bank_db).as_posix()}"

    gateway_url = config.database.gateway_url
    if config.gateway_db and Path(config.gateway_db).is_absolute():
        gateway_url = f"sqlite+aiosqlite:///{Path(config.gateway_db).as_posix()}"
    return bank_url, gateway_url


async def seed_data(profile: str) -> None:
    config = AgentGateConfig.load(profile=profile)
    Path("runtime").mkdir(parents=True, exist_ok=True)
    bank_url, gateway_url = _resolve_db_urls(config)
    pair, _ = _resolve_billpal_key()

    # 1. Seed bank database
    bank_engine = create_bank_db_engine(bank_url)
    async with bank_engine.begin() as conn:
        await conn.run_sync(BankBase.metadata.create_all)

    bank_sf = create_bank_session_factory(bank_engine)
    async with bank_sf() as session:
        await seed_bank_data(session)

    await bank_engine.dispose()

    # 2. Ensure signing keys (mandate, audit, token)
    def _ensure_key(path_str: str | None, default_rel: str) -> Path:
        target = Path(path_str) if path_str else Path(default_rel)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            kp = generate_agent_keypair()
            save_private_key_pem(kp, target, overwrite=True)
        return target

    _ensure_key(config.mandate_signing_key_path, "runtime/mandate.pem")
    _ensure_key(config.audit_signing_key_path, "runtime/audit.pem")
    _ensure_key(config.token_signing_key_path, "runtime/token.pem")

    # 3. Seed gateway database
    gw_engine = create_db_engine(gateway_url)
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

    customer_pin = os.getenv("D06_CUSTOMER_PIN", "4821")
    ops_id = os.getenv("D06_OPS_ID", "ops_admin")
    ops_password = os.getenv("D06_OPS_PASSWORD", "ops-secret-2026")

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
    for op_name in {ops_id, "ops_admin"}:
        async with gw_sf() as session:
            ops_res = await session.execute(
                select(OperatorRecord).where(OperatorRecord.operator_id == op_name)
            )
            ops_exists = ops_res.scalar_one_or_none() is not None

        if not ops_exists:
            await human_auth.seed_operator(
                operator_id=op_name,
                display_name="Ops Administrator",
                role=HumanRole.OPS,
                password=ops_password,
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
            password="reg-secret-2026",
        )

    # Seed BillPal Agent
    async with gw_sf() as session:
        agent_res = await session.execute(
            select(AgentRecord).where(AgentRecord.agent_id == "agt_billpal")
        )
        existing_agent = agent_res.scalar_one_or_none()
        agent_status = existing_agent.status if existing_agent else None

    if agent_status is None:
        public_jwk = public_key_to_jwk(pair.public_key)
        await registry.register(
            name="BillPal Agent",
            vendor="BillPal Inc",
            public_jwk=public_jwk,
            max_scopes=[e.value for e in ActionType],
            agent_id="agt_billpal",
        )
    elif agent_status != "ACTIVE":
        from sqlalchemy import update

        async with gw_sf() as session:
            await session.execute(
                update(AgentRecord)
                .where(AgentRecord.agent_id == "agt_billpal")
                .values(status="ACTIVE")
            )
            await session.commit()

    await gw_engine.dispose()

    print(
        f"AgentGate seed complete for profile '{profile}': "
        f"cust_001, ACC-001 (12,500,000 paise), demo payees, {ops_id}, and agt_billpal seeded."
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
