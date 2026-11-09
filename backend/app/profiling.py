"""Deterministic profiling (no LLM). Everything here is computed fact; the Data Model agent
only interprets it. Output is plain JSON-serialisable dicts so it can go straight into a prompt
and into SQLite.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from itertools import permutations
from typing import Any

import numpy as np
import pandas as pd

from .data_io import null_mask

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")

# Order matters: each value is attributed to the FIRST format that parses it.
# dd/mm vs mm/dd is inherently ambiguous; dd/mm wins and the ambiguity is not resolved here.
CANDIDATE_DATE_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%b %d %Y",
    "%m-%d-%Y",
    "%d-%m-%Y",
]

MAX_SAMPLE_VALUES = 5
LOW_CARDINALITY = 10
FK_TYPES = {"string", "integer"}


# --------------------------------------------------------------------------- helpers
def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        return None if math.isnan(obj) or math.isinf(obj) else float(obj)
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    return obj


def _shape(value: str) -> str:
    """Collapse a string to a coarse pattern: 'ORD-10001' -> 'A-9', 'IN-UP' -> 'A-A'."""
    return re.sub(r"\d+", "9", re.sub(r"[A-Za-z]+", "A", value))


def _detect_date_formats(text: pd.Series) -> tuple[dict[str, int], int]:
    """Return ({format: value_count}, unmatched_count). Each value is counted once."""
    remaining = text
    counts: dict[str, int] = {}
    for fmt in CANDIDATE_DATE_FORMATS:
        if remaining.empty:
            break
        ok = pd.to_datetime(remaining, format=fmt, errors="coerce").notna()
        if ok.any():
            counts[fmt] = int(ok.sum())
        remaining = remaining[~ok]
    return counts, len(remaining)


# --------------------------------------------------------------------------- columns
def profile_column(s: pd.Series, name: str | None = None) -> dict:
    name = name or str(s.name)
    n = len(s)
    nulls = null_mask(s)
    nn = s[~nulls]
    non_null = len(nn)
    distinct = int(nn.nunique())

    prof: dict[str, Any] = {
        "name": name,
        "pandas_dtype": str(s.dtype),
        "row_count": n,
        "null_count": int(nulls.sum()),
        "null_pct": round(100 * float(nulls.sum()) / n, 2) if n else 0.0,
        "distinct_count": distinct,
        "is_unique": non_null > 0 and distinct == non_null,
        "sample_values": nn.drop_duplicates().head(MAX_SAMPLE_VALUES).tolist(),
    }
    prof["is_primary_key_candidate"] = bool(prof["is_unique"] and prof["null_count"] == 0)
    if 0 < distinct <= LOW_CARDINALITY:
        prof["top_values"] = {str(k): int(v) for k, v in nn.value_counts().items()}

    hints: list[str] = []
    numeric_values: pd.Series | None = None

    if non_null == 0:
        inferred = "unknown"
    elif pd.api.types.is_bool_dtype(nn):
        inferred = "boolean"
    elif pd.api.types.is_integer_dtype(nn):
        inferred, numeric_values = "integer", nn
    elif pd.api.types.is_float_dtype(nn):
        inferred, numeric_values = "float", nn
    elif pd.api.types.is_datetime64_any_dtype(nn):
        inferred = "datetime"
    else:
        text = nn.astype(str)
        num = pd.to_numeric(text, errors="coerce")
        numeric_pct = float(num.notna().mean())
        date_counts, unmatched = _detect_date_formats(text)
        date_pct = (non_null - unmatched) / non_null
        email_pct = 100 * float(text.map(lambda v: bool(EMAIL_RE.fullmatch(v))).mean())

        if numeric_pct == 1.0:
            inferred = "integer" if bool((num % 1 == 0).all()) else "float"
            numeric_values = num
        elif date_pct >= 0.8:
            inferred = "datetime"
        else:
            inferred = "string"

        prof["numeric_like_pct"] = round(100 * numeric_pct, 1)
        if inferred == "string":
            lengths = text.str.len()
            prof["min_length"], prof["max_length"] = int(lengths.min()), int(lengths.max())
            shapes = Counter(text.map(_shape))
            prof["top_patterns"] = [
                {"pattern": p, "count": c, "pct": round(100 * c / non_null, 1)} for p, c in shapes.most_common(3)
            ]
            prof["email_like_pct"] = round(email_pct, 1)
            if email_pct >= 50:
                hints.append("email")
        if date_pct >= 0.5:
            prof["date_formats"] = date_counts
            prof["mixed_date_formats"] = len(date_counts) > 1
            prof["unparseable_date_count"] = unmatched

    if inferred == "datetime":
        hints.append("datetime")
    if numeric_values is not None and len(numeric_values):
        prof.update(
            min=numeric_values.min(),
            max=numeric_values.max(),
            mean=round(float(numeric_values.mean()), 4),
            negative_count=int((numeric_values < 0).sum()),
            zero_count=int((numeric_values == 0).sum()),
        )

    prof["inferred_type"] = inferred
    prof["semantic_hints"] = hints
    return prof


# --------------------------------------------------------------------------- tables
def _ordering_hints(df: pd.DataFrame, columns: dict[str, dict]) -> list[dict]:
    """Numeric column pairs where `left <= right` holds on every row where both are present."""
    numeric = [c for c, p in columns.items() if p["inferred_type"] in ("integer", "float")]
    hints = []
    for a, b in permutations(numeric, 2):
        x, y = pd.to_numeric(df[a], errors="coerce"), pd.to_numeric(df[b], errors="coerce")
        both = x.notna() & y.notna()
        if both.sum() < 2:
            continue
        xa, yb = x[both], y[both]
        if bool((xa <= yb).all()) and not bool((xa == yb).all()):
            hints.append(
                {
                    "left": a,
                    "op": "<=",
                    "right": b,
                    "rows_compared": int(both.sum()),
                    "holds_pct": 100.0,
                    "strictly_less_pct": round(100 * float((xa < yb).mean()), 1),
                }
            )
    return hints


def profile_dataframe(df: pd.DataFrame) -> dict:
    columns = {str(c): profile_column(df[c], str(c)) for c in df.columns}
    return {"row_count": len(df), "columns": columns, "ordering_hints": _ordering_hints(df, columns)}


def detect_relationships(
    tables: dict[str, pd.DataFrame], table_profiles: dict[str, dict], min_overlap: float = 0.8
) -> list[dict]:
    """Foreign-key candidates: a child column whose values (almost) all exist in a UNIQUE parent column
    of the same type. At most one parent per child column (best name match, then best overlap)."""
    relationships: list[dict] = []
    for child_name, child in tables.items():
        for cc in child.columns:
            cp = table_profiles[child_name]["columns"][str(cc)]
            if cp["inferred_type"] not in FK_TYPES or cp["distinct_count"] == 0:
                continue
            child_vals = child[cc][~null_mask(child[cc])].astype(str)
            child_set = set(child_vals)

            best: tuple[tuple, dict] | None = None
            for parent_name, parent in tables.items():
                if parent_name == child_name:
                    continue
                for pc in parent.columns:
                    pp = table_profiles[parent_name]["columns"][str(pc)]
                    if pp["inferred_type"] != cp["inferred_type"] or not pp["is_unique"]:
                        continue
                    parent_set = set(parent[pc][~null_mask(parent[pc])].astype(str))
                    overlap = len(child_set & parent_set) / len(child_set)
                    if overlap < min_overlap:
                        continue
                    name_match = str(cc) == str(pc)
                    coverage = float(child_vals.isin(parent_set).mean())
                    cand = {
                        "child_table": child_name,
                        "child_column": str(cc),
                        "parent_table": parent_name,
                        "parent_column": str(pc),
                        "overlap_ratio": round(overlap, 4),
                        "row_coverage": round(coverage, 4),
                        "orphan_row_count": int((~child_vals.isin(parent_set)).sum()),
                        "name_match": name_match,
                        "cardinality": "one_to_one" if cp["is_unique"] else "many_to_one",
                        "confidence": "high" if (overlap == 1.0 and name_match) else
                                      "medium" if overlap >= 0.95 else "low",
                    }
                    rank = (name_match, overlap)
                    if best is None or rank > best[0]:
                        best = (rank, cand)
            if best:
                relationships.append(best[1])
    return relationships


def profile_tables(tables: dict[str, pd.DataFrame]) -> dict:
    """Profile several named tables and detect relationships between them. JSON-serialisable."""
    table_profiles = {name: profile_dataframe(df) for name, df in tables.items()}
    relationships = detect_relationships(tables, table_profiles)
    return _jsonable({"tables": table_profiles, "relationships": relationships})
