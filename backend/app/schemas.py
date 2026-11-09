"""Pydantic models for the rule DSL and for rule execution results.

DSL semantics (every rule type):
  not_null              columns=[c1, ...]   fails if ANY listed column is null/blank
  unique                columns=[c1, ...]   fails on repeated (composite) values.
                                            params.keep: "first" (default, flags repeats) | "all" (flags every occurrence)
  range                 columns=[c]         params.min and/or params.max (inclusive); non-numeric values fail
  regex                 columns=[c]         params.pattern; the WHOLE value must match (re.fullmatch)
  date_format           columns=[c]         params.formats = accepted strptime formats (default: ISO 8601 variants)
  cross_column_compare  columns=[a, b]      params.operator in < <= > >= == != ; rule PASSES when  a <op> b
  lookup_in             columns=[c]         params.lookup_file, params.lookup_column ; value must exist in the lookup

Null convention: nulls are flagged ONLY by not_null. Every other rule skips null rows (counted as `skipped`).
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

COMPARE_OPERATORS = {"<", "<=", ">", ">=", "==", "!="}
DEFAULT_DATE_FORMATS = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"]
MAX_PATTERN_LENGTH = 300


class RuleType(str, Enum):
    NOT_NULL = "not_null"
    UNIQUE = "unique"
    RANGE = "range"
    REGEX = "regex"
    DATE_FORMAT = "date_format"
    CROSS_COLUMN_COMPARE = "cross_column_compare"
    LOOKUP_IN = "lookup_in"


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class Rule(BaseModel):
    rule_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    type: RuleType
    columns: list[str]
    params: dict[str, Any] = Field(default_factory=dict)
    severity: Literal["low", "medium", "high"] = "medium"
    description: str = ""

    @model_validator(mode="after")
    def _validate_params(self) -> "Rule":
        t, cols, p = self.type, self.columns, self.params
        if not cols:
            raise ValueError("columns must not be empty")
        if len(set(cols)) != len(cols):
            raise ValueError("columns must not contain duplicates")

        if t == RuleType.UNIQUE:
            if p.get("keep", "first") not in ("first", "all"):
                raise ValueError("unique.params.keep must be 'first' or 'all'")

        elif t == RuleType.RANGE:
            if len(cols) != 1:
                raise ValueError("range needs exactly one column")
            lo, hi = p.get("min"), p.get("max")
            if lo is None and hi is None:
                raise ValueError("range needs params.min and/or params.max")
            for label, v in (("min", lo), ("max", hi)):
                if v is not None and not _is_number(v):
                    raise ValueError(f"range.params.{label} must be a number")
            if lo is not None and hi is not None and lo > hi:
                raise ValueError("range.params.min must be <= max")

        elif t == RuleType.REGEX:
            if len(cols) != 1:
                raise ValueError("regex needs exactly one column")
            pattern = p.get("pattern")
            if not isinstance(pattern, str) or not pattern:
                raise ValueError("regex needs params.pattern (non-empty string)")
            if len(pattern) > MAX_PATTERN_LENGTH:
                raise ValueError(f"regex pattern longer than {MAX_PATTERN_LENGTH} characters")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regex: {exc}") from exc

        elif t == RuleType.DATE_FORMAT:
            if len(cols) != 1:
                raise ValueError("date_format needs exactly one column")
            fmts = p.get("formats")
            if fmts is not None and (
                not isinstance(fmts, list) or not fmts or not all(isinstance(f, str) and "%" in f for f in fmts)
            ):
                raise ValueError("date_format.params.formats must be a non-empty list of strptime formats")

        elif t == RuleType.CROSS_COLUMN_COMPARE:
            if len(cols) != 2:
                raise ValueError("cross_column_compare needs exactly two columns: [left, right]")
            if p.get("operator") not in COMPARE_OPERATORS:
                raise ValueError(f"cross_column_compare.params.operator must be one of {sorted(COMPARE_OPERATORS)}")

        elif t == RuleType.LOOKUP_IN:
            if len(cols) != 1:
                raise ValueError("lookup_in needs exactly one column")
            for key in ("lookup_file", "lookup_column"):
                if not isinstance(p.get(key), str) or not p[key]:
                    raise ValueError(f"lookup_in needs params.{key}")
        return self


class RuleResult(BaseModel):
    """Outcome of running one rule against one dataframe. Row indices are 0-based data-row positions."""

    rule_id: str
    status: Literal["ok", "error"] = "ok"
    error: str | None = None
    total_rows: int = 0
    pass_count: int = 0
    fail_count: int = 0
    skipped_count: int = 0                      # null rows the rule did not evaluate (counted as pass)
    failed_row_indices: list[int] = Field(default_factory=list)   # ALL failures; persistence layer caps
    failed_row_ids: list[str] = Field(default_factory=list)
    sample_reasons: list[dict[str, Any]] = Field(default_factory=list)  # [{row_index,row_id,reason}]

    @property
    def pass_rate(self) -> float | None:
        return None if self.status == "error" or self.total_rows == 0 else self.pass_count / self.total_rows
