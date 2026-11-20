"""Member A runtime configuration and profile loader."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[1]


class ProfileSettings(BaseModel):
    allow_fakes: bool = True
    allow_dev_controls: bool = True
    frozen_clock: bool = True


class DatabaseSettings(BaseModel):
    gateway_url: str = "sqlite+aiosqlite:///runtime/gateway.db"
    bank_url: str = "sqlite+aiosqlite:///runtime/bank.db"
    wal: bool = True
    workers: int = 1


class RiskSettings(BaseModel):
    mode: Literal["enforce", "shadow"] = "enforce"
    shadow_allowed_profiles: list[str] = Field(default_factory=lambda: ["test"])


class ApprovalSettings(BaseModel):
    default_ttl_seconds: int = 300
    scripted_ttl_seconds: int = 60
    max_pending_per_mandate: int = 3


class AgentGateConfig(BaseSettings):
    """Runtime configuration model loading config.yaml and environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="AGENTGATE_",
        extra="ignore",
    )

    profile: Literal["test", "scripted", "real"] = "test"
    contract_version: str = "1.0.0"
    display_timezone: str = "Asia/Kolkata"
    currency: Literal["INR"] = "INR"

    gateway_origin: str = "http://127.0.0.1:8000"
    bank_origin: str = "http://127.0.0.1:8001"
    gateway_db: str = "runtime/gateway.db"
    bank_db: str = "runtime/bank.db"

    bank_service_credential: str | None = None
    human_session_secret: str | None = None
    dev_control_credential: str | None = None

    mandate_signing_key_path: str | None = None
    audit_signing_key_path: str | None = None
    token_signing_key_path: str | None = None

    profiles: dict[str, ProfileSettings] = Field(
        default_factory=lambda: {
            "test": ProfileSettings(
                allow_fakes=True, allow_dev_controls=True, frozen_clock=True
            ),
            "scripted": ProfileSettings(
                allow_fakes=False, allow_dev_controls=True, frozen_clock=True
            ),
            "real": ProfileSettings(
                allow_fakes=False, allow_dev_controls=False, frozen_clock=False
            ),
        }
    )
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    approval: ApprovalSettings = Field(default_factory=ApprovalSettings)

    @property
    def active_profile_settings(self) -> ProfileSettings:
        return self.profiles.get(self.profile, self.profiles["test"])

    @classmethod
    def load(
        cls,
        profile: str | None = None,
        config_path: Path | str | None = None,
        env_file: Path | str | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> AgentGateConfig:
        cfg_path = Path(config_path) if config_path else (ROOT_DIR / "config.yaml")
        yaml_data: dict[str, Any] = {}
        if cfg_path.exists():
            with cfg_path.open(encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
                if isinstance(loaded, dict):
                    yaml_data = loaded

        # Overlay explicitly passed profile or environment profile
        env_profile = os.getenv("AGENTGATE_PROFILE")
        target_profile = profile or env_profile or yaml_data.get("default_profile", "test")
        init_kwargs: dict[str, Any] = {**yaml_data}
        init_kwargs["profile"] = target_profile
        if overrides:
            init_kwargs.update(overrides)

        env_path = Path(env_file) if env_file else (ROOT_DIR / ".env")
        if env_path.exists():
            return cls(_env_file=str(env_path), **init_kwargs)
        return cls(**init_kwargs)
