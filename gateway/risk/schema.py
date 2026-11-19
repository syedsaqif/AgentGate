"""Member C risk-owned SQLAlchemy table registration for composition root."""

from __future__ import annotations

from typing import Any

from .models import Base, RiskAttemptRecord, RiskBaselineRecord


def register_risk_tables(metadata: Any = None) -> list[str]:
    """Register Member C owned risk tables with the gateway composition root metadata.

    Does not create the engine or issue DDL.
    """
    tables = [
        RiskAttemptRecord.__tablename__,
        RiskBaselineRecord.__tablename__,
    ]
    if metadata is not None and metadata is not Base.metadata:
        for table in Base.metadata.tables.values():
            if table.name in tables and table.name not in metadata.tables:
                table.to_metadata(metadata)
    return tables


__all__ = ["Base", "RiskAttemptRecord", "RiskBaselineRecord", "register_risk_tables"]
