"""Member C escalation-owned SQLAlchemy table registration for composition root."""

from __future__ import annotations

from typing import Any

from .models import Base, EscalationRecord


def register_escalation_tables(metadata: Any = None) -> list[str]:
    """Register Member C owned escalations table with gateway composition root metadata.

    Does not create the engine or issue DDL.
    """
    tables = [EscalationRecord.__tablename__]
    if metadata is not None and metadata is not Base.metadata:
        for table in Base.metadata.tables.values():
            if table.name in tables and table.name not in metadata.tables:
                table.to_metadata(metadata)
    return tables


__all__ = ["Base", "EscalationRecord", "register_escalation_tables"]
