import json

import pytest

from app.agents.rule_rater import apply_caps, rate_rules, rating_warnings
from app.agent_schemas import RatingItem
from app.default_rules import default_rules
from app.llm import LLMOutputError
from app.rule_engine import run_rules
from app.schemas import RuleResult
from fakes import make_llm
from fixtures import ratings_json

SCORES = [5, 5, 5, 4, 5, 3, 4, 2]


@pytest.fixture(scope="module")
def rules():
    return default_rules()


@pytest.fixture(scope="module")
def clean_runs(rules, sample_df, regions_df):
    return {r.rule_id: res for r, res in zip(rules, run_rules(rules, sample_df, {"valid_regions": regions_df}, "order_id"))}


def rate(model, rules, runs, *answers):
    llm = make_llm(list(answers))
    return rate_rules(llm, rules, runs, model), llm


def test_ratings_returned_in_rule_order_and_untouched_when_clean(data_model, rules, clean_runs):
    ratings, llm = rate(data_model, rules, clean_runs, ratings_json(list(reversed(SCORES))[::-1]))
    assert [r.rule_id for r in ratings] == [f"R{i}" for i in range(1, 9)]
    assert [r.score for r in ratings] == SCORES
    assert all(r.adjustment is None and r.score == r.llm_score for r in ratings)
    assert ratings[0].justification == "Reason for R1."


def test_prompt_has_rubric_dry_runs_and_model_summary(data_model, rules, clean_runs):
    _, llm = rate(data_model, rules, clean_runs, ratings_json(SCORES))
    p = llm.calls[0].prompt
    assert "Rubric" in p and "relationship: sample_orders.region_code -> valid_regions.region_code" in p
    assert '"rule_id": "R8"' in p and '"dry_run"' in p and '"failed": 0' in p and "{{" not in p


def test_fails_on_clean_sample_capped_at_2_and_errors_at_1(data_model, rules, clean_runs):
    runs = dict(clean_runs)
    runs["R4"] = RuleResult(rule_id="R4", total_rows=20, fail_count=3, pass_count=17, failed_row_indices=[1, 2, 3],
                            sample_reasons=[{"row_index": 1, "row_id": "x", "reason": "too strict"}])
    runs["R5"] = RuleResult(rule_id="R5", status="error", error="boom", total_rows=20)
    ratings, llm = rate(data_model, rules, runs, ratings_json([5] * 8))
    by = {r.rule_id: r for r in ratings}
    assert by["R4"].score == 2 and by["R4"].llm_score == 5 and "fails on 3 of 20" in by["R4"].adjustment
    assert by["R4"].justification.endswith("]") and "capped to 2" in by["R4"].justification
    assert by["R5"].score == 1 and "boom" in by["R5"].adjustment
    assert by["R1"].score == 5
    assert "too strict" in llm.calls[0].prompt and '"status": "error"' in llm.calls[0].prompt


def test_caps_never_raise_a_low_score():
    item = [RatingItem(rule_id="R1", score=1, justification="redundant rule")]
    bad = {"R1": RuleResult(rule_id="R1", total_rows=5, fail_count=2, pass_count=3)}
    (r,) = apply_caps(item, default_rules()[:1], bad)
    assert r.score == 1 and r.adjustment is None


def test_no_dry_run_means_no_cap(rules):
    items = [RatingItem(rule_id=r.rule_id, score=5, justification="fine") for r in rules]
    assert all(r.score == 5 for r in apply_caps(items, rules, {}))


@pytest.mark.parametrize(
    "bad, expected",
    [
        (lambda: json.loads(ratings_json(SCORES[:6])), "missing ratings for ['R7', 'R8']"),
        (lambda: json.loads(ratings_json(SCORES + [3], ids=[f"R{i}" for i in range(1, 9)] + ["R99"])), "unknown rule_ids ['R99']"),
        (lambda: {"ratings": json.loads(ratings_json(SCORES))["ratings"] + [{"rule_id": "R1", "score": 3, "justification": "again"}]},
         "duplicate ratings for ['R1']"),
        (lambda: {"ratings": [{"rule_id": "R1", "score": 7, "justification": "too high"}]}, "ratings.0.score"),
        (lambda: {"ratings": [{"rule_id": "R1", "score": 3, "justification": ""}]}, "justification"),
    ],
)
def test_bad_ratings_are_retried_with_feedback(data_model, rules, clean_runs, bad, expected):
    ratings, llm = rate(data_model, rules, clean_runs, json.dumps(bad()), ratings_json(SCORES))
    assert len(llm.calls) == 2 and expected in llm.calls[1].prompt
    assert [r.score for r in ratings] == SCORES


def test_gives_up_when_ratings_never_complete(data_model, rules, clean_runs):
    with pytest.raises(LLMOutputError) as ei:
        rate(data_model, rules, clean_runs, *[ratings_json([5])] * 3)
    assert ei.value.agent == "rule_rater"


def test_numeric_strings_and_floats_are_accepted(data_model, rules, clean_runs):
    answer = {"ratings": [{"rule_id": r.rule_id, "score": "4" if i % 2 else 5.0, "justification": "ok ok"} for i, r in enumerate(rules)]}
    ratings, _ = rate(data_model, rules, clean_runs, json.dumps(answer))
    assert {r.score for r in ratings} == {4, 5}


def test_rating_warnings():
    def mk(scores):
        return apply_caps([RatingItem(rule_id=f"R{i}", score=s, justification="because") for i, s in enumerate(scores, 1)],
                          [type("R", (), {"rule_id": f"R{i}"}) for i in range(1, len(scores) + 1)], {})
    assert any("same score" in w for w in rating_warnings(mk([5, 5, 5, 5])))
    assert any("nothing will be executed" in w for w in rating_warnings(mk([3, 2, 3, 1])))
    assert len(rating_warnings(mk([3, 2, 3, 1]))) == 1
    assert rating_warnings(mk([5, 4, 3, 2])) == []
