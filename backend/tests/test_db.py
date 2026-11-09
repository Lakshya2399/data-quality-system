import sqlite3

import pytest

from app import db
from app.default_rules import default_rules
from app.schemas import RuleResult


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "t.sqlite"
    db.init_db(path)
    db.init_db(path)                         # idempotent
    with db.get_db(path) as c:
        yield c


def seed_run(conn):
    run_id = db.new_run(conn, "sample_orders.csv", "valid_regions.csv")
    pks = db.save_rules(conn, run_id, default_rules()[:3])
    return run_id, pks


def test_run_model_roundtrip(conn):
    run_id = db.new_run(conn, "s.csv", "l.csv")
    db.save_data_model(conn, run_id, {"tables": []}, {"profile": 1})
    got = db.get_data_model(conn, run_id)
    assert got["model"] == {"tables": []} and got["profile"] == {"profile": 1}
    db.set_run_status(conn, run_id, "completed")
    assert db.get_run(conn, run_id)["status"] == "completed"
    assert db.list_runs(conn)[0]["run_id"] == run_id


def test_rules_ratings_and_threshold_filter(conn):
    run_id, pks = seed_run(conn)
    for key, score in zip(["R1", "R2", "R3"], [2, 4, 5]):
        db.save_rating(conn, pks[key], score, f"why {key}")
    db.save_rating(conn, pks["R1"], 3, "re-rated")           # upsert, not duplicate
    rules = db.list_rules(conn, run_id)
    assert [r["rule_key"] for r in rules] == ["R3", "R2", "R1"]   # best first
    assert rules[2]["score"] == 3 and rules[2]["justification"] == "re-rated"
    top = db.list_rules(conn, run_id, min_score=4)
    assert [r["rule_key"] for r in top] == ["R3", "R2"]
    assert db.rule_from_row(top[0]) == default_rules()[2]    # definition survives the round trip


def test_check_constraints_and_foreign_keys(conn):
    run_id, pks = seed_run(conn)
    for bad in (0, 6):
        with pytest.raises(sqlite3.IntegrityError):
            db.save_rating(conn, pks["R1"], bad)
    with pytest.raises(sqlite3.IntegrityError):
        db.save_rating(conn, 99999, 3)                       # unknown rule
    with pytest.raises(sqlite3.IntegrityError):
        db.save_rule(conn, run_id, default_rules()[0])       # duplicate (run_id, rule_key)


def test_execution_results_capped_and_joined(conn):
    run_id, pks = seed_run(conn)
    db.save_rating(conn, pks["R2"], 5, "critical")
    exec_id = db.new_execution(conn, run_id, "prod_orders.csv")
    big = RuleResult(rule_id="R2", total_rows=500, fail_count=150, pass_count=350,
                     failed_row_ids=[f"ORD-{i}" for i in range(150)], failed_row_indices=list(range(150)),
                     sample_reasons=[{"row_index": 0, "row_id": "ORD-0", "reason": "bad"}])
    err = RuleResult(rule_id="R1", status="error", error="boom", total_rows=500)
    db.save_execution_result(conn, exec_id, pks["R2"], big, max_ids=100)
    db.save_execution_result(conn, exec_id, pks["R1"], err)
    db.finish_execution(conn, exec_id, "completed", total_rows=500)

    ex = db.get_execution(conn, exec_id)
    assert ex["status"] == "completed" and ex["total_rows"] == 500 and ex["finished_at"]
    first = ex["results"][0]                                  # ordered by fail_count desc
    assert first["rule_key"] == "R2" and first["score"] == 5
    assert first["fail_count"] == 150 and len(first["failed_row_ids"]) == 100 and first["failed_ids_truncated"]
    assert first["sample_reasons"][0]["reason"] == "bad"
    assert ex["results"][1]["status"] == "error" and ex["results"][1]["error"] == "boom"
    assert len(db.list_executions(conn, run_id)) == 1


def test_deleting_a_run_cascades(conn):
    run_id, pks = seed_run(conn)
    db.save_rating(conn, pks["R1"], 4)
    exec_id = db.new_execution(conn, run_id)
    db.save_execution_result(conn, exec_id, pks["R1"], RuleResult(rule_id="R1", total_rows=1))
    db.log_llm_call(conn, run_id, "rule_builder", "p", "r")
    conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
    for table in ("rules", "ratings", "executions", "execution_results", "llm_calls"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


def test_get_db_rolls_back_on_error(tmp_path):
    path = tmp_path / "r.sqlite"
    db.init_db(path)
    with pytest.raises(RuntimeError):
        with db.get_db(path) as c:
            db.new_run(c)
            raise RuntimeError("fail")
    with db.get_db(path) as c:
        assert db.list_runs(c) == []
