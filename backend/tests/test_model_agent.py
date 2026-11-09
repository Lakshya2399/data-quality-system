import copy
import json

import pytest

from app.agents.model_agent import build_data_model
from app.llm import LLMOutputError
from fakes import make_llm
from fixtures import enrichment


def run(profile, *answers):
    llm = make_llm([a if isinstance(a, str) else json.dumps(a) for a in answers])
    return build_data_model(llm, profile), llm


def test_facts_from_profile_semantics_from_llm(profile):
    model, llm = run(profile, enrichment(profile))
    assert model.table_names() == ["sample_orders", "valid_regions"]
    orders = model.tables[0]
    cols = {c.name: c for c in orders.columns}
    assert orders.role == "fact" and orders.primary_key == ["order_id"] and orders.row_count == 20
    assert cols["order_amount"].data_type == "float" and cols["created_at"].data_type == "datetime"   # profile
    assert cols["customer_email"].semantic_type == "email"                                            # LLM
    assert cols["order_id"].observed["is_unique"] is True and cols["order_id"].has_nulls is False
    assert model.tables[1].role == "lookup"
    assert len(llm.calls) == 1


def test_relationship_from_profile_with_llm_description(profile):
    model, _ = run(profile, enrichment(profile))
    (rel,) = model.relationships
    assert (rel.child_table, rel.child_column, rel.parent_table, rel.parent_column) == (
        "sample_orders", "region_code", "valid_regions", "region_code")
    assert rel.cardinality == "many_to_one" and rel.overlap_ratio == 1.0 and "region" in rel.description


def test_cross_column_constraint_profile_wins_and_llm_extras_kept(profile):
    e = enrichment(profile)
    e["cross_column_constraints"].append(
        {"table": "sample_orders", "columns": ["region_code", "order_amount"], "description": "extra idea"})
    model, _ = run(profile, e)
    by_src = {(c.source, tuple(c.columns)) for c in model.cross_column_constraints}
    assert by_src == {("profile", ("discount_amount", "order_amount")), ("llm", ("region_code", "order_amount"))}


def test_llm_cannot_override_profile_facts(profile):
    e = enrichment(profile)
    for c in e["columns"]:
        c["data_type"] = "string"          # extra field the schema does not have
        c["has_nulls"] = True
    model, _ = run(profile, e)
    cols = {c.name: c for c in model.tables[0].columns}
    assert cols["order_amount"].data_type == "float" and cols["order_amount"].has_nulls is False


def test_prompt_contains_profile_and_no_leftover_placeholders(profile):
    _, llm = run(profile, enrichment(profile))
    prompt = llm.calls[0].prompt
    assert '"order_amount"' in prompt and '"relationships"' in prompt
    assert "{{" not in prompt


def test_missing_column_triggers_retry_with_specific_feedback(profile):
    bad = enrichment(profile)
    bad["columns"] = [c for c in bad["columns"] if c["column"] != "discount_amount"]
    model, llm = run(profile, bad, enrichment(profile))
    assert len(llm.calls) == 2
    assert "missing entries" in llm.calls[1].prompt and "sample_orders.discount_amount" in llm.calls[1].prompt
    assert len(model.tables[0].columns) == 6


def test_non_unique_primary_key_rejected(profile):
    bad = enrichment(profile)
    bad["tables"][0]["primary_key"] = ["region_code"]
    _, llm = run(profile, bad, enrichment(profile))
    assert "not unique" in llm.calls[1].prompt


def test_invented_names_rejected(profile):
    bad = enrichment(profile)
    bad["columns"].append({"table": "sample_orders", "column": "ghost", "semantic_type": "x", "description": "x"})
    bad["tables"].append({"table": "ghost_table", "role": "other", "description": "x"})
    bad["cross_column_constraints"].append({"table": "sample_orders", "columns": ["nope"], "description": "x"})
    _, llm = run(profile, bad, enrichment(profile))
    feedback = llm.calls[1].prompt
    assert "unknown columns ['sample_orders.ghost']" in feedback and "unknown tables ['ghost_table']" in feedback
    assert "unknown columns ['nope']" in feedback


def test_schema_errors_are_retried(profile):
    bad = copy.deepcopy(enrichment(profile))
    bad["tables"][0]["role"] = "dimension"          # not in the allowed set
    _, llm = run(profile, bad, enrichment(profile))
    assert "role" in llm.calls[1].prompt


def test_gives_up_after_max_attempts(profile):
    bad = enrichment(profile)
    bad["columns"] = []
    with pytest.raises(LLMOutputError) as ei:
        run(profile, bad, bad, bad)
    assert ei.value.agent == "model_agent" and "missing entries" in ei.value.problems[0]
