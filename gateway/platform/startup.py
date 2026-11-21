"""Member A schema registration and recovery startup routines."""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncEngine

# Ensure Member A tables are registered on Base.metadata
import gateway.executor.schema  # noqa: F401
import gateway.mandates.schema  # noqa: F401


async def init_database(engine: AsyncEngine, metadata: MetaData) -> None:
    """Ensure database tables exist according to the supplied metadata."""
    # 1. Member B: identity and audit tables (if available)
    try:
        from gateway.identity.schema import register_identity_tables  # type: ignore

        register_identity_tables(metadata)
    except (ImportError, AttributeError):
        pass

    try:
        from gateway.audit.schema import register_audit_tables  # type: ignore

        register_audit_tables(metadata)
    except (ImportError, AttributeError):
        pass

    # 2. Member A tables are already registered via imports above

    # 3. Member C: risk_attempts, risk_baselines, escalations
    try:
        from gateway.risk.schema import register_risk_tables

        register_risk_tables(metadata)
    except (ImportError, AttributeError):
        pass

    try:
        from gateway.escalation.schema import register_escalation_tables

        register_escalation_tables(metadata)
    except (ImportError, AttributeError):
        pass

    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
