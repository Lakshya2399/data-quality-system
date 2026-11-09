"""Agent 1: Data Lookup & Model.

Facts (types, keys, null rates, relationships) come from profiling. The LLM supplies only
meaning: semantic types, descriptions, expected constraints. Its answer is cross-checked
against the profile (every column covered, no invented names, primary keys really unique)
and retried with specific errors if it doesn't line up. assemble_data_model() then merges the
two, so the LLM can never override a deterministic fact.
"""
from __future__ import annotations

import json
from collections import Counter

from ..agent_schemas import (
    ColumnModel, CrossColumnConstraint, DataModel, ModelEnrichment, RelationshipModel, TableModel,
)
from ..llm import LLM
from ..prompts import render

AGENT = "model_agent"
OBSERVED_KEYS = ("null_pct", "distinct_count", "is_unique", "min", "max", "mean", "date_formats",
                 "mixed_date_formats", "top_values", "sample_values")


def check_enrichment(e: ModelEnrichment, profile: dict) -> list[str]:
    """Problems with the LLM's answer relative to the profile (empty list = fine)."""
    problems: list[str] = []
    tables = profile["tables"]

    names = [t.table for t in e.tables]
    missing_t = [t for t in tables if t not in names]
    unknown_t = sorted({t for t in names if t not in tables})
    if missing_t:
        problems.append(f"tables: missing entries for {missing_t}")
    if unknown_t:
        problems.append(f"tables: unknown tables {unknown_t}; use only {list(tables)}")
    if len(names) != len(set(names)):
        problems.append("tables: duplicate entries")

    for t in e.tables:
        cols = tables.get(t.table, {}).get("columns", {})
        for k in t.primary_key:
            if k not in cols:
                problems.append(f"tables.{t.table}.primary_key: unknown column '{k}'")
            elif not cols[k]["is_unique"]:
                problems.append(f"tables.{t.table}.primary_key: '{k}' is not unique in the profile, so it cannot be a key")

    expected = {(t, c) for t, tp in tables.items() for c in tp["columns"]}
    counts = Counter((c.table, c.column) for c in e.columns)
    missing_c = [f"{t}.{c}" for t, c in sorted(expected) if (t, c) not in counts]
    unknown_c = [f"{t}.{c}" for (t, c) in counts if (t, c) not in expected]
    dup_c = [f"{t}.{c}" for (t, c), n in counts.items() if n > 1]
    if missing_c:
        problems.append(f"columns: missing entries for {missing_c}")
    if unknown_c:
        problems.append(f"columns: unknown columns {unknown_c}")
    if dup_c:
        problems.append(f"columns: duplicate entries for {dup_c}")

    for x in e.cross_column_constraints:
        known = tables.get(x.table, {}).get("columns")
        if known is None:
            problems.append(f"cross_column_constraints: unknown table '{x.table}'")
        else:
            bad = [c for c in x.columns if c not in known]
            if bad:
                problems.append(f"cross_column_constraints: unknown columns {bad} in table '{x.table}'")
    return problems


def assemble_data_model(profile: dict, e: ModelEnrichment) -> DataModel:
    notes = {t.table: t for t in e.tables}
    sem = {(c.table, c.column): c for c in e.columns}
    rel_desc = {(r.child_table, r.child_column, r.parent_table, r.parent_column): r.description for r in e.relationships}

    tables: list[TableModel] = []
    for tname, tp in profile["tables"].items():
        note = notes[tname]
        cols = []
        for cname, cp in tp["columns"].items():
            s = sem[(tname, cname)]
            cols.append(
                ColumnModel(
                    name=cname,
                    data_type=cp["inferred_type"],
                    semantic_type=s.semantic_type,
                    description=s.description,
                    expected_constraints=s.expected_constraints,
                    has_nulls=cp["null_count"] > 0,
                    observed={k: cp[k] for k in OBSERVED_KEYS if k in cp},
                )
            )
        tables.append(
            TableModel(name=tname, role=note.role, description=note.description, row_count=tp["row_count"],
                       primary_key=note.primary_key, columns=cols)
        )

    relationships = []
    for r in profile["relationships"]:
        key = (r["child_table"], r["child_column"], r["parent_table"], r["parent_column"])
        relationships.append(
            RelationshipModel(
                child_table=r["child_table"], child_column=r["child_column"],
                parent_table=r["parent_table"], parent_column=r["parent_column"],
                cardinality=r["cardinality"], overlap_ratio=r["overlap_ratio"], confidence=r["confidence"],
                description=rel_desc.get(key, f"{key[0]}.{key[1]} references {key[2]}.{key[3]}"),
            )
        )

    constraints: list[CrossColumnConstraint] = []
    seen: set[tuple[str, frozenset[str]]] = set()
    for tname, tp in profile["tables"].items():                 # profile-derived first: they are facts
        for h in tp.get("ordering_hints", []):
            cols = [h["left"], h["right"]]
            seen.add((tname, frozenset(cols)))
            constraints.append(
                CrossColumnConstraint(
                    table=tname, columns=cols, source="profile",
                    description=f"{h['left']} {h['op']} {h['right']} held on all {h['rows_compared']} sample rows",
                )
            )
    for x in e.cross_column_constraints:
        if (x.table, frozenset(x.columns)) not in seen:
            constraints.append(CrossColumnConstraint(table=x.table, columns=x.columns, description=x.description, source="llm"))

    return DataModel(tables=tables, relationships=relationships, cross_column_constraints=constraints)


def build_data_model(llm: LLM, profile: dict) -> DataModel:
    prompt = render("model_agent", profile=json.dumps(profile, separators=(",", ":")))
    enrichment = llm.generate_json(
        prompt, ModelEnrichment, validator=lambda e: check_enrichment(e, profile), agent=AGENT
    )
    return assemble_data_model(profile, enrichment)
