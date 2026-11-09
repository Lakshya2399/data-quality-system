"""Agent 2: DQ Rule Builder.

The LLM writes flat rule drafts; code converts them into the validated Rule DSL, assigns the
rule ids (R1..Rn by position), and checks the whole set statically: columns exist, count in
range, no duplicates, all three categories covered. Problems are fed back to the LLM
immediately. Dry-running on the clean sample happens later in the graph, which can call this
agent again with `feedback`.
"""
from __future__ import annotations

import json

import pandas as pd
from pydantic import ValidationError

from ..agent_schemas import DataModel, RuleDraft, RuleDraftList
from ..llm import LLM, format_validation_error
from ..prompts import render
from ..rule_engine import check_rule_columns
from ..schemas import Rule, RuleType

AGENT = "rule_builder"
SINGLE_COLUMN_TYPES = {RuleType.NOT_NULL, RuleType.UNIQUE, RuleType.RANGE, RuleType.REGEX, RuleType.DATE_FORMAT}


def sample_rows_from_df(df: pd.DataFrame, n: int = 5) -> list[dict]:
    """First n rows as JSON-safe dicts (NaN becomes null)."""
    return json.loads(df.head(n).to_json(orient="records"))


def drafts_to_rules(drafts: list[RuleDraft]) -> tuple[list[Rule], list[str]]:
    """Convert drafts to Rules. Ids are assigned by position (R1, R2, ...). Returns (rules, problems)."""
    rules, problems = [], []
    for i, d in enumerate(drafts, start=1):
        try:
            rules.append(d.to_rule(f"R{i}"))
        except ValidationError as exc:
            problems.append(f"rule #{i} ({d.name}, type {d.type.value}): {format_validation_error(exc)}")
    return rules, problems


def check_rule_set(
    rules: list[Rule],
    target_columns: list[str],
    lookup_columns: dict[str, list[str]],
    *,
    min_rules: int = 5,
    max_rules: int = 8,
    require_coverage: bool = True,
) -> list[str]:
    problems: list[str] = []
    if not (min_rules <= len(rules) <= max_rules):
        problems.append(f"write between {min_rules} and {max_rules} valid rules; got {len(rules)}")

    names: dict[str, str] = {}
    defs: dict[str, str] = {}
    for r in rules:
        tag = f"{r.rule_id} ({r.name})"
        problems += [f"{tag}: {p}" for p in check_rule_columns(r, target_columns, lookup_columns)]
        if r.name in names:
            problems.append(f"{tag}: duplicate rule name (already used by {names[r.name]})")
        names.setdefault(r.name, r.rule_id)
        key = json.dumps([r.type.value, r.columns, r.params], sort_keys=True)
        if key in defs:
            problems.append(f"{tag}: identical to {defs[key]}; remove the duplicate")
        defs.setdefault(key, r.rule_id)

    if require_coverage:
        types = {r.type for r in rules}
        if not types & SINGLE_COLUMN_TYPES:
            problems.append("coverage: no single-column rule (not_null, unique, range, regex, date_format)")
        if RuleType.CROSS_COLUMN_COMPARE not in types:
            problems.append("coverage: no cross_column_compare rule comparing two columns of the target table")
        if lookup_columns and RuleType.LOOKUP_IN not in types:
            problems.append(f"coverage: no lookup_in rule against a lookup table ({sorted(lookup_columns)})")
    return problems


def build_rules(
    llm: LLM,
    model: DataModel,
    sample_rows: list[dict],
    target_table: str,
    *,
    feedback: str | None = None,
    min_rules: int = 5,
    max_rules: int = 8,
) -> list[Rule]:
    """Return 5-8 validated rules for `target_table`. Raises LLMOutputError if the LLM cannot comply.

    `feedback` is free text from a previous attempt (e.g. dry-run failures) and is shown to the LLM.
    """
    target_columns = model.columns_of(target_table)
    if not target_columns:
        raise ValueError(f"target table '{target_table}' not in data model {model.table_names()}")
    lookup_columns = {t.name: [c.name for c in t.columns] for t in model.tables if t.name != target_table}

    feedback_section = (
        f"\n## Feedback on your previous attempt (fix these)\n{feedback.strip()}\n" if feedback else ""
    )
    prompt = render(
        "rule_builder",
        target_table=target_table,
        model=json.dumps(model.model_dump(mode="json"), separators=(",", ":")),
        target_columns=", ".join(target_columns),
        lookup_tables="\n".join(f"- {n}: {', '.join(c)}" for n, c in lookup_columns.items()) or "(none)",
        sample_rows=json.dumps(sample_rows, separators=(",", ":")),
        min_rules=str(min_rules),
        max_rules=str(max_rules),
        feedback_section=feedback_section,
    )

    def validator(draft_list: RuleDraftList) -> list[str]:
        rules, problems = drafts_to_rules(draft_list.rules)
        return problems + check_rule_set(rules, target_columns, lookup_columns, min_rules=min_rules, max_rules=max_rules)

    result = llm.generate_json(prompt, RuleDraftList, validator=validator, agent=AGENT)
    rules, _ = drafts_to_rules(result.rules)      # validator guaranteed there are no problems
    return rules
