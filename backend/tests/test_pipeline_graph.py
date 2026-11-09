import json

import pytest

from app import db, services, storage
from app.agent_schemas import DataModel
from app.default_rules import default_rules
from app.graphs.pipeline import applicable_default_rules, dry_run_feedback, severity_ratings
from app.rule_engine import run_rule
from fakes import TransientError, make_llm
from fixtures import draft, enrichment, good_drafts, ratings_json, rules_json
from pipeline_helpers import GARBAGE, SCORES, happy_script, rows, run_pipeline

BAD_RULE = draft("order_amount_at_least_500", "range", ["order_amount"], "high", min=500)


def bad_drafts():
    d = good_drafts()
    d[2] = BAD_RULE                          # fails on clean sample rows -> R3
    return d


# ---- happy path -------------------------------------------------------------
def test_happy_path_populates_every_table(env, data_dir, profile):
    run, llm = run_pipeline(env, data_dir, happy_script(profile))
    run_id = run["run_id"]
    assert run["status"] == "completed" and run["error"] is None and run["warnings"] == []
    assert (run["sample_file"], run["lookup_file"]) == ("sample_orders.csv", "valid_regions.csv")
    assert len(llm.calls) == 3

    (model_row,) = rows(env, "SELECT * FROM data_models WHERE run_id = ?", run_id)
    DataModel.model_validate(json.loads(model_row["model_json"]))
    assert json.loads(model_row["profile_json"])["relationships"]

    rules = db.list_rules(db.connect(env.db), run_id)
    assert [r["rule_key"] for r in rules] == [f"R{i}" for i in range(1, 9)]
    assert [r["score"] for r in rules] == sorted(SCORES, reverse=True)       # best first
    assert all(r["sample_fail_count"] == 0 and r["sample_pass_rate"] == 1.0 for r in rules)
    assert len(rows(env, "SELECT * FROM ratings")) == 8

    audit = rows(env, "SELECT agent, run_id, prompt, response FROM llm_calls ORDER BY call_id")
    assert [a["agent"] for a in audit] == ["model_agent", "rule_builder", "rule_rater"]
    assert {a["run_id"] for a in audit} == {run_id} and all(a["prompt"] and a["response"] for a in audit)


def test_inputs_are_stored_with_the_run(env, data_dir, profile):
    run, _ = run_pipeline(env, data_dir, happy_script(profile))
    d = storage.run_dir(run["run_id"], env.work)
    assert (d / "sample_orders.csv").exists() and (d / "valid_regions.csv").exists()


# ---- retry loop -------------------------------------------------------------
def test_dry_run_failure_triggers_retry_with_evidence(env, data_dir, profile):
    script = [json.dumps(enrichment(profile)), rules_json(bad_drafts()), rules_json(), ratings_json(SCORES)]
    run, llm = run_pipeline(env, data_dir, script)
    assert run["status"] == "completed" and len(llm.calls) == 4
    first, second = llm.calls[1].prompt, llm.calls[2].prompt
    assert "Feedback on your previous attempt" not in first
    assert "Feedback on your previous attempt" in second
    assert "order_amount_at_least_500" in second and "FAILS on" in second and "clean sample" in second
    names = {r["name"] for r in rows(env, "SELECT name FROM rules")}
    assert "order_amount_at_least_500" not in names and len(names) == 8      # only the final set is persisted
    assert len(rows(env, "SELECT * FROM llm_calls")) == 4


def test_retries_are_bounded_and_leftovers_are_capped(env, data_dir, profile):
    script = [json.dumps(enrichment(profile)), *[rules_json(bad_drafts())] * 3, ratings_json([5] * 8)]
    run, llm = run_pipeline(env, data_dir, script)               # default: 2 retries -> 3 builder calls
    assert run["status"] == "completed" and len(llm.calls) == 5
    (r3,) = rows(env, "SELECT rt.score, rt.justification FROM ratings rt JOIN rules r ON r.id = rt.rule_pk WHERE r.rule_key = 'R3'")
    assert r3["score"] == 2 and "capped to 2" in r3["justification"]
    assert any(w.startswith("R3 (order_amount_at_least_500): capped to 2") for w in run["warnings"])


def test_zero_retries_configurable(env, data_dir, profile):
    script = [json.dumps(enrichment(profile)), rules_json(bad_drafts()), ratings_json([5] * 8)]
    run, llm = run_pipeline(env, data_dir, script, max_rule_retries=0)
    assert run["status"] == "completed" and len(llm.calls) == 3


# ---- degraded modes ---------------------------------------------------------
def test_builder_failure_falls_back_to_default_rules(env, data_dir, profile):
    script = [json.dumps(enrichment(profile)), *GARBAGE, ratings_json(SCORES)]
    run, llm = run_pipeline(env, data_dir, script)
    assert run["status"] == "completed" and len(llm.calls) == 5
    names = [r["name"] for r in rows(env, "SELECT name FROM rules ORDER BY id")]
    assert names == [r.name for r in default_rules()]
    assert any("deterministic default rules" in w for w in run["warnings"])


def test_builder_failure_on_retry_keeps_previous_rules(env, data_dir, profile):
    script = [json.dumps(enrichment(profile)), rules_json(bad_drafts()), *GARBAGE, ratings_json([5] * 8)]
    run, llm = run_pipeline(env, data_dir, script)
    assert run["status"] == "completed" and len(llm.calls) == 6
    assert "order_amount_at_least_500" in {r["name"] for r in rows(env, "SELECT name FROM rules")}
    assert any("kept the previous attempt's rules" in w for w in run["warnings"])


def test_rater_failure_falls_back_to_severity_ratings(env, data_dir, profile):
    script = [json.dumps(enrichment(profile)), rules_json(), *GARBAGE]
    run, _ = run_pipeline(env, data_dir, script)
    assert run["status"] == "completed"
    scores = {r["rule_key"]: r["score"] for r in db.list_rules(db.connect(env.db), run["run_id"])}
    assert scores == {"R1": 4, "R2": 4, "R3": 4, "R4": 4, "R5": 4, "R6": 3, "R7": 3, "R8": 3}   # high=4, medium=3
    assert any("derived from rule severity" in w for w in run["warnings"])


def test_no_rule_rated_high_enough_is_a_visible_warning(env, data_dir, profile):
    run, _ = run_pipeline(env, data_dir, happy_script(profile, [3] * 8))
    assert run["status"] == "completed"
    assert any("nothing will be executed" in w for w in run["warnings"])
    assert any("same score" in w for w in run["warnings"])


# ---- failures ---------------------------------------------------------------
def test_model_agent_failure_fails_the_run_without_raising(env, data_dir, profile):
    run, llm = run_pipeline(env, data_dir, GARBAGE)
    assert run["status"] == "failed" and run["error"].startswith("LLMOutputError: model_agent")
    assert rows(env, "SELECT * FROM rules") == [] and rows(env, "SELECT * FROM data_models") == []
    assert len(rows(env, "SELECT * FROM llm_calls")) == 3                 # the failed attempts are still audited


def test_unreachable_llm_fails_the_run(env, data_dir):
    run, _ = run_pipeline(env, data_dir, [TransientError(400)])
    assert run["status"] == "failed" and "Gemini call failed" in run["error"]


# ---- service validation -----------------------------------------------------
def test_service_input_validation(env, data_dir, sample_df, regions_df, profile):
    with pytest.raises(ValueError, match="no rows"):
        services.create_run(sample_df.iloc[0:0], "s.csv", {"r.csv": regions_df}, db_path=env.db, workdir=env.work)
    with pytest.raises(ValueError, match="different names"):
        services.create_run(sample_df, "x.csv", {"x.csv": regions_df}, db_path=env.db, workdir=env.work)
    with pytest.raises(KeyError):
        services.execute_pipeline("0" * 12, make_llm([]), db_path=env.db, workdir=env.work)
    run, _ = run_pipeline(env, data_dir, happy_script(profile))
    with pytest.raises(ValueError, match="expected 'created'"):
        services.execute_pipeline(run["run_id"], make_llm([]), db_path=env.db, workdir=env.work)


def test_storage_rejects_path_tricks():
    for bad in ("../x", "..", "abc", "A" * 12):
        with pytest.raises(ValueError):
            storage.run_dir(bad)
    assert storage.table_name("../../etc/passwd.csv") == "passwd"
    assert storage.table_name("C:\\data\\my file.CSV") == "my_file"
    for bad in ("", ".csv", ".."):
        with pytest.raises(ValueError):
            storage.table_name(bad)


# ---- unit tests for graph helpers -------------------------------------------
def test_applicable_default_rules_filters_to_the_schema():
    cols = ["order_id", "customer_email", "region_code", "order_amount", "discount_amount", "created_at"]
    lk = {"valid_regions": ["region_code", "country", "tax_cap"]}
    assert len(applicable_default_rules(cols, lk)) == 8
    no_discount = applicable_default_rules([c for c in cols if c != "discount_amount"], lk)
    assert [r.rule_id for r in no_discount] == ["R1", "R2", "R3", "R5", "R6", "R7", "R8"]
    assert [r.name for r in applicable_default_rules(cols, {})] != []            # lookup rule dropped, rest kept
    assert all(r.rule_id != "R5" for r in applicable_default_rules(cols, {}))
    assert applicable_default_rules(["foo"], {}) == []


def test_dry_run_feedback(sample_df, regions_df):
    rules = default_rules()[:2]
    clean = {r.rule_id: run_rule(r, sample_df, {"valid_regions": regions_df}) for r in rules}
    assert dry_run_feedback(rules, clean) is None
    broken = dict(clean, R1=run_rule(default_rules()[0].model_copy(update={"columns": ["zzz"]}), sample_df))
    text = dry_run_feedback(rules, broken)
    assert "could not be executed" in text and "unknown column" in text


def test_severity_ratings_apply_caps(sample_df):
    rules = default_rules()[:2]                                      # both severity: R1 medium, R2 high
    dry = {"R1": run_rule(rules[0], sample_df), "R2": run_rule(rules[1].model_copy(update={"columns": ["zzz"]}), sample_df)}
    r1, r2 = severity_ratings(rules, dry)
    assert (r1.score, r2.score) == (3, 1) and r2.adjustment and "could not be executed" in r2.adjustment
