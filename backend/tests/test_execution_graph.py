import json

import pytest

from app import db, services, storage
from app.data_io import load_csv
from fixtures import enrichment, ratings_json, rules_json
from pipeline_helpers import GT_KEY as _ALL_GT, SCORES, happy_script, run_pipeline

GT_KEY = {k: v for k, v in _ALL_GT.items() if k not in ('customer_email_format', 'customer_email_not_null')}

@pytest.fixture
def completed(env, data_dir, profile):
    run, _ = run_pipeline(env, data_dir, happy_script(profile))
    return run["run_id"]


@pytest.fixture
def prod(data_dir):
    return load_csv(data_dir / "prod_orders.csv")


@pytest.fixture
def truth(data_dir):
    return json.loads((data_dir / "ground_truth.json").read_text())["expected_rule_failures"]


def execute(env, run_id, prod, **kw):
    return services.run_execution(run_id, prod, "prod_orders.csv", db_path=env.db, workdir=env.work, **kw)


def test_persisted_rules_reproduce_the_ground_truth(env, completed, prod, truth):
    ex = execute(env, completed, prod)
    assert ex["status"] == "completed" and ex["total_rows"] == 400 and ex["target_file"] == "prod_orders.csv"
    assert ex["error"] is None and ex["finished_at"]
    assert {r["rule_name"] for r in ex["results"]} == set(GT_KEY)             # exactly the rules rated >= 4
    for res in ex["results"]:
        expected = truth[GT_KEY[res["rule_name"]]]
        assert res["status"] == "ok", res
        assert res["fail_count"] == expected["count"], res["rule_name"]
        assert res["pass_count"] == 400 - expected["count"] and res["total_rows"] == 400
        assert res["failed_row_ids"] == expected["order_ids"], res["rule_name"]       # labelled by order_id, not row number
        assert res["failed_row_indices"] == expected["row_indices"]
        assert res["sample_reasons"] and not res["failed_ids_truncated"]
        assert res["score"] in (4, 5) and res["rule_key"].startswith("R")
    assert ex["results"][0]["rule_name"] == "discount_not_above_amount"            # worst offender first (22)


def test_only_rules_rated_at_or_above_threshold_run(env, completed, prod):
    names = {r["rule_name"] for r in execute(env, completed, prod)["results"]}
    assert "customer_email_format" not in names and "customer_email_not_null" not in names     # rated 3
    strict = {r["rule_name"] for r in execute(env, completed, prod, rating_threshold=5)["results"]}
    assert strict == set(GT_KEY) - {"created_at_iso"}                                           # R6 is rated 4


def test_nothing_rated_high_enough_fails_loudly(env, data_dir, profile, prod):
    run, _ = run_pipeline(env, data_dir, happy_script(profile, [3] * 8))
    ex = execute(env, run["run_id"], prod)
    assert ex["status"] == "failed" and "No rules rated 4" in ex["error"] and ex["results"] == []


def test_missing_column_is_a_per_rule_error_not_a_crash(env, completed, prod):
    ex = execute(env, completed, prod.drop(columns=["discount_amount"]))
    assert ex["status"] == "completed"
    by = {r["rule_name"]: r for r in ex["results"]}
    assert by["discount_not_above_amount"]["status"] == "error" and "unknown column" in by["discount_not_above_amount"]["error"]
    assert by["order_amount_not_null"]["status"] == "ok" and by["order_amount_not_null"]["fail_count"] == 5


def test_multiple_executions_and_stored_copy(env, completed, prod):
    a, b = execute(env, completed, prod), execute(env, completed, prod.head(100))
    assert a["exec_id"] != b["exec_id"] and (a["total_rows"], b["total_rows"]) == (400, 100)
    with db.get_db(env.db) as conn:
        assert len(db.list_executions(conn, completed)) == 2
    assert (storage.run_dir(completed, env.work) / "executions" / str(a["exec_id"]) / "prod_orders.csv").exists()


def test_execution_needs_only_the_database_and_disk(env, completed, prod):
    """start/execute are separate calls that share no memory: this is what lets an API run them in the background."""
    exec_id = services.start_execution(completed, prod, "prod_orders.csv", db_path=env.db, workdir=env.work)
    with db.get_db(env.db) as conn:
        assert db.get_execution(conn, exec_id)["status"] == "running"
    ex = services.execute_rules(exec_id, db_path=env.db, workdir=env.work)
    assert ex["status"] == "completed"


def test_execution_state_validation(env, data_dir, profile, completed, prod):
    with pytest.raises(KeyError):
        services.start_execution("0" * 12, prod, "p.csv", db_path=env.db, workdir=env.work)
    with pytest.raises(ValueError, match="no rows"):
        services.start_execution(completed, prod.iloc[0:0], "p.csv", db_path=env.db, workdir=env.work)
    with pytest.raises(KeyError):
        services.execute_rules(999, db_path=env.db, workdir=env.work)
    ex = execute(env, completed, prod)
    with pytest.raises(ValueError, match="expected 'running'"):
        services.execute_rules(ex["exec_id"], db_path=env.db, workdir=env.work)
    failed_run, _ = run_pipeline(env, data_dir, ["garbage"] * 3)
    with pytest.raises(ValueError, match="must complete first"):
        services.start_execution(failed_run["run_id"], prod, "p.csv", db_path=env.db, workdir=env.work)
