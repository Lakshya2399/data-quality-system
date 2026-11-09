"""Graph A - the sample pipeline.

  profile -> model_agent -> rule_builder -> validate_rules (dry-run on the clean sample)
                               ^                  |
                               |--- retry --------+--- continue --> rate_rules -> persist -> END

Retry policy: after each rule_builder call the rules are dry-run on the clean sample. If any
rule errors or fails on clean data, the builder is called again with that evidence as feedback,
up to `max_rule_retries` extra times. Rules still failing after that are kept; the rater caps
them low.

Degraded modes (each adds a visible warning to the run, none is silent):
  * rule_builder cannot produce valid output  -> keep the previous attempt's rules, else fall back
    to the deterministic default rules that fit the schema
  * rater cannot produce valid output         -> ratings derived from rule severity
A failure of the model agent (or an unreachable LLM) fails the run.
"""
from __future__ import annotations

import logging
from pathlib import Path

from langgraph.graph import END, START, StateGraph

from .. import db
from ..agent_schemas import DataModel, RatingItem
from ..agents.model_agent import build_data_model
from ..agents.rule_builder import build_rules, sample_rows_from_df
from ..agents.rule_rater import apply_caps, rate_rules, rating_warnings
from ..config import settings
from ..default_rules import default_rules
from ..llm import LLM, LLMOutputError
from ..profiling import profile_tables
from ..rule_engine import check_rule_columns, run_rule
from ..schemas import Rule, RuleResult
from .common import primary_key_column
from .state import PipelineState

log = logging.getLogger(__name__)
SEVERITY_SCORE = {"high": 4, "medium": 3, "low": 2}


def dry_run_feedback(rules: list[Rule], dry_runs: dict[str, RuleResult]) -> str | None:
    """Evidence for the next rule_builder attempt, or None if every rule ran cleanly."""
    lines = []
    for r in rules:
        d = dry_runs[r.rule_id]
        what = f"{r.name} ({r.type.value} on {', '.join(r.columns)}, params {r.params})"
        if d.status == "error":
            lines.append(f"- {what} could not be executed: {d.error}")
        elif d.fail_count:
            examples = "; ".join(x["reason"] for x in d.sample_reasons[:2])
            lines.append(f"- {what} FAILS on {d.fail_count} of {d.total_rows} rows of the clean sample, e.g. {examples}")
    if not lines:
        return None
    return (
        "When your previous rules were executed on the clean sample, these misbehaved:\n"
        + "\n".join(lines)
        + "\nThe sample is clean, so these rules are wrong or too strict. Rewrite or replace them "
        "and keep the other rules unchanged."
    )


def applicable_default_rules(target_columns: list[str], lookup_columns: dict[str, list[str]]) -> list[Rule]:
    """The deterministic fallback rules whose columns and lookups exist in this schema."""
    return [r for r in default_rules() if not check_rule_columns(r, target_columns, lookup_columns)]


def severity_ratings(rules: list[Rule], dry_runs: dict[str, RuleResult]):
    """Degraded-mode ratings when the rater LLM is unusable: score from severity, caps still applied."""
    items = [
        RatingItem(
            rule_id=r.rule_id,
            score=SEVERITY_SCORE[r.severity],
            justification=f"LLM rating unavailable; score derived from severity '{r.severity}'.",
        )
        for r in rules
    ]
    return apply_caps(items, rules, dry_runs)


def build_pipeline_graph(
    llm: LLM,
    db_path: Path | str | None = None,
    *,
    max_rule_retries: int | None = None,
    rating_threshold: int | None = None,
):
    max_retries = settings.max_rule_retries if max_rule_retries is None else max_rule_retries
    threshold = settings.rating_threshold if rating_threshold is None else rating_threshold

    # ------------------------------------------------------------------ nodes
    def profile_node(state: PipelineState) -> dict:
        tables = {state["target_table"]: state["sample_df"], **state["lookups"]}
        return {"profile": profile_tables(tables)}

    def model_node(state: PipelineState) -> dict:
        model = build_data_model(llm, state["profile"])
        return {"data_model": model, "id_column": primary_key_column(model, state["target_table"])}

    def rule_builder_node(state: PipelineState) -> dict:
        attempt = state.get("attempt", 0) + 1
        model: DataModel = state["data_model"]
        target = state["target_table"]
        try:
            rules = build_rules(
                llm, model, sample_rows_from_df(state["sample_df"]), target, feedback=state.get("feedback")
            )
            return {"rules": rules, "attempt": attempt, "builder_failed": False}
        except LLMOutputError as exc:
            log.warning("rule_builder attempt %d failed: %s", attempt, exc)
            if state.get("rules"):
                return {
                    "attempt": attempt, "builder_failed": True,
                    "warnings": [f"Rule builder could not improve its rules on attempt {attempt} "
                                 f"({exc.problems[0] if exc.problems else 'invalid output'}); kept the previous attempt's rules."],
                }
            lookup_cols = {t.name: [c.name for c in t.columns] for t in model.tables if t.name != target}
            fallback = applicable_default_rules(model.columns_of(target), lookup_cols)
            if not fallback:
                raise
            return {
                "rules": fallback, "attempt": attempt, "builder_failed": True, "used_fallback": True,
                "warnings": [f"Rule builder produced no valid rules after retries ({exc.problems[0] if exc.problems else 'invalid output'}); "
                             f"used {len(fallback)} deterministic default rules instead."],
            }

    def validate_rules_node(state: PipelineState) -> dict:
        lookups = state["lookups"]
        rules = state["rules"]
        dry = {r.rule_id: run_rule(r, state["sample_df"], lookups, state.get("id_column")) for r in rules}
        return {"dry_runs": dry, "feedback": dry_run_feedback(rules, dry)}

    def route_after_validate(state: PipelineState) -> str:
        if state.get("builder_failed") or not state.get("feedback"):
            return "continue"
        return "retry" if state["attempt"] <= max_retries else "continue"

    def rate_rules_node(state: PipelineState) -> dict:
        rules, dry = state["rules"], state["dry_runs"]
        warnings: list[str] = []
        try:
            ratings = rate_rules(llm, rules, dry, state["data_model"])
        except LLMOutputError as exc:
            log.warning("rule_rater failed: %s", exc)
            ratings = severity_ratings(rules, dry)
            warnings.append("Rater produced no valid ratings after retries; scores were derived from rule severity "
                            "(treat them as provisional).")
        names = {r.rule_id: r.name for r in rules}
        warnings += [f"{x.rule_id} ({names[x.rule_id]}): {x.adjustment}" for x in ratings if x.adjustment]
        warnings += rating_warnings(ratings, threshold)
        return {"ratings": ratings, "warnings": warnings}

    def persist_node(state: PipelineState) -> dict:
        warnings = state.get("warnings", [])
        with db.get_db(db_path) as conn:
            db.save_data_model(conn, state["run_id"], state["data_model"].model_dump(mode="json"), state["profile"])
            pks = db.save_rules(conn, state["run_id"], state["rules"], state["dry_runs"])
            for r in state["ratings"]:
                db.save_rating(conn, pks[r.rule_id], r.score, r.justification)
            db.set_run_status(conn, state["run_id"], "completed", warnings=warnings)
        return {}

    # ------------------------------------------------------------------ wiring
    g = StateGraph(PipelineState)
    g.add_node("profile_node", profile_node)
    g.add_node("model_node", model_node)
    g.add_node("rule_builder_node", rule_builder_node)
    g.add_node("validate_rules_node", validate_rules_node)
    g.add_node("rate_rules_node", rate_rules_node)
    g.add_node("persist_node", persist_node)

    g.add_edge(START, "profile_node")
    g.add_edge("profile_node", "model_node")
    g.add_edge("model_node", "rule_builder_node")
    g.add_edge("rule_builder_node", "validate_rules_node")
    g.add_conditional_edges(
        "validate_rules_node", route_after_validate, {"retry": "rule_builder_node", "continue": "rate_rules_node"}
    )
    g.add_edge("rate_rules_node", "persist_node")
    g.add_edge("persist_node", END)
    return g.compile()
