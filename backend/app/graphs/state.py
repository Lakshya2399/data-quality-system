"""LangGraph state definitions. (No `from __future__ import annotations` here: LangGraph
inspects these annotations at runtime to find the reducers.)"""
import operator
from typing import Annotated, Any, TypedDict

import pandas as pd

from ..agent_schemas import DataModel, Rating
from ..schemas import Rule, RuleResult


class PipelineState(TypedDict, total=False):
    # inputs
    run_id: str
    target_table: str                        # table name of the sample (rules are written against it)
    sample_df: pd.DataFrame
    lookups: dict[str, pd.DataFrame]         # lookup table name -> dataframe
    # produced by nodes
    profile: dict
    data_model: DataModel
    id_column: str | None                    # primary key used to label failing rows
    rules: list[Rule]
    dry_runs: dict[str, RuleResult]          # rule_id -> result on the clean sample
    feedback: str | None                     # dry-run feedback for the next rule_builder attempt
    attempt: int                             # rule_builder invocations so far
    builder_failed: bool                     # builder gave up (kept earlier rules or fell back)
    used_fallback: bool
    ratings: list[Rating]
    warnings: Annotated[list[str], operator.add]   # nodes append; LangGraph concatenates


class ExecutionState(TypedDict, total=False):
    # inputs
    run_id: str
    exec_id: int
    target_df: pd.DataFrame
    # produced by nodes
    selected: list[dict[str, Any]]           # [{"pk": int, "rule": Rule, "score": int}] rated >= threshold
    lookups: dict[str, pd.DataFrame]
    id_column: str | None
    results: list[tuple[int, RuleResult]]    # (rule_pk, result)
    warnings: Annotated[list[str], operator.add]
