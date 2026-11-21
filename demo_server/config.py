"""Configuration for the AgentGate demo backend service."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class DemoServerConfig:
    """Settings for the loopback demo server on port 8002."""

    host: str = "127.0.0.1"
    port: int = 8002
    gateway_origin: str = "http://127.0.0.1:8000"
    bank_origin: str = "http://127.0.0.1:8001"
    bank_service_key: str = "local-bank-dev-secret"
    ops_id: str = "ops_admin"
    ops_password: str | None = None
    billpal_key_path: Path | None = None
    customer_id: str = "cust_001"
    account_id: str = "ACC-001"
    customer_pin: str | None = None
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:4173",
            "http://127.0.0.1:4173",
            "http://localhost:8002",
            "http://127.0.0.1:8002",
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ]
    )

    @classmethod
    def from_env(cls) -> DemoServerConfig:
        gw_origin = os.getenv("AGENTGATE_GATEWAY_ORIGIN", "http://127.0.0.1:8000")
        bank_origin = os.getenv("AGENTGATE_BANK_ORIGIN", "http://127.0.0.1:8001")
        bank_key = os.getenv("AGENTGATE_BANK_SERVICE_CREDENTIAL", "dev-bank-service-credential-2026")
        ops_id = os.getenv("D06_OPS_ID", "ops_admin")
        ops_pw = os.getenv("D06_OPS_PASSWORD", "ops-secret-2026")
        cust_pin = os.getenv("D06_CUSTOMER_PIN", "4821")
        key_path_env = os.getenv("D06_BILLPAL_KEY", "runtime/billpal.pem")
        billpal_key = Path(key_path_env) if key_path_env and Path(key_path_env).exists() else None

        custom_origins = os.getenv("DEMO_FRONTEND_ORIGINS")
        origins = [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:4173",
            "http://127.0.0.1:4173",
            "http://localhost:8002",
            "http://127.0.0.1:8002",
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ]
        if custom_origins:
            for item in custom_origins.split(","):
                cleaned = item.strip()
                if cleaned and cleaned not in origins:
                    origins.append(cleaned)

        return cls(
            host=os.getenv("DEMO_HOST", "127.0.0.1"),
            port=int(os.getenv("DEMO_PORT", "8002")),
            gateway_origin=gw_origin,
            bank_origin=bank_origin,
            bank_service_key=bank_key,
            ops_id=ops_id,
            ops_password=ops_pw,
            billpal_key_path=billpal_key,
            customer_id=os.getenv("DEMO_CUSTOMER_ID", "cust_001"),
            account_id=os.getenv("DEMO_ACCOUNT_ID", "ACC-001"),
            customer_pin=cust_pin,
            allowed_origins=origins,
        )
