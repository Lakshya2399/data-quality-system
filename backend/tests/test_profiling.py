import json

import pytest

import generate_data as gd
from app.data_io import load_csv
from app.profiling import profile_column, profile_tables


@pytest.fixture(scope="module")
def frames(tmp_path_factory):
    out = tmp_path_factory.mktemp("prof")
    gd.generate(out, seed=42)
    return {n: load_csv(out / f"{n}.csv") for n in ("sample_orders", "prod_orders", "valid_regions")}


@pytest.fixture(scope="module")
def sample_profile(frames):
    return profile_tables({"sample_orders": frames["sample_orders"], "valid_regions": frames["valid_regions"]})


def cols(profile, table):
    return profile["tables"][table]["columns"]


def test_types_and_keys(sample_profile):
    c = cols(sample_profile, "sample_orders")
    assert sample_profile["tables"]["sample_orders"]["row_count"] == 20
    assert c["order_id"]["inferred_type"] == "string" and c["order_id"]["is_primary_key_candidate"]
    assert c["order_id"]["top_patterns"][0]["pattern"] == "A-9"
    assert c["order_amount"]["inferred_type"] == "float" and c["order_amount"]["negative_count"] == 0
    assert c["created_at"]["inferred_type"] == "datetime"
    assert c["region_code"]["inferred_type"] == "string" and not c["region_code"]["is_unique"]
    assert cols(sample_profile, "valid_regions")["region_code"]["is_primary_key_candidate"]
    assert cols(sample_profile, "valid_regions")["tax_cap"]["inferred_type"] == "float"


def test_semantic_hints(sample_profile):
    c = cols(sample_profile, "sample_orders")
    assert "email" in c["customer_email"]["semantic_hints"]
    assert "datetime" in c["created_at"]["semantic_hints"]


def test_sample_dates_are_single_format(sample_profile):
    c = cols(sample_profile, "sample_orders")["created_at"]
    assert list(c["date_formats"]) == ["%Y-%m-%d %H:%M:%S"] and c["mixed_date_formats"] is False


def test_foreign_key_detected_and_nothing_else(sample_profile):
    rels = sample_profile["relationships"]
    assert len(rels) == 1
    r = rels[0]
    assert (r["child_table"], r["child_column"]) == ("sample_orders", "region_code")
    assert (r["parent_table"], r["parent_column"]) == ("valid_regions", "region_code")
    assert r["overlap_ratio"] == 1.0 and r["orphan_row_count"] == 0
    assert r["cardinality"] == "many_to_one" and r["confidence"] == "high"


def test_ordering_hint_discount_le_amount(sample_profile):
    hints = sample_profile["tables"]["sample_orders"]["ordering_hints"]
    assert [(h["left"], h["op"], h["right"]) for h in hints] == [("discount_amount", "<=", "order_amount")]


def test_prod_profile_surfaces_the_anomalies(frames):
    c = profile_tables({"prod_orders": frames["prod_orders"]})["tables"]["prod_orders"]["columns"]
    assert c["customer_email"]["null_count"] == 10
    assert c["order_amount"]["null_count"] == 5 and c["order_amount"]["negative_count"] == 10
    assert c["order_id"]["is_unique"] is False
    assert c["created_at"]["mixed_date_formats"] is True and len(c["created_at"]["date_formats"]) == 4
    assert c["created_at"]["unparseable_date_count"] == 0


def test_profile_is_json_serialisable(sample_profile):
    json.dumps(sample_profile, allow_nan=False)


def test_edge_cases():
    import pandas as pd

    assert profile_column(pd.Series([None, None], dtype=object), "x")["inferred_type"] == "unknown"
    mixed = profile_column(pd.Series(["1", "2", "oops"]), "m")
    assert mixed["inferred_type"] == "string" and mixed["numeric_like_pct"] == 66.7
    assert profile_column(pd.Series([True, False]), "b")["inferred_type"] == "boolean"
    assert profile_column(pd.Series(["1", "2", "3"]), "n")["inferred_type"] == "integer"
