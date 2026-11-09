"""SQLite schema + small repository helpers (plain sqlite3, no ORM).

Everything hangs off runs.run_id, and child rows are deleted with their run (ON DELETE CASCADE).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import settings
from .schemas import Rule, RuleResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    sample_file  TEXT,
    lookup_file  TEXT,
    status       TEXT NOT NULL DEFAULT 'created'
                 CHECK (status IN ('created','running','completed','failed')),
    error        TEXT,
    warnings_json TEXT
);

CREATE TABLE IF NOT EXISTS data_models (
    model_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    model_json   TEXT NOT NULL,
    profile_json TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS rules (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    rule_key          TEXT NOT NULL,              -- the DSL rule_id, e.g. 'R3' (unique within a run)
    name              TEXT NOT NULL,
    type              TEXT NOT NULL,
    severity          TEXT,
    description       TEXT,
    definition_json   TEXT NOT NULL,
    sample_fail_count INTEGER,                    -- dry-run on the clean sample
    sample_pass_rate  REAL,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (run_id, rule_key)
);

CREATE TABLE IF NOT EXISTS ratings (
    rating_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_pk       INTEGER NOT NULL UNIQUE REFERENCES rules(id) ON DELETE CASCADE,
    score         INTEGER NOT NULL CHECK (score BETWEEN 1 AND 5),
    justification TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS executions (
    exec_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    target_file TEXT,
    total_rows  INTEGER,
    status      TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','completed','failed')),
    error       TEXT,
    started_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS execution_results (
    result_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    exec_id                 INTEGER NOT NULL REFERENCES executions(exec_id) ON DELETE CASCADE,
    rule_pk                 INTEGER NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
    status                  TEXT NOT NULL CHECK (status IN ('ok','error')),
    error                   TEXT,
    total_rows              INTEGER,
    pass_count              INTEGER,
    fail_count              INTEGER,
    skipped_count           INTEGER,
    failed_row_ids_json     TEXT,                 -- capped list; fail_count is always the true total
    failed_row_indices_json TEXT,
    failed_ids_truncated    INTEGER NOT NULL DEFAULT 0,
    sample_reasons_json     TEXT,
    created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS llm_calls (              -- audit trail of prompts and raw responses
    call_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT REFERENCES runs(run_id) ON DELETE CASCADE,
    agent      TEXT NOT NULL,
    prompt     TEXT,
    response   TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_rules_run       ON rules(run_id);
CREATE INDEX IF NOT EXISTS idx_models_run      ON data_models(run_id);
CREATE INDEX IF NOT EXISTS idx_exec_run        ON executions(run_id);
CREATE INDEX IF NOT EXISTS idx_results_exec    ON execution_results(exec_id);
CREATE INDEX IF NOT EXISTS idx_llm_run         ON llm_calls(run_id);
"""


# --------------------------------------------------------------------------- connection
def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path or settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)       # wait for a competing writer instead of failing at once
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")     # SQLite ignores FKs unless enabled per connection
    return conn


@contextmanager
def get_db(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """Commit on success, roll back on error, always close."""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")      # readers (API polling) do not block the pipeline's writes
        conn.executescript(SCHEMA)
        # lightweight migration for databases created before runs.warnings_json existed
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
        if "warnings_json" not in cols:
            conn.execute("ALTER TABLE runs ADD COLUMN warnings_json TEXT")


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=str)


def _loads(text: str | None) -> Any:
    return json.loads(text) if text else None


# --------------------------------------------------------------------------- runs
def new_run(conn: sqlite3.Connection, sample_file: str | None = None, lookup_file: str | None = None) -> str:
    run_id = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO runs (run_id, sample_file, lookup_file) VALUES (?, ?, ?)", (run_id, sample_file, lookup_file)
    )
    return run_id


def set_run_status(
    conn: sqlite3.Connection, run_id: str, status: str, error: str | None = None, warnings: list[str] | None = None
) -> None:
    if warnings is None:
        conn.execute("UPDATE runs SET status = ?, error = ? WHERE run_id = ?", (status, error, run_id))
    else:
        conn.execute(
            "UPDATE runs SET status = ?, error = ?, warnings_json = ? WHERE run_id = ?",
            (status, error, _dumps(warnings), run_id),
        )


def _run_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["warnings"] = _loads(d.pop("warnings_json", None)) or []
    return d


def get_run(conn: sqlite3.Connection, run_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return _run_dict(row) if row else None


def list_runs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT r.*,
                  (SELECT COUNT(*) FROM rules   WHERE run_id = r.run_id) AS rule_count,
                  (SELECT COUNT(*) FROM executions WHERE run_id = r.run_id) AS execution_count
           FROM runs r ORDER BY r.created_at DESC, r.rowid DESC"""
    ).fetchall()
    return [_run_dict(r) for r in rows]


# --------------------------------------------------------------------------- data model
def save_data_model(conn: sqlite3.Connection, run_id: str, model: dict, profile: dict | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO data_models (run_id, model_json, profile_json) VALUES (?, ?, ?)",
        (run_id, _dumps(model), _dumps(profile) if profile is not None else None),
    )
    return int(cur.lastrowid)


def get_data_model(conn: sqlite3.Connection, run_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM data_models WHERE run_id = ? ORDER BY model_id DESC LIMIT 1", (run_id,)
    ).fetchone()
    if not row:
        return None
    return {"model_id": row["model_id"], "run_id": run_id, "model": _loads(row["model_json"]),
            "profile": _loads(row["profile_json"]), "created_at": row["created_at"]}


# --------------------------------------------------------------------------- rules + ratings
def save_rule(conn: sqlite3.Connection, run_id: str, rule: Rule, sample_result: RuleResult | None = None) -> int:
    """Insert a rule (with its clean-sample dry-run stats). Returns the rule's primary key."""
    pass_rate = sample_result.pass_rate if sample_result else None
    cur = conn.execute(
        """INSERT INTO rules (run_id, rule_key, name, type, severity, description, definition_json,
                              sample_fail_count, sample_pass_rate)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, rule.rule_id, rule.name, rule.type.value, rule.severity, rule.description,
         rule.model_dump_json(), sample_result.fail_count if sample_result else None, pass_rate),
    )
    return int(cur.lastrowid)


def save_rules(
    conn: sqlite3.Connection, run_id: str, rules: list[Rule], sample_results: dict[str, RuleResult] | None = None
) -> dict[str, int]:
    """Returns {rule_key: rule_pk}."""
    sample_results = sample_results or {}
    return {r.rule_id: save_rule(conn, run_id, r, sample_results.get(r.rule_id)) for r in rules}


def save_rating(conn: sqlite3.Connection, rule_pk: int, score: int, justification: str = "") -> None:
    """Insert or replace the rating for a rule. Scores outside 1-5 are rejected by a CHECK constraint."""
    conn.execute(
        """INSERT INTO ratings (rule_pk, score, justification) VALUES (?, ?, ?)
           ON CONFLICT(rule_pk) DO UPDATE SET score = excluded.score, justification = excluded.justification""",
        (rule_pk, score, justification),
    )


def list_rules(conn: sqlite3.Connection, run_id: str, min_score: int | None = None) -> list[dict]:
    """Rules joined with their ratings, best-rated first. Each dict has a parsed `definition`."""
    sql = """SELECT r.*, rt.score, rt.justification
             FROM rules r LEFT JOIN ratings rt ON rt.rule_pk = r.id
             WHERE r.run_id = ?"""
    args: list[Any] = [run_id]
    if min_score is not None:
        sql += " AND rt.score >= ?"
        args.append(min_score)
    sql += " ORDER BY rt.score DESC, r.id ASC"
    out = []
    for row in conn.execute(sql, args).fetchall():
        d = dict(row)
        d["definition"] = _loads(d.pop("definition_json"))
        out.append(d)
    return out


def rule_from_row(row: dict) -> Rule:
    return Rule.model_validate(row["definition"])


# --------------------------------------------------------------------------- executions
def new_execution(conn: sqlite3.Connection, run_id: str, target_file: str | None = None) -> int:
    cur = conn.execute("INSERT INTO executions (run_id, target_file) VALUES (?, ?)", (run_id, target_file))
    return int(cur.lastrowid)


def save_execution_result(
    conn: sqlite3.Connection, exec_id: int, rule_pk: int, result: RuleResult, max_ids: int | None = None
) -> None:
    cap = max_ids if max_ids is not None else settings.max_failed_ids
    ids, idx = result.failed_row_ids, result.failed_row_indices
    conn.execute(
        """INSERT INTO execution_results
           (exec_id, rule_pk, status, error, total_rows, pass_count, fail_count, skipped_count,
            failed_row_ids_json, failed_row_indices_json, failed_ids_truncated, sample_reasons_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (exec_id, rule_pk, result.status, result.error, result.total_rows, result.pass_count, result.fail_count,
         result.skipped_count, _dumps(ids[:cap]), _dumps(idx[:cap]), int(len(ids) > cap),
         _dumps(result.sample_reasons)),
    )


def finish_execution(
    conn: sqlite3.Connection, exec_id: int, status: str, error: str | None = None, total_rows: int | None = None
) -> None:
    conn.execute(
        """UPDATE executions SET status = ?, error = ?, total_rows = COALESCE(?, total_rows),
                  finished_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
           WHERE exec_id = ?""",
        (status, error, total_rows, exec_id),
    )


def get_execution(conn: sqlite3.Connection, exec_id: int) -> dict | None:
    ex = conn.execute("SELECT * FROM executions WHERE exec_id = ?", (exec_id,)).fetchone()
    if not ex:
        return None
    rows = conn.execute(
        """SELECT er.*, r.rule_key, r.name AS rule_name, r.type AS rule_type, r.severity, r.description,
                  rt.score, rt.justification
           FROM execution_results er
           JOIN rules r ON r.id = er.rule_pk
           LEFT JOIN ratings rt ON rt.rule_pk = r.id
           WHERE er.exec_id = ? ORDER BY er.fail_count DESC, er.result_id""",
        (exec_id,),
    ).fetchall()
    results = []
    for row in rows:
        d = dict(row)
        for col in ("failed_row_ids_json", "failed_row_indices_json", "sample_reasons_json"):
            d[col.removesuffix("_json")] = _loads(d.pop(col))
        d["failed_ids_truncated"] = bool(d["failed_ids_truncated"])
        results.append(d)
    return {**dict(ex), "results": results}


def list_executions(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM executions WHERE run_id = ? ORDER BY exec_id DESC", (run_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def fail_interrupted(conn: sqlite3.Connection) -> dict[str, int]:
    """Mark runs/executions left 'created'/'running' by a stopped server as failed.
    Call once at startup (assumes a single server process)."""
    msg = "Interrupted: the server stopped before this finished. Start it again."
    runs = conn.execute(
        "UPDATE runs SET status = 'failed', error = ? WHERE status IN ('created', 'running')", (msg,)
    ).rowcount
    execs = conn.execute(
        "UPDATE executions SET status = 'failed', error = ?, finished_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "WHERE status = 'running'", (msg,)
    ).rowcount
    return {"runs": runs, "executions": execs}


# --------------------------------------------------------------------------- audit
def log_llm_call(conn: sqlite3.Connection, run_id: str | None, agent: str, prompt: str, response: str) -> None:
    conn.execute(
        "INSERT INTO llm_calls (run_id, agent, prompt, response) VALUES (?, ?, ?, ?)", (run_id, agent, prompt, response)
    )
