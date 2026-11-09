"""Pydantic models used by the agents.

LLM-facing models are deliberately FLAT and small (the LLM adds meaning, code supplies facts):
  ModelEnrichment  - semantics for the data model agent
  RuleDraftList    - rule builder output (flat params; converted to the validated Rule DSL)
  RatingList       - rater output
Stored/derived models:
  DataModel        - deterministic facts from profiling + LLM semantics, persisted as JSON
  Rating           - final rating after the deterministic caps are applied
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .schemas import Rule, RuleType

# =========================================================================== data model: LLM-facing
class TableNote(BaseModel):
    table: str
    role: Literal["fact", "lookup", "other"]
    description: str
    primary_key: list[str] = Field(default_factory=list)


class ColumnNote(BaseModel):
    table: str
    column: str
    semantic_type: str
    description: str
    expected_constraints: list[str] = Field(default_factory=list)


class RelationshipNote(BaseModel):
    child_table: str
    child_column: str
    parent_table: str
    parent_column: str
    description: str


class ConstraintNote(BaseModel):
    table: str
    columns: list[str]
    description: str


class ModelEnrichment(BaseModel):
    tables: list[TableNote]
    columns: list[ColumnNote]
    relationships: list[RelationshipNote] = Field(default_factory=list)
    cross_column_constraints: list[ConstraintNote] = Field(default_factory=list)


# =========================================================================== data model: final
class ColumnModel(BaseModel):
    name: str
    data_type: str                                  # from profiling (fact)
    semantic_type: str                              # from the LLM
    description: str
    expected_constraints: list[str] = Field(default_factory=list)
    has_nulls: bool
    observed: dict[str, Any] = Field(default_factory=dict)   # null_pct, distinct_count, min, max, ...


class TableModel(BaseModel):
    name: str
    role: str
    description: str
    row_count: int
    primary_key: list[str]
    columns: list[ColumnModel]


class RelationshipModel(BaseModel):
    child_table: str
    child_column: str
    parent_table: str
    parent_column: str
    cardinality: str
    overlap_ratio: float
    confidence: str
    description: str


class CrossColumnConstraint(BaseModel):
    table: str
    columns: list[str]
    description: str
    source: Literal["profile", "llm"]


class DataModel(BaseModel):
    tables: list[TableModel]
    relationships: list[RelationshipModel] = Field(default_factory=list)
    cross_column_constraints: list[CrossColumnConstraint] = Field(default_factory=list)

    def table_names(self) -> list[str]:
        return [t.name for t in self.tables]

    def columns_of(self, table: str) -> list[str]:
        return [c.name for t in self.tables if t.name == table for c in t.columns]

    def summary_text(self) -> str:
        """Compact human-readable summary (used in the rater prompt)."""
        lines: list[str] = []
        for t in self.tables:
            pk = f", primary key: {', '.join(t.primary_key)}" if t.primary_key else ""
            lines.append(f"- {t.name} ({t.role}, {t.row_count} sample rows{pk}): {t.description}")
            for c in t.columns:
                cons = f" [{'; '.join(c.expected_constraints)}]" if c.expected_constraints else ""
                dtype = "datetime stored as text" if c.data_type == "datetime" else c.data_type
                lines.append(f"    {c.name} ({dtype}, {c.semantic_type}){cons}")
        for r in self.relationships:
            lines.append(
                f"- relationship: {r.child_table}.{r.child_column} -> {r.parent_table}.{r.parent_column} "
                f"({r.cardinality}): {r.description}"
            )
        for x in self.cross_column_constraints:
            lines.append(f"- cross-column ({x.table}: {', '.join(x.columns)}): {x.description}")
        return "\n".join(lines)


# =========================================================================== rule builder
_OPERATOR_ALIASES = {"≤": "<=", "≥": ">=", "=": "==", "<>": "!=", "=<": "<=", "=>": ">="}


class RuleDraft(BaseModel):
    """What the LLM writes for one rule. Flat: only the fields a rule's type uses are filled."""

    name: str
    type: RuleType
    columns: list[str]
    severity: Literal["low", "medium", "high"] = "medium"
    description: str = ""
    # type-specific fields
    min: float | None = None
    max: float | None = None
    pattern: str | None = None
    formats: list[str] | None = None
    operator: str | None = None
    lookup_file: str | None = None
    lookup_column: str | None = None
    keep: Literal["first", "all"] | None = None

    @field_validator("columns", mode="before")
    @classmethod
    def _columns_as_list(cls, v):
        return [v] if isinstance(v, str) else v

    @field_validator("name", mode="after")
    @classmethod
    def _slug(cls, v: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", v.strip().lower()).strip("_") or v

    @field_validator("severity", mode="before")
    @classmethod
    def _lower_severity(cls, v):
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("operator", mode="before")
    @classmethod
    def _normalise_operator(cls, v):
        return _OPERATOR_ALIASES.get(v.strip(), v.strip()) if isinstance(v, str) else v

    def to_rule(self, rule_id: str) -> Rule:
        """Convert to the validated DSL (raises pydantic.ValidationError if params are invalid)."""

        def num(x: float) -> float | int:
            return int(x) if float(x).is_integer() else x

        t, p = self.type, {}
        if t == RuleType.UNIQUE and self.keep:
            p["keep"] = self.keep
        elif t == RuleType.RANGE:
            if self.min is not None:
                p["min"] = num(self.min)
            if self.max is not None:
                p["max"] = num(self.max)
        elif t == RuleType.REGEX:
            p["pattern"] = self.pattern
        elif t == RuleType.DATE_FORMAT and self.formats:
            p["formats"] = self.formats
        elif t == RuleType.CROSS_COLUMN_COMPARE:
            p["operator"] = self.operator
        elif t == RuleType.LOOKUP_IN:
            p["lookup_file"], p["lookup_column"] = self.lookup_file, self.lookup_column
        return Rule(
            rule_id=rule_id, name=self.name, type=t, columns=self.columns, params=p,
            severity=self.severity, description=self.description,
        )


class RuleDraftList(BaseModel):
    rules: list[RuleDraft] = Field(min_length=1)


# =========================================================================== rater
class RatingItem(BaseModel):
    rule_id: str
    score: int = Field(ge=1, le=5)
    justification: str = Field(min_length=3)


class RatingList(BaseModel):
    ratings: list[RatingItem]


class Rating(BaseModel):
    """Final rating: the LLM's score after deterministic caps. `justification` includes any cap note."""

    rule_id: str
    score: int = Field(ge=1, le=5)
    justification: str
    llm_score: int
    adjustment: str | None = None
