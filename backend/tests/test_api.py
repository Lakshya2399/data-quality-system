import json

import pytest
from fastapi.testclient import TestClient

from app import db
from app.llm import LLMError
from app.main import create_app
from api_helpers import ScriptedFactory, make_client
from pipeline_helpers import GARBAGE, GT_KEY, SCORES, happy_script


def csv_bytes(df):
    return df.to_csv(index=False).encode()


def start_run(client):
    r = client.post("/pipeline/run")
    assert r.status_code == 202, r.text
    return r.json()["run_id"]


@pytest.fixture
def api(env, data_dir, profile):
    """(client, run_id) with a completed run; rules R1-R6 rated >= 4, R7-R8 rated 3."""
    client = make_client(env, data_dir, happy_script(profile))
    return client, start_run(client)


@pytest.fixture
def truth(data_dir):
    return json.loads((data_dir / "ground_truth.json").read_text())["expected_rule_failures"]


# ---- meta -------------------------------------------------------------------
def test_health_and_openapi(env, data_dir):
    c = make_client(env, data_dir)
    assert c.get("/health").json()["status"] == "ok"
    paths = c.get("/openapi.json").json()["paths"]
    assert set(paths) == {"/health", "/pipeline/run", "/runs", "/runs/{run_id}", "/runs/{run_id}/model",
                          "/runs/{run_id}/rules", "/runs/{run_id}/executions", "/runs/{run_id}/execute",
                          "/executions/{exec_id}"}


def test_sqlite_runs_in_wal_mode(env, data_dir):
    make_client(env, data_dir).get("/runs")
    with db.get_db(env.db) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


# ---- starting the pipeline --------------------------------------------------
def test_defaults_run_in_background_and_can_be_browsed(api):
    client, run_id = api
    run = client.get(f"/runs/{run_id}").json()
    assert run["status"] == "completed" and run["rule_count"] == 8 and run["execution_count"] == 0
    assert run["warnings"] == [] and run["error"] is None
    assert client.get("/runs").json()[0]["run_id"] == run_id

    model = client.get(f"/runs/{run_id}/model").json()
    assert [t["name"] for t in model["model"]["tables"]] == ["sample_orders", "valid_regions"] and model["profile"] is None
    assert "relationships" in client.get(f"/runs/{run_id}/model?include_profile=true").json()["profile"]

    rules = client.get(f"/runs/{run_id}/rules").json()
    assert (rules["min_score"], rules["executable_count"], len(rules["rules"])) == (4, 6, 8)
    assert rules["score_distribution"] == {"1": 0, "2": 0, "3": 2, "4": 1, "5": 5}
    assert [r["score"] for r in rules["rules"]] == sorted(SCORES, reverse=True)
    top = rules["rules"][0]
    assert top["will_execute"] and top["justification"] and top["definition"]["type"] and top["sample_pass_rate"] == 1.0
    assert not rules["rules"][-1]["will_execute"]
    assert client.get(f"/runs/{run_id}/rules?min_score=5").json()["executable_count"] == 5


def test_start_response_shape(env, data_dir, profile):
    client = make_client(env, data_dir, happy_script(profile))
    body = client.post("/pipeline/run").json()
    assert body["status"] == "created" and body["poll_url"] == f"/runs/{body['run_id']}"


def test_upload_files_instead_of_defaults(env, tmp_path, sample_df, regions_df, profile):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    client = make_client(env, empty_dir, happy_script(profile))          # no defaults exist: uploads must be used
    r = client.post("/pipeline/run", files={
        "sample_file": ("sample_orders.csv", csv_bytes(sample_df), "text/csv"),
        "lookup_file": ("valid_regions.csv", csv_bytes(regions_df), "text/csv")})
    assert r.status_code == 202
    run = client.get(f"/runs/{r.json()['run_id']}").json()
    assert run["status"] == "completed" and run["sample_file"] == "sample_orders.csv"


def test_wait_true_blocks_and_returns_200(env, data_dir, profile):
    client = make_client(env, data_dir, happy_script(profile))
    r = client.post("/pipeline/run?wait=true")
    assert r.status_code == 200 and r.json()["status"] == "completed"


def test_pipeline_failure_is_data_not_an_http_error(env, data_dir):
    client = make_client(env, data_dir, GARBAGE)
    run_id = start_run(client)
    run = client.get(f"/runs/{run_id}").json()
    assert run["status"] == "failed" and "LLMOutputError: model_agent" in run["error"]
    for path in ("model", "rules"):
        r = client.get(f"/runs/{run_id}/{path}")
        assert r.status_code == 409 and "failed" in r.json()["detail"]
    assert client.post(f"/runs/{run_id}/execute").status_code == 409
    assert client.get(f"/runs/{run_id}/executions").json() == []


@pytest.mark.parametrize(
    "files, status, text",
    [
        ({"sample_file": ("a.csv", b"x\n1\n", "text/csv")}, 422, "both sample_file and lookup_file"),
        ({"sample_file": ("a.txt", b"x\n1\n", "text/csv"), "lookup_file": ("b.csv", b"x\n1\n", "text/csv")}, 400, "must end with .csv"),
        ({"sample_file": ("a.csv", b"  \n", "text/csv"), "lookup_file": ("b.csv", b"x\n1\n", "text/csv")}, 400, "empty"),
        ({"sample_file": ("a.csv", b"x,y\n", "text/csv"), "lookup_file": ("b.csv", b"x\n1\n", "text/csv")}, 400, "no data rows"),
        ({"sample_file": ("a.csv", b"\xff\xfe\xfa\xfb\x00\x01", "text/csv"), "lookup_file": ("b.csv", b"x\n1\n", "text/csv")}, 400, "not a readable CSV"),
        ({"sample_file": ("a.csv", b"a,b\n1,2\n1,2,3,4,5\n", "text/csv"), "lookup_file": ("b.csv", b"x\n1\n", "text/csv")}, 400, "not a readable CSV"),
        ({"sample_file": ("x.csv", b"a\n1\n", "text/csv"), "lookup_file": ("x.csv", b"a\n1\n", "text/csv")}, 400, "different names"),
    ],
)
def test_pipeline_input_validation(env, data_dir, files, status, text):
    client = make_client(env, data_dir)
    r = client.post("/pipeline/run", files=files)
    assert r.status_code == status and text in r.text
    assert client.get("/runs").json() == []                              # nothing was created


def test_upload_size_limit(env, data_dir):
    client = make_client(env, data_dir, max_upload_mb=0.0001)            # ~100 bytes
    r = client.post("/pipeline/run", files={"sample_file": ("a.csv", b"x\n" + b"1\n" * 500, "text/csv"),
                                            "lookup_file": ("b.csv", b"x\n1\n", "text/csv")})
    assert r.status_code == 413 and "upload limit" in r.text


def test_missing_default_files(env, tmp_path):
    empty = tmp_path / "nothing"
    empty.mkdir()
    r = make_client(env, empty).post("/pipeline/run")
    assert r.status_code == 400 and "default" in r.text and "generate_data.py" in r.text


def test_llm_unavailable_is_503_and_creates_no_run(env, data_dir):
    def no_key():
        raise LLMError("GEMINI_API_KEY is not set")

    client = make_client(env, data_dir, factory=no_key)
    r = client.post("/pipeline/run")
    assert r.status_code == 503 and "GEMINI_API_KEY" in r.json()["detail"]
    assert client.get("/runs").json() == []


@pytest.mark.parametrize("method, path", [
    ("get", "/runs/nope"), ("get", "/runs/nope/model"), ("get", "/runs/nope/rules"),
    ("get", "/runs/nope/executions"), ("post", "/runs/nope/execute"), ("get", "/executions/999")])
def test_unknown_ids_are_404(env, data_dir, method, path):
    assert getattr(make_client(env, data_dir), method)(path).status_code == 404


def test_bad_parameters_are_422(api):
    client, run_id = api
    assert client.get("/executions/abc").status_code == 422
    assert client.get(f"/runs/{run_id}/rules?min_score=9").status_code == 422
    assert client.post(f"/runs/{run_id}/execute?min_score=0").status_code == 422
    assert client.get("/runs?limit=0").status_code == 422


# ---- execution ---------------------------------------------------------------
def test_execute_defaults_matches_ground_truth(api, truth):
    client, run_id = api
    r = client.post(f"/runs/{run_id}/execute")
    assert r.status_code == 202 and r.json()["status"] == "running"
    ex = client.get(r.json()["poll_url"]).json()
    assert ex["status"] == "completed" and ex["total_rows"] == 400 and ex["target_file"] == "prod_orders.csv"
    assert ex["summary"] == {"rules_executed": 6, "rules_with_failures": 6, "rules_errored": 0}
    for res in ex["results"]:
        expected = truth[GT_KEY[res["rule_name"]]]
        assert res["fail_count"] == expected["count"] and res["failed_row_ids"] == expected["order_ids"], res["rule_name"]
        assert res["sample_reasons"] and res["score"] >= 4
    assert client.get(f"/runs/{run_id}").json()["execution_count"] == 1


def test_min_score_controls_which_rules_run(api, truth):
    client, run_id = api
    five = client.get(client.post(f"/runs/{run_id}/execute?min_score=5").json()["poll_url"]).json()
    assert five["summary"]["rules_executed"] == 5
    three = client.get(client.post(f"/runs/{run_id}/execute?min_score=3").json()["poll_url"]).json()
    by = {r["rule_name"]: r for r in three["results"]}
    assert three["summary"]["rules_executed"] == 8
    assert by["customer_email_format"]["fail_count"] == truth["customer_email_format"]["count"] == 6      # caught at threshold 3
    assert len(client.get(f"/runs/{run_id}/executions").json()) == 2


def test_upload_target_file_with_missing_column(api, data_dir):
    client, run_id = api
    from app.data_io import load_csv
    prod = load_csv(data_dir / "prod_orders.csv").drop(columns=["discount_amount"])
    r = client.post(f"/runs/{run_id}/execute", files={"target_file": ("my_prod.csv", csv_bytes(prod), "text/csv")})
    ex = client.get(r.json()["poll_url"]).json()
    assert ex["status"] == "completed" and ex["target_file"] == "my_prod.csv"
    assert ex["summary"]["rules_errored"] == 1
    bad = next(x for x in ex["results"] if x["status"] == "error")
    assert bad["rule_name"] == "discount_not_above_amount" and "unknown column" in bad["error"]


def test_execute_wait_true(api):
    client, run_id = api
    r = client.post(f"/runs/{run_id}/execute?wait=true")
    assert r.status_code == 200 and r.json()["status"] == "completed"


def test_execute_with_nothing_rated_high_enough_reports_it(env, data_dir, profile):
    client = make_client(env, data_dir, happy_script(profile, [3] * 8))
    run_id = start_run(client)
    ex = client.get(client.post(f"/runs/{run_id}/execute").json()["poll_url"]).json()
    assert ex["status"] == "failed" and "No rules rated 4" in ex["error"] and ex["results"] == []
    assert ex["summary"]["rules_executed"] == 0


def test_execute_input_validation(api, tmp_path, env, data_dir):
    client, run_id = api
    r = client.post(f"/runs/{run_id}/execute", files={"target_file": ("p.csv", b"", "text/csv")})
    assert r.status_code == 400 and "empty" in r.text
    r = client.post(f"/runs/{run_id}/execute", files={"target_file": ("p.json", b"{}", "application/json")})
    assert r.status_code == 400
    assert client.get(f"/runs/{run_id}/executions").json() == []         # rejected before anything was created
    empty = tmp_path / "none"
    empty.mkdir()
    no_default = TestClient(create_app(llm_factory=ScriptedFactory(), db_path=env.db, workdir=env.work, data_dir=empty))
    assert no_default.post(f"/runs/{run_id}/execute").status_code == 400


# ---- restart recovery -------------------------------------------------------
def test_interrupted_jobs_are_failed_on_startup(env, data_dir):
    db.init_db(env.db)
    with db.get_db(env.db) as conn:
        stuck_created, stuck_running, done = (db.new_run(conn, "s.csv", "l.csv") for _ in range(3))
        db.set_run_status(conn, stuck_running, "running")
        db.set_run_status(conn, done, "completed")
        exec_stuck = db.new_execution(conn, done, "p.csv")
        exec_done = db.new_execution(conn, done, "p.csv")
        db.finish_execution(conn, exec_done, "completed")

    with TestClient(create_app(llm_factory=ScriptedFactory(), db_path=env.db, workdir=env.work, data_dir=data_dir)) as c:
        for run_id in (stuck_created, stuck_running):
            run = c.get(f"/runs/{run_id}").json()
            assert run["status"] == "failed" and "Interrupted" in run["error"]
        assert c.get(f"/runs/{done}").json()["status"] == "completed"
        assert c.get(f"/executions/{exec_stuck}").json()["status"] == "failed"
        assert c.get(f"/executions/{exec_done}").json()["status"] == "completed"


def test_fail_interrupted_counts(env):
    db.init_db(env.db)
    with db.get_db(env.db) as conn:
        assert db.fail_interrupted(conn) == {"runs": 0, "executions": 0}
        db.new_run(conn)
        assert db.fail_interrupted(conn) == {"runs": 1, "executions": 0}
        assert db.fail_interrupted(conn) == {"runs": 0, "executions": 0}       # idempotent
