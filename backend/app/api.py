"""HTTP API.

  POST /pipeline/run              upload sample + lookup CSVs (or none, to use the defaults); starts Graph A
  GET  /runs                      list runs                      GET /runs/{id}              one run (poll its status)
  GET  /runs/{id}/model           inferred data model            GET /runs/{id}/rules        rules + ratings + dry-run stats
  POST /runs/{id}/execute         upload a prod CSV (or none, for the default); starts Graph B
  GET  /runs/{id}/executions      executions of a run            GET /executions/{exec_id}   per-rule results

The two POSTs that start slow work answer 202 immediately and run in the background; poll the
returned `poll_url` until status is 'completed' or 'failed'. Add `?wait=true` to block instead.
Pipeline/execution failures are data, not HTTP errors: they appear as status='failed' with `error`.
"""
import io
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pandas as pd
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, Request, Response, UploadFile
from starlette.concurrency import run_in_threadpool

from . import db, services, storage
from .api_schemas import (
    ExecutionListItem, ExecutionOut, ExecutionStarted, ModelOut, RuleOut, RunOut, RulesOut, StartedRun,
)
from .config import settings
from .data_io import load_csv
from .llm import LLM, LLMError

log = logging.getLogger(__name__)
router = APIRouter()

DEFAULT_SAMPLE, DEFAULT_LOOKUP, DEFAULT_PROD = "sample_orders.csv", "valid_regions.csv", "prod_orders.csv"
PARSE_ERRORS = (pd.errors.ParserError, pd.errors.EmptyDataError, UnicodeDecodeError)


@dataclass
class ApiConfig:
    llm_factory: Callable[[], LLM]
    db_path: Path | None
    workdir: Path | None
    data_dir: Path
    max_upload_bytes: int
    _ready: bool = field(default=False, init=False, repr=False)

    def conn(self):
        """DB connection context manager; creates the schema on first use."""
        if not self._ready:
            db.init_db(self.db_path)
            self._ready = True
        return db.get_db(self.db_path)


def get_config(request: Request) -> ApiConfig:
    return request.app.state.config


# --------------------------------------------------------------------------- helpers
def _bad_csv(label: str, why: str) -> HTTPException:
    return HTTPException(400, f"{label}: {why}")


def _parse_csv(data: bytes, label: str) -> pd.DataFrame:
    if not data.strip():
        raise _bad_csv(label, "the file is empty")
    try:
        df = load_csv(io.BytesIO(data))
    except PARSE_ERRORS as exc:
        raise _bad_csv(label, f"not a readable CSV ({str(exc)[:200]})") from exc
    if df.empty:
        raise _bad_csv(label, "the file has a header but no data rows")
    return df


def _provided(file: UploadFile | None) -> bool:
    return file is not None and bool(file.filename)


async def _read_upload(file: UploadFile, label: str, cfg: ApiConfig) -> tuple[str, pd.DataFrame]:
    name = file.filename
    if not name.lower().endswith(".csv"):
        raise _bad_csv(label, f"the file name must end with .csv (got {name!r})")
    data = await file.read(cfg.max_upload_bytes + 1)
    if len(data) > cfg.max_upload_bytes:
        raise HTTPException(413, f"{label}: larger than the {cfg.max_upload_bytes // (1024 * 1024)} MB upload limit")
    return name, _parse_csv(data, label)


def _default_csv(cfg: ApiConfig, filename: str, label: str) -> tuple[str, pd.DataFrame]:
    path = cfg.data_dir / filename
    if not path.exists():
        raise _bad_csv(label, f"no file uploaded and the default {path} does not exist "
                              f"(upload a CSV, or run scripts/generate_data.py)")
    return filename, _parse_csv(path.read_bytes(), f"{label} (default {filename})")


def _make_llm(cfg: ApiConfig) -> LLM:
    try:
        return cfg.llm_factory()
    except LLMError as exc:
        raise HTTPException(503, f"LLM unavailable: {exc}") from exc


def _get_run(conn, run_id: str) -> dict:
    run = db.get_run(conn, run_id)
    if run is None:
        raise HTTPException(404, f"unknown run '{run_id}'")
    return run


def _require_completed(run: dict, what: str) -> None:
    if run["status"] != "completed":
        detail = f"run is '{run['status']}', so the {what} is not available"
        if run["error"]:
            detail += f": {run['error']}"
        raise HTTPException(409, detail)


# --------------------------------------------------------------------------- background tasks
def _pipeline_task(cfg: ApiConfig, run_id: str, llm: LLM) -> None:
    try:
        services.execute_pipeline(run_id, llm, db_path=cfg.db_path, workdir=cfg.workdir)
    except Exception:  # noqa: BLE001 - services record pipeline failures; this guards infrastructure errors
        log.exception("background pipeline crashed for run %s", run_id)


def _execution_task(cfg: ApiConfig, exec_id: int, min_score: int | None) -> None:
    try:
        services.execute_rules(exec_id, db_path=cfg.db_path, workdir=cfg.workdir, rating_threshold=min_score)
    except Exception:  # noqa: BLE001
        log.exception("background execution crashed for exec %s", exec_id)


# --------------------------------------------------------------------------- pipeline
@router.post("/pipeline/run", response_model=StartedRun, status_code=202, tags=["pipeline"])
async def start_pipeline(
    background: BackgroundTasks,
    response: Response,
    cfg: ApiConfig = Depends(get_config),
    sample_file: UploadFile | None = File(None, description="clean sample CSV (e.g. sample_orders.csv)"),
    lookup_file: UploadFile | None = File(None, description="lookup CSV (e.g. valid_regions.csv)"),
    wait: bool = Query(False, description="block until the pipeline finishes (200) instead of returning 202"),
):
    """Upload both files, or neither to use the default sample and lookup from the data directory."""
    have_sample, have_lookup = _provided(sample_file), _provided(lookup_file)
    if have_sample != have_lookup:
        raise HTTPException(422, "provide both sample_file and lookup_file, or neither to use the default files")

    if have_sample:
        sample_name, sample_df = await _read_upload(sample_file, "sample_file", cfg)
        lookup_name, lookup_df = await _read_upload(lookup_file, "lookup_file", cfg)
    else:
        sample_name, sample_df = _default_csv(cfg, DEFAULT_SAMPLE, "sample_file")
        lookup_name, lookup_df = _default_csv(cfg, DEFAULT_LOOKUP, "lookup_file")

    if storage.table_name(sample_name) == storage.table_name(lookup_name):
        raise HTTPException(400, f"sample and lookup files must have different names (both are '{sample_name}')")
    llm = _make_llm(cfg)                                    # inputs are valid; fail fast (503) before creating a run
    try:
        run_id = await run_in_threadpool(
            services.create_run, sample_df, sample_name, {lookup_name: lookup_df},
            db_path=cfg.db_path, workdir=cfg.workdir)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    if wait:
        run = await run_in_threadpool(
            services.execute_pipeline, run_id, llm, db_path=cfg.db_path, workdir=cfg.workdir)
        response.status_code = 200
        return StartedRun(run_id=run_id, status=run["status"], poll_url=f"/runs/{run_id}")
    background.add_task(_pipeline_task, cfg, run_id, llm)
    return StartedRun(run_id=run_id, status="created", poll_url=f"/runs/{run_id}")


# --------------------------------------------------------------------------- runs
@router.get("/runs", response_model=list[RunOut], tags=["runs"])
def list_runs(limit: int = Query(50, ge=1, le=200), cfg: ApiConfig = Depends(get_config)):
    with cfg.conn() as conn:
        return db.list_runs(conn)[:limit]


@router.get("/runs/{run_id}", response_model=RunOut, tags=["runs"])
def get_run(run_id: str, cfg: ApiConfig = Depends(get_config)):
    with cfg.conn() as conn:
        run = _get_run(conn, run_id)
        run["rule_count"] = conn.execute("SELECT COUNT(*) FROM rules WHERE run_id = ?", (run_id,)).fetchone()[0]
        run["execution_count"] = conn.execute("SELECT COUNT(*) FROM executions WHERE run_id = ?", (run_id,)).fetchone()[0]
        return run


@router.get("/runs/{run_id}/model", response_model=ModelOut, tags=["runs"])
def get_model(run_id: str, include_profile: bool = False, cfg: ApiConfig = Depends(get_config)):
    with cfg.conn() as conn:
        _require_completed(_get_run(conn, run_id), "data model")
        row = db.get_data_model(conn, run_id)
    if row is None:
        raise HTTPException(404, "no data model stored for this run")
    return ModelOut(run_id=run_id, created_at=row["created_at"], model=row["model"],
                    profile=row["profile"] if include_profile else None)


@router.get("/runs/{run_id}/rules", response_model=RulesOut, tags=["runs"])
def get_rules(
    run_id: str,
    min_score: int | None = Query(None, ge=1, le=5, description="rules scoring at least this will execute (default 4)"),
    cfg: ApiConfig = Depends(get_config),
):
    threshold = settings.rating_threshold if min_score is None else min_score
    with cfg.conn() as conn:
        run = _get_run(conn, run_id)
        _require_completed(run, "rules")
        rows = db.list_rules(conn, run_id)
    rules = [RuleOut.model_validate({**r, "will_execute": r["score"] is not None and r["score"] >= threshold})
             for r in rows]
    distribution = {str(i): sum(1 for r in rules if r.score == i) for i in range(1, 6)}
    return RulesOut(
        run_id=run_id, status=run["status"], min_score=threshold, rules=rules, warnings=run["warnings"],
        executable_count=sum(r.will_execute for r in rules), score_distribution=distribution,
    )


@router.get("/runs/{run_id}/executions", response_model=list[ExecutionListItem], tags=["runs"])
def list_run_executions(run_id: str, cfg: ApiConfig = Depends(get_config)):
    with cfg.conn() as conn:
        _get_run(conn, run_id)
        return db.list_executions(conn, run_id)


# --------------------------------------------------------------------------- execution
@router.post("/runs/{run_id}/execute", response_model=ExecutionStarted, status_code=202, tags=["execution"])
async def start_execution(
    run_id: str,
    background: BackgroundTasks,
    response: Response,
    cfg: ApiConfig = Depends(get_config),
    target_file: UploadFile | None = File(None, description="production CSV; omit to use the default prod_orders.csv"),
    min_score: int | None = Query(None, ge=1, le=5, description="execute rules rated at least this (default 4)"),
    wait: bool = Query(False, description="block until the execution finishes (200) instead of returning 202"),
):
    with cfg.conn() as conn:
        _require_completed(_get_run(conn, run_id), "rules to execute")

    if _provided(target_file):
        name, df = await _read_upload(target_file, "target_file", cfg)
    else:
        name, df = _default_csv(cfg, DEFAULT_PROD, "target_file")

    try:
        exec_id = await run_in_threadpool(
            services.start_execution, run_id, df, name, db_path=cfg.db_path, workdir=cfg.workdir)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    poll = f"/executions/{exec_id}"
    if wait:
        ex = await run_in_threadpool(
            services.execute_rules, exec_id, db_path=cfg.db_path, workdir=cfg.workdir, rating_threshold=min_score)
        response.status_code = 200
        return ExecutionStarted(exec_id=exec_id, run_id=run_id, status=ex["status"], poll_url=poll)
    background.add_task(_execution_task, cfg, exec_id, min_score)
    return ExecutionStarted(exec_id=exec_id, run_id=run_id, status="running", poll_url=poll)


@router.get("/executions/{exec_id}", response_model=ExecutionOut, tags=["execution"])
def get_execution(exec_id: int, cfg: ApiConfig = Depends(get_config)):
    with cfg.conn() as conn:
        ex = db.get_execution(conn, exec_id)
    if ex is None:
        raise HTTPException(404, f"unknown execution {exec_id}")
    results = ex["results"]
    ex["summary"] = {
        "rules_executed": len(results),
        "rules_with_failures": sum(1 for r in results if r["status"] == "ok" and (r["fail_count"] or 0) > 0),
        "rules_errored": sum(1 for r in results if r["status"] == "error"),
    }
    return ex
