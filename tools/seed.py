"""Member A developer seed utility for test and demo data."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_sdk.keys import generate_agent_keypair, load_private_key_pem, save_private_key_pem
from bank_mock.schema import (
    BankBase,
    create_bank_db_engine,
    create_bank_session_factory,
)
from bank_mock.service import seed_bank_data
from contracts.common import HumanRole
from gateway.audit.append import SqlAuditLog
from gateway.audit.schema import create_audit_schema
from gateway.config import AgentGateConfig
from gateway.identity.crypto import generate_ed25519_keypair
from gateway.identity.human_auth import HumanSessionKeyring, SqlHumanAuthService
from gateway.identity.models import AgentRecord, CustomerRecord, OperatorRecord, session_scope
from gateway.identity.registry import SqlAgentRegistry
from gateway.identity.schema import (
    create_identity_schema,
    create_identity_session_factory,
    create_sqlite_identity_engine,
)
from gateway.platform.clock import SystemClock
from gateway.platform.db import Base, create_db_engine
from gateway.platform.startup import init_database


async def seed_data(profile: str) -> None:
    config = AgentGateConfig.load(profile=profile)
    Path("runtime").mkdir(parents=True, exist_ok=True)

    # 1. Seed bank database
    bank_engine = create_bank_db_engine(config.database.bank_url)
    async with bank_engine.begin() as conn:
        await conn.run_sync(BankBase.metadata.create_all)

    session_factory = create_bank_session_factory(bank_engine)
    async with session_factory() as session:
        await seed_bank_data(session)

    await bank_engine.dispose()

    # 2. Seed gateway database schema
    gw_sync_engine = create_sqlite_identity_engine(config.database.gateway_url)
    create_identity_schema(gw_sync_engine)
    create_audit_schema(gw_sync_engine)

    gw_async_engine = create_db_engine(config.database.gateway_url)
    await init_database(gw_async_engine, Base.metadata)
    await gw_async_engine.dispose()

    # 3. Setup signing keys (mandate, audit, token)
    def _ensure_key(path_str: str | None, default_rel: str) -> Path:
        target = Path(path_str) if path_str else Path(default_rel)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            kp = generate_agent_keypair()
            save_private_key_pem(kp, target, overwrite=True)
        return target

    _ensure_key(config.mandate_signing_key_path, "runtime/mandate.pem")
    audit_key_file = _ensure_key(config.audit_signing_key_path, "runtime/audit.pem")
    _ensure_key(config.token_signing_key_path, "runtime/token.pem")

    # 4. Setup BillPal agent keypair
    billpal_env = os.getenv("D06_BILLPAL_KEY")
    billpal_path = Path(billpal_env) if billpal_env else Path("runtime/billpal.pem")
    if not billpal_path.exists():
        billpal_path.parent.mkdir(parents=True, exist_ok=True)
        bp_kp = generate_agent_keypair()
        save_private_key_pem(bp_kp, billpal_path, overwrite=True)
    else:
        bp_kp = load_private_key_pem(billpal_path)

    # 5. Seed Human Accounts (Customer and Ops) & Agent (BillPal)
    sf = create_identity_session_factory(gw_sync_engine)
    clock = SystemClock()
    audit_kp = load_private_key_pem(audit_key_file)
    audit_log = SqlAuditLog(
        session_factory=sf,
        signing_key=audit_kp.private_key,
        clock=clock,
    )
    human_auth = SqlHumanAuthService(
        session_factory=sf,
        clock=clock,
        keyring=HumanSessionKeyring.generate(),
        audit_log=audit_log,
    )
    agent_registry = SqlAgentRegistry(session_factory=sf, clock=clock, audit_log=audit_log)

    customer_pin = os.getenv("D06_CUSTOMER_PIN", "4821")
    ops_id = os.getenv("D06_OPS_ID", "ops_admin")
    ops_password = os.getenv("D06_OPS_PASSWORD", "ops-secret-2026")

    # Seed Customer
    with sf() as session:
        existing_cust = session.get(CustomerRecord, "cust_001")
    if not existing_cust:
        await human_auth.seed_customer(
            customer_id="cust_001",
            display_name="Demo Customer",
            pin=customer_pin,
        )

    # Seed Ops Operators (both ops_id and fallback probe names)
    for op_name in {ops_id, "ops_admin", "d06-probe"}:
        with sf() as session:
            existing_op = session.get(OperatorRecord, op_name)
        if not existing_op:
            await human_auth.seed_operator(
                operator_id=op_name,
                display_name="Operations Administrator",
                role=HumanRole.OPS,
                password=ops_password,
            )

    # Seed Registrar
    with sf() as session:
        existing_reg = session.get(OperatorRecord, "reg_001")
    if not existing_reg:
        await human_auth.seed_operator(
            operator_id="reg_001",
            display_name="Agent Registrar",
            role=HumanRole.REGISTRAR,
            password="reg-secret-2026",
        )

    # Register BillPal
    with sf() as session:
        existing_agent = session.get(AgentRecord, "agt_billpal")
    if not existing_agent:
        await agent_registry.register(
            agent_id="agt_billpal",
            name="BillPal",
            vendor="Acme Billing Services",
            public_jwk=bp_kp.public_jwk(),
            max_scopes=["bill_pay", "balance_read"],
        )

    gw_sync_engine.dispose()
    print(
        f"AgentGate seed complete for profile '{profile}': "
        "cust_001, ACC-001 (12,500,000 paise), demo payees, ops accounts, and agt_billpal seeded."
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
