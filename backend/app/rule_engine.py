"""Deterministic rule interpreter. No LLM output is ever executed as code: a rule's `type`
selects one of the evaluator functions below.

Each evaluator returns (fail_mask, skipped_mask, reason_fn):
  fail_mask     bool Series, True where the row violates the rule
  skipped_mask  bool Series, True where the rule was not evaluated because of nulls
  reason_fn     row_index -> human-readable explanation (only called for a few failing rows)
"""
from __future__ import annotations

import operator
import re
from typing import Callable, Iterable

import pandas as pd

from .data_io import null_mask
from .schemas import Rule, RuleResult, RuleType

_OPS = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
}

Evaluation = tuple[pd.Series, pd.Series, Callable[[int], str]]


class RuleExecutionError(Exception):
    """Rule cannot be run (unknown column, missing lookup, ...)."""


def normalize_lookup_name(name: str) -> str:
    """'valid_regions.csv' and 'valid_regions' refer to the same lookup."""
    name = name.strip()
    return name[:-4] if name.lower().endswith(".csv") else name


def check_rule_columns(
    rule: Rule, columns: Iterable[str], lookup_columns: dict[str, list[str]] | None = None
) -> list[str]:
    """Return a list of problems (empty = OK). Used by the validate node before any rule runs."""
    problems: list[str] = []
    cols = set(columns)
    missing = [c for c in rule.columns if c not in cols]
    if missing:
        problems.append(f"unknown column(s) {missing}; available: {sorted(cols)}")
    if rule.type == RuleType.LOOKUP_IN:
        lookups = {normalize_lookup_name(k): v for k, v in (lookup_columns or {}).items()}
        key = normalize_lookup_name(rule.params["lookup_file"])
        if key not in lookups:
            problems.append(f"unknown lookup_file '{rule.params['lookup_file']}'; available: {sorted(lookups)}")
        elif rule.params["lookup_column"] not in lookups[key]:
            problems.append(
                f"unknown lookup_column '{rule.params['lookup_column']}' in '{key}'; available: {sorted(lookups[key])}"
            )
    return problems


# --------------------------------------------------------------------------- evaluators
def _no_rows(df: pd.DataFrame) -> pd.Series:
    return pd.Series(False, index=df.index)


def _any_null(df: pd.DataFrame, cols: list[str]) -> pd.Series:
    return pd.DataFrame({c: null_mask(df[c]) for c in cols}).any(axis=1)


def _eval_not_null(rule: Rule, df: pd.DataFrame, lookups) -> Evaluation:
    cols = rule.columns
    nulls = pd.DataFrame({c: null_mask(df[c]) for c in cols})
    fail = nulls.any(axis=1)

    def reason(i: int) -> str:
        bad = [c for c in cols if nulls.at[i, c]]
        return f"{', '.join(bad)} is null or blank"

    return fail, _no_rows(df), reason


def _eval_unique(rule: Rule, df: pd.DataFrame, lookups) -> Evaluation:
    cols = rule.columns
    skipped = _any_null(df, cols)
    sub = df.loc[~skipped, cols]
    keep = False if rule.params.get("keep", "first") == "all" else "first"
    dup = sub.duplicated(keep=keep)
    fail = _no_rows(df)
    fail.loc[dup.index[dup]] = True

    def reason(i: int) -> str:
        vals = [df.at[i, c] for c in cols]
        n = int((sub[cols] == vals).all(axis=1).sum())
        label = vals[0] if len(vals) == 1 else tuple(vals)
        return f"duplicate value {label!r} in {'+'.join(cols)} ({n} occurrences)"

    return fail, skipped, reason


def _eval_range(rule: Rule, df: pd.DataFrame, lookups) -> Evaluation:
    col = rule.columns[0]
    lo, hi = rule.params.get("min"), rule.params.get("max")
    skipped = null_mask(df[col])
    num = pd.to_numeric(df[col].where(~skipped), errors="coerce")
    nonnum = ~skipped & num.isna()
    below = (num < lo) if lo is not None else _no_rows(df)
    above = (num > hi) if hi is not None else _no_rows(df)
    fail = nonnum | below | above

    def reason(i: int) -> str:
        v = df.at[i, col]
        if nonnum.at[i]:
            return f"{col}={v!r} is not numeric"
        if below.at[i]:
            return f"{col}={v} is below min {lo}"
        return f"{col}={v} is above max {hi}"

    return fail, skipped, reason


def _eval_regex(rule: Rule, df: pd.DataFrame, lookups) -> Evaluation:
    col = rule.columns[0]
    pattern = re.compile(rule.params["pattern"])
    skipped = null_mask(df[col])
    text = df[col].where(~skipped, "").astype(str)      # nulls -> "" so the regex never sees NaN
    matches = text.map(lambda v: bool(pattern.fullmatch(v)))
    fail = ~skipped & ~matches

    def reason(i: int) -> str:
        return f"{col}={df.at[i, col]!r} does not match pattern {rule.params['pattern']!r}"

    return fail, skipped, reason


def _eval_date_format(rule: Rule, df: pd.DataFrame, lookups) -> Evaluation:
    from .schemas import DEFAULT_DATE_FORMATS

    col = rule.columns[0]
    formats = rule.params.get("formats") or DEFAULT_DATE_FORMATS
    skipped = null_mask(df[col])
    if pd.api.types.is_datetime64_any_dtype(df[col]):
        ok = pd.Series(True, index=df.index)
    else:
        text = df[col].astype(str)
        ok = _no_rows(df)
        for fmt in formats:
            ok = ok | pd.to_datetime(text, format=fmt, errors="coerce").notna()
    fail = ~skipped & ~ok

    def reason(i: int) -> str:
        return f"{col}={df.at[i, col]!r} matches none of the accepted formats {formats}"

    return fail, skipped, reason


def _eval_compare(rule: Rule, df: pd.DataFrame, lookups) -> Evaluation:
    a, b = rule.columns
    op = rule.params["operator"]
    na, nb = null_mask(df[a]), null_mask(df[b])
    skipped = na | nb
    x = pd.to_numeric(df[a].where(~na), errors="coerce")
    y = pd.to_numeric(df[b].where(~nb), errors="coerce")
    holds = _OPS[op](x, y)                      # NaN comparisons are False
    fail = ~skipped & ~holds
    nonnum = ~skipped & (x.isna() | y.isna())

    def reason(i: int) -> str:
        if nonnum.at[i]:
            return f"non-numeric value: {a}={df.at[i, a]!r}, {b}={df.at[i, b]!r}"
        return f"expected {a} {op} {b}, got {x.at[i]} vs {y.at[i]}"

    return fail, skipped, reason


def _eval_lookup(rule: Rule, df: pd.DataFrame, lookups: dict[str, pd.DataFrame]) -> Evaluation:
    col = rule.columns[0]
    key = normalize_lookup_name(rule.params["lookup_file"])
    lk_col = rule.params["lookup_column"]
    if key not in lookups:
        raise RuleExecutionError(f"lookup '{key}' was not provided")
    if lk_col not in lookups[key].columns:
        raise RuleExecutionError(f"lookup '{key}' has no column '{lk_col}'")
    valid = set(lookups[key][lk_col].dropna().astype(str))
    skipped = null_mask(df[col])
    fail = ~skipped & ~df[col].astype(str).isin(valid)

    def reason(i: int) -> str:
        return f"{col}={df.at[i, col]!r} not found in {key}.{lk_col}"

    return fail, skipped, reason


_EVALUATORS: dict[RuleType, Callable[..., Evaluation]] = {
    RuleType.NOT_NULL: _eval_not_null,
    RuleType.UNIQUE: _eval_unique,
    RuleType.RANGE: _eval_range,
    RuleType.REGEX: _eval_regex,
    RuleType.DATE_FORMAT: _eval_date_format,
    RuleType.CROSS_COLUMN_COMPARE: _eval_compare,
    RuleType.LOOKUP_IN: _eval_lookup,
}


# --------------------------------------------------------------------------- public API
def run_rule(
    rule: Rule,
    df: pd.DataFrame,
    lookups: dict[str, pd.DataFrame] | None = None,
    id_column: str | None = None,
    max_reasons: int = 10,
) -> RuleResult:
    """Run one rule. Never raises: failures to run are reported as status='error'."""
    df = df.reset_index(drop=True)              # row index == 0-based data-row position
    lk = {normalize_lookup_name(k): v for k, v in (lookups or {}).items()}
    total = len(df)
    try:
        problems = check_rule_columns(rule, df.columns, {k: list(v.columns) for k, v in lk.items()})
        if problems:
            raise RuleExecutionError("; ".join(problems))
        fail, skipped, reason_fn = _EVALUATORS[rule.type](rule, df, lk)
    except Exception as exc:  # noqa: BLE001 - one bad rule must not abort a whole run
        return RuleResult(rule_id=rule.rule_id, status="error", error=f"{type(exc).__name__}: {exc}", total_rows=total)

    fail = fail & ~skipped
    failed_idx = [int(i) for i in fail.index[fail]]
    if id_column and id_column in df.columns:
        ids = df[id_column].astype(str)
    else:
        ids = pd.Series(df.index.map(str), index=df.index)

    return RuleResult(
        rule_id=rule.rule_id,
        total_rows=total,
        fail_count=len(failed_idx),
        pass_count=total - len(failed_idx),
        skipped_count=int(skipped.sum()),
        failed_row_indices=failed_idx,
        failed_row_ids=[ids.at[i] for i in failed_idx],
        sample_reasons=[
            {"row_index": i, "row_id": ids.at[i], "reason": reason_fn(i)} for i in failed_idx[:max_reasons]
        ],
    )


def run_rules(
    rules: list[Rule],
    df: pd.DataFrame,
    lookups: dict[str, pd.DataFrame] | None = None,
    id_column: str | None = None,
) -> list[RuleResult]:
    return [run_rule(r, df, lookups, id_column) for r in rules]
