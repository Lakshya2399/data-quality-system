from __future__ import annotations

from ..agent_schemas import DataModel


def primary_key_column(model: DataModel, table: str) -> str | None:
    """The single-column primary key of `table`, used to label failing rows (None -> row index)."""
    for t in model.tables:
        if t.name == table and len(t.primary_key) == 1:
            return t.primary_key[0]
    return None
