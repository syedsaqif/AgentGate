"""Member A mandate-owned SQLAlchemy table registration."""

from __future__ import annotations

from sqlalchemy import MetaData

from gateway.mandates.models import MandateRecord, MandateUsageRecord


def register_mandate_tables(metadata: MetaData) -> list[str]:
    """Register mandate and mandate_usage tables into the supplied metadata registry."""
    from gateway.platform.db import Base

    tables = [MandateRecord.__tablename__, MandateUsageRecord.__tablename__]
    if metadata is not Base.metadata:
        for table in Base.metadata.tables.values():
            if table.name in tables and table.name not in metadata.tables:
                table.to_metadata(metadata)
    return tables
