"""Service layer: the functions the CLI (and, in Phase 5, FastAPI) call.

Each flow is split in two so an API can respond immediately and run the slow part in the background:
    create_run(...)      -> run_id            (fast: stores inputs, creates the run row)
    execute_pipeline(..) -> run row           (slow: Graph A; LLM calls)
    start_execution(...) -> exec_id           (fast: stores the prod file, creates the execution row)
    execute_rules(...)   -> execution dict    (Graph B)
Pipeline/execution failures never raise: they are recorded as status='failed' with the error text
in SQLite. Only invalid inputs (unknown run, wrong state, empty file) raise.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from . import db, storage
from .graphs.execution import build_execution_graph
from .graphs.pipeline import build_pipeline_graph
from .llm import LLM

log = logging.getLogger(__name__)
MAX_ERROR_CHARS = 2000


def _audit_hook(run_id: str, db_path):
    def hook(agent: str, prompt: str, response: str) -> None:
        with db.get_db(db_path) as conn:
            db.log_llm_call(conn, run_id, agent, prompt, response)

    return hook


def _error_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]


# --------------------------------------------------------------------------- sample pipeline
def create_run(
    sample_df: pd.DataFrame,
    sample_name: str,
    lookups: dict[str, pd.DataFrame],
    *,
    db_path: Path | str | None = None,
    workdir: Path | str | None = None,
) -> str:
    if sample_df.empty:
        raise ValueError("the sample file has no rows")
    sample_table = storage.table_name(sample_name)
    lk = {storage.table_name(name): df for name, df in lookups.items()}
    if sample_table in lk:
        raise ValueError(f"sample and lookup files must have different names (both are '{sample_table}')")

    db.init_db(db_path)
    with db.get_db(db_path) as conn:
        run_id = db.new_run(conn, f"{sample_table}.csv", ",".join(f"{name}.csv" for name in lk))
    directory = storage.run_dir(run_id, workdir)
    storage.save_table(directory, sample_table, sample_df)
    for name, df in lk.items():
        storage.save_table(directory, name, df)
    return run_id


def execute_pipeline(
    run_id: str,
    llm: LLM,
    *,
    db_path: Path | str | None = None,
    workdir: Path | str | None = None,
    max_rule_retries: int | None = None,
    rating_threshold: int | None = None,
) -> dict:
    """Run Graph A for a run created by create_run. Returns the run row (check ['status'])."""
    db.init_db(db_path)
    with db.get_db(db_path) as conn:
        run = db.get_run(conn, run_id)
        if run is None:
            raise KeyError(f"unknown run {run_id}")
        if run["status"] != "created":
            raise ValueError(f"run {run_id} is '{run['status']}', expected 'created'")
        db.set_run_status(conn, run_id, "running")

    try:
        directory = storage.run_dir(run_id, workdir)
        target = storage.table_name(run["sample_file"])
        lookups = {storage.table_name(n): storage.load_table(directory, n)
                   for n in run["lookup_file"].split(",") if n}
        scoped = llm.with_audit(_audit_hook(run_id, db_path)) if hasattr(llm, "with_audit") else llm
        graph = build_pipeline_graph(scoped, db_path, max_rule_retries=max_rule_retries, rating_threshold=rating_threshold)
        graph.invoke({
            "run_id": run_id,
            "target_table": target,
            "sample_df": storage.load_table(directory, target),
            "lookups": lookups,
        })
    except Exception as exc:  # noqa: BLE001 - recorded, not raised (see module docstring)
        log.exception("pipeline failed for run %s", run_id)
        with db.get_db(db_path) as conn:
            db.set_run_status(conn, run_id, "failed", error=_error_text(exc))

    with db.get_db(db_path) as conn:
        return db.get_run(conn, run_id)


def run_sample_pipeline(
    sample_df: pd.DataFrame,
    sample_name: str,
    lookups: dict[str, pd.DataFrame],
    llm: LLM,
    **kwargs,
) -> dict:
    """create_run + execute_pipeline in one call."""
    paths = {k: kwargs.pop(k) for k in ("db_path", "workdir") if k in kwargs}
    run_id = create_run(sample_df, sample_name, lookups, **paths)
    return execute_pipeline(run_id, llm, **paths, **kwargs)


# --------------------------------------------------------------------------- execution on prod data
def start_execution(
    run_id: str,
    target_df: pd.DataFrame,
    target_name: str,
    *,
    db_path: Path | str | None = None,
    workdir: Path | str | None = None,
) -> int:
    if target_df.empty:
        raise ValueError("the target file has no rows")
    db.init_db(db_path)
    with db.get_db(db_path) as conn:
        run = db.get_run(conn, run_id)
        if run is None:
            raise KeyError(f"unknown run {run_id}")
        if run["status"] != "completed":
            raise ValueError(f"run {run_id} is '{run['status']}'; the sample pipeline must complete first")
        exec_id = db.new_execution(conn, run_id, f"{storage.table_name(target_name)}.csv")
    try:
        storage.save_table(storage.run_dir(run_id, workdir) / "executions" / str(exec_id), target_name, target_df)
    except Exception as exc:  # noqa: BLE001
        with db.get_db(db_path) as conn:
            db.finish_execution(conn, exec_id, "failed", error=_error_text(exc))
        raise
    return exec_id


def execute_rules(
    exec_id: int,
    *,
    db_path: Path | str | None = None,
    workdir: Path | str | None = None,
    rating_threshold: int | None = None,
) -> dict:
    """Run Graph B for an execution created by start_execution. Returns the execution with its results."""
    db.init_db(db_path)
    with db.get_db(db_path) as conn:
        ex = db.get_execution(conn, exec_id)
    if ex is None:
        raise KeyError(f"unknown execution {exec_id}")
    if ex["status"] != "running":
        raise ValueError(f"execution {exec_id} is '{ex['status']}', expected 'running'")

    try:
        directory = storage.run_dir(ex["run_id"], workdir) / "executions" / str(exec_id)
        target_df = storage.load_table(directory, ex["target_file"])
        graph = build_execution_graph(db_path, workdir, rating_threshold=rating_threshold)
        graph.invoke({"run_id": ex["run_id"], "exec_id": exec_id, "target_df": target_df})
    except Exception as exc:  # noqa: BLE001
        log.exception("execution %s failed", exec_id)
        with db.get_db(db_path) as conn:
            db.finish_execution(conn, exec_id, "failed", error=_error_text(exc))

    with db.get_db(db_path) as conn:
        return db.get_execution(conn, exec_id)


def run_execution(run_id: str, target_df: pd.DataFrame, target_name: str, **kwargs) -> dict:
    """start_execution + execute_rules in one call."""
    paths = {k: kwargs.pop(k) for k in ("db_path", "workdir") if k in kwargs}
    exec_id = start_execution(run_id, target_df, target_name, **paths)
    return execute_rules(exec_id, **paths, **kwargs)
