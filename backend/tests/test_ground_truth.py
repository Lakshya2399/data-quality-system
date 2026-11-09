"""The Phase 2 acceptance test: hand-written reference rules must reproduce the injected anomalies
EXACTLY (same count, same rows), and must find nothing on the clean sample."""
import pytest

import generate_data as gd
from app.data_io import load_csv
from app.default_rules import default_rules
from app.rule_engine import run_rules


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    out = tmp_path_factory.mktemp("gt")
    truth = gd.generate(out, seed=42)
    return {
        "truth": truth,
        "sample": load_csv(out / "sample_orders.csv"),
        "prod": load_csv(out / "prod_orders.csv"),
        "lookups": {"valid_regions": load_csv(out / "valid_regions.csv")},
    }


def test_prod_matches_ground_truth_exactly(world):
    rules = default_rules()
    results = run_rules(rules, world["prod"], world["lookups"], id_column="order_id")
    assert len(results) == 8
    for rule, res in zip(rules, results):
        expected = world["truth"]["expected_rule_failures"][rule.name]
        assert res.status == "ok", (rule.name, res.error)
        assert res.fail_count == expected["count"], rule.name
        assert res.failed_row_indices == expected["row_indices"], rule.name
        assert res.failed_row_ids == expected["order_ids"], rule.name
        assert res.pass_count == res.total_rows - expected["count"]
        assert len(res.sample_reasons) == min(10, expected["count"])


def test_clean_sample_passes_every_rule(world):
    for rule, res in zip(default_rules(), run_rules(default_rules(), world["sample"], world["lookups"], "order_id")):
        assert res.status == "ok" and res.fail_count == 0, rule.name


def test_null_rows_are_skipped_not_double_counted(world):
    by_name = {r.name: res for r, res in zip(default_rules(), run_rules(default_rules(), world["prod"], world["lookups"]))}
    assert by_name["customer_email_format"].skipped_count == 10   # the null emails
    assert by_name["order_amount_min_0"].skipped_count == 5       # the null amounts
    assert by_name["discount_le_order_amount"].skipped_count == 5
