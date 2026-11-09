import json

import pytest

from app.agent_schemas import RuleDraft
from app.agents.rule_builder import build_rules, check_rule_set, sample_rows_from_df
from app.llm import LLMOutputError
from app.rule_engine import run_rules
from app.schemas import RuleType
from fakes import make_llm
from fixtures import draft, good_drafts, rules_json


def build(model, sample_df, *answers, **kw):
    llm = make_llm([a if isinstance(a, str) else json.dumps(a) for a in answers])
    rows = sample_rows_from_df(sample_df)
    return build_rules(llm, model, rows, "sample_orders", **kw), llm


def test_good_answer_becomes_validated_rules(data_model, sample_df):
    rules, llm = build(data_model, sample_df, rules_json())
    assert [r.rule_id for r in rules] == [f"R{i}" for i in range(1, 9)]
    by = {r.name: r for r in rules}
    assert by["order_amount_non_negative"].params == {"min": 0} and by["order_amount_non_negative"].type == RuleType.RANGE
    assert by["discount_not_above_amount"].params == {"operator": "<="}
    assert by["discount_not_above_amount"].columns == ["discount_amount", "order_amount"]
    assert by["region_code_valid"].params == {"lookup_file": "valid_regions", "lookup_column": "region_code"}
    assert by["order_id_unique"].params == {"keep": "first"} and by["created_at_iso"].params == {}
    assert len(llm.calls) == 1


def test_good_rules_execute_cleanly_on_the_sample(data_model, sample_df, regions_df):
    rules, _ = build(data_model, sample_df, rules_json())
    for rule, res in zip(rules, run_rules(rules, sample_df, {"valid_regions": regions_df}, "order_id")):
        assert res.status == "ok" and res.fail_count == 0, rule.name


def test_prompt_content(data_model, sample_df):
    _, llm = build(data_model, sample_df, rules_json())
    p = llm.calls[0].prompt
    assert "order_id, customer_email, region_code, order_amount, discount_amount, created_at" in p
    assert "- valid_regions: region_code, country, tax_cap" in p
    assert "ORD-10001" in p                                  # sample rows are included
    assert "between 5 and 8 rules" in p and "{{" not in p and "Feedback on your previous attempt" not in p


def test_feedback_is_included(data_model, sample_df):
    _, llm = build(data_model, sample_df, rules_json(), feedback="R3 failed on 4 clean rows")
    assert "Feedback on your previous attempt" in llm.calls[0].prompt and "R3 failed on 4 clean rows" in llm.calls[0].prompt


def test_unknown_target_table_fails_before_calling_llm(data_model, sample_df):
    llm = make_llm([])
    with pytest.raises(ValueError, match="not in data model"):
        build_rules(llm, data_model, [], "nope")
    assert llm.calls == []


# ---- draft normalisation ----------------------------------------------------
def test_draft_normalisation():
    d = RuleDraft.model_validate({
        "rule_id": "ignored", "name": "Order ID  Unique!", "type": "not_null",
        "columns": "order_id", "severity": "HIGH"})
    assert d.name == "order_id_unique" and d.columns == ["order_id"] and d.severity == "high"
    assert d.to_rule("R9").rule_id == "R9"          # the LLM's own rule_id is ignored; ours is assigned


@pytest.mark.parametrize("written, normalised", [("≤", "<="), ("≥", ">="), ("=", "=="), ("<>", "!="), (" < ", "<")])
def test_operator_aliases(written, normalised):
    d = RuleDraft.model_validate(draft("c", "cross_column_compare", ["a", "b"], operator=written))
    assert d.to_rule("R1").params == {"operator": normalised}


def test_irrelevant_fields_are_ignored_and_integral_floats_tidied():
    rule = RuleDraft.model_validate(draft("x", "range", ["a"], min=0.0, max=99.5, pattern="ignored", operator="<")).to_rule("R1")
    assert rule.params == {"min": 0, "max": 99.5} and isinstance(rule.params["min"], int)


# ---- feedback loop ----------------------------------------------------------
def retry_case(data_model, sample_df, bad_drafts, expected_in_feedback):
    rules, llm = build(data_model, sample_df, {"rules": bad_drafts}, rules_json())
    assert len(llm.calls) == 2 and len(rules) == 8
    for text in expected_in_feedback:
        assert text in llm.calls[1].prompt, text


def test_missing_lookup_category(data_model, sample_df):
    bad = [d for d in good_drafts() if d["type"] != "lookup_in"] + [draft("amount_max", "range", ["order_amount"], max=1e6)]
    retry_case(data_model, sample_df, bad, ["coverage: no lookup_in rule"])


def test_missing_cross_column_category(data_model, sample_df):
    bad = [d for d in good_drafts() if d["type"] != "cross_column_compare"] + [draft("disc_min", "range", ["discount_amount"], min=0)]
    retry_case(data_model, sample_df, bad, ["coverage: no cross_column_compare"])


def test_unknown_column_and_lookup(data_model, sample_df):
    bad = good_drafts()
    bad[1] = draft("ghost", "not_null", ["ghost_col"])
    bad[4] = draft("bad_lookup", "lookup_in", ["region_code"], lookup_file="nope.csv", lookup_column="x")
    retry_case(data_model, sample_df, bad, ["unknown column(s) ['ghost_col']", "unknown lookup_file 'nope.csv'"])


def test_cannot_use_a_lookup_table_column_as_target_column(data_model, sample_df):
    bad = good_drafts()
    bad[1] = draft("tax_not_null", "not_null", ["tax_cap"])
    retry_case(data_model, sample_df, bad, ["unknown column(s) ['tax_cap']"])


def test_invalid_params_reported_per_rule(data_model, sample_df):
    bad = good_drafts()
    bad[6] = draft("broken_regex", "regex", ["customer_email"], pattern="(")
    bad[2] = draft("no_bounds", "range", ["order_amount"])
    retry_case(data_model, sample_df, bad, ["rule #7 (broken_regex", "invalid regex", "rule #3 (no_bounds", "min and/or params.max"])


def test_rule_count_bounds(data_model, sample_df):
    retry_case(data_model, sample_df, good_drafts()[:3], ["write between 5 and 8 valid rules; got 3"])
    nine = good_drafts() + [draft("extra", "range", ["discount_amount"], min=0)]
    retry_case(data_model, sample_df, nine, ["got 9"])


def test_duplicates_rejected(data_model, sample_df):
    bad = good_drafts()
    bad[7] = dict(bad[0], name="order_id_unique_again")           # same check, different name
    bad[5] = dict(bad[5], name=bad[1]["name"])                    # same name as another rule
    retry_case(data_model, sample_df, bad, ["identical to R1", "duplicate rule name"])


def test_unknown_type_is_a_schema_error(data_model, sample_df):
    bad = good_drafts()
    bad[0] = dict(bad[0], type="fuzzy_match")
    retry_case(data_model, sample_df, bad, ["rules.0.type"])


def test_gives_up_after_max_attempts(data_model, sample_df):
    with pytest.raises(LLMOutputError) as ei:
        build(data_model, sample_df, *[{"rules": good_drafts()[:2]}] * 3)
    assert ei.value.agent == "rule_builder"


def test_check_rule_set_coverage_can_be_disabled(data_model):
    from app.agents.rule_builder import drafts_to_rules
    rules, _ = drafts_to_rules([RuleDraft.model_validate(d) for d in good_drafts()[:5] if d["type"] == "not_null"])
    cols = data_model.columns_of("sample_orders")
    assert check_rule_set(rules, cols, {}, min_rules=1, max_rules=8, require_coverage=False) == []
