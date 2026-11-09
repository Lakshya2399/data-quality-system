"""Agent 3: Rule Rater / Critic.

The LLM scores each rule 1-5 using the rubric, the data model and the rule's dry-run on the
clean sample. Two caps are then enforced in code so a lenient model cannot over-rate a broken
rule: a rule that cannot execute scores 1; a rule that fails on clean data scores at most 2.
"""
from __future__ import annotations

import json

from ..agent_schemas import DataModel, Rating, RatingItem, RatingList
from ..llm import LLM
from ..prompts import render
from ..schemas import Rule, RuleResult

AGENT = "rule_rater"
FAIL_ON_CLEAN_CAP = 2
ERROR_CAP = 1


def _rule_view(rule: Rule, dry: RuleResult | None) -> dict:
    view: dict = {
        "rule_id": rule.rule_id, "name": rule.name, "type": rule.type.value, "columns": rule.columns,
        "params": rule.params, "severity": rule.severity, "description": rule.description,
    }
    if dry is None:
        view["dry_run"] = "not run"
    elif dry.status == "error":
        view["dry_run"] = {"status": "error", "error": dry.error}
    else:
        view["dry_run"] = {
            "status": "ok", "rows": dry.total_rows, "failed": dry.fail_count, "skipped": dry.skipped_count,
            "example_failures": [r["reason"] for r in dry.sample_reasons[:3]],
        }
    return view


def check_ratings(items: RatingList, rules: list[Rule]) -> list[str]:
    expected = [r.rule_id for r in rules]
    got = [i.rule_id for i in items.ratings]
    problems = []
    missing = [r for r in expected if r not in got]
    unknown = sorted({g for g in got if g not in expected})
    dups = sorted({g for g in got if got.count(g) > 1})
    if missing:
        problems.append(f"missing ratings for {missing}")
    if unknown:
        problems.append(f"unknown rule_ids {unknown}; valid ids are {expected}")
    if dups:
        problems.append(f"duplicate ratings for {dups}; rate each rule exactly once")
    return problems


def apply_caps(items: list[RatingItem], rules: list[Rule], dry_runs: dict[str, RuleResult]) -> list[Rating]:
    by_id = {i.rule_id: i for i in items}
    out: list[Rating] = []
    for rule in rules:
        item = by_id[rule.rule_id]
        score, note = item.score, None
        dry = dry_runs.get(rule.rule_id)
        if dry is not None and dry.status == "error":
            if score > ERROR_CAP:
                score, note = ERROR_CAP, f"capped to {ERROR_CAP}: rule could not be executed ({dry.error})"
        elif dry is not None and dry.fail_count > 0 and score > FAIL_ON_CLEAN_CAP:
            score = FAIL_ON_CLEAN_CAP
            note = f"capped to {FAIL_ON_CLEAN_CAP}: fails on {dry.fail_count} of {dry.total_rows} rows of the clean sample"
        out.append(
            Rating(
                rule_id=rule.rule_id, score=score, llm_score=item.score, adjustment=note,
                justification=item.justification.strip() + (f" [{note}]" if note else ""),
            )
        )
    return out


def rating_warnings(ratings: list[Rating], threshold: int = 4) -> list[str]:
    """Soft problems worth surfacing in the UI (they do not fail the run)."""
    warnings = []
    if len(ratings) >= 3 and len({r.score for r in ratings}) == 1:
        warnings.append(f"All {len(ratings)} rules received the same score ({ratings[0].score}); ratings are not discriminating.")
    if not any(r.score >= threshold for r in ratings):
        warnings.append(f"No rule scored {threshold} or higher, so nothing will be executed on the production file.")
    return warnings


def rate_rules(llm: LLM, rules: list[Rule], dry_runs: dict[str, RuleResult], model: DataModel) -> list[Rating]:
    """Rate every rule. Raises LLMOutputError if the LLM never returns a complete, valid rating set."""
    prompt = render(
        "rule_rater",
        model_summary=model.summary_text(),
        rules=json.dumps([_rule_view(r, dry_runs.get(r.rule_id)) for r in rules], indent=1),
    )
    result = llm.generate_json(prompt, RatingList, validator=lambda x: check_ratings(x, rules), agent=AGENT)
    return apply_caps(result.ratings, rules, dry_runs)
