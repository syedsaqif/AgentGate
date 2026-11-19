"""Member A schema registration and recovery startup routines."""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncEngine

# Ensure Member A tables are registered on Base.metadata
import gateway.mandates.schema  # noqa: F401


async def init_database(engine: AsyncEngine, metadata: MetaData) -> None:
    """Ensure database tables exist according to the supplied metadata."""
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
