"""Independently recomputes every anomaly from the CSVs with pandas and compares it to
ground_truth.json. If this passes, the data and the ground truth can be trusted as the
yardstick for the rule engine in Phase 2."""
import json
import re
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import generate_data as gd  # noqa: E402

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")


def failures(df: pd.DataFrame, regions: pd.DataFrame) -> dict[str, list[int]]:
    """Reference implementations, written independently of the generator."""
    amt, disc = df["order_amount"], df["discount_amount"]
    email = df["customer_email"]
    parsed = pd.to_datetime(df["created_at"], format=gd.ISO_FORMAT, errors="coerce")
    return {
        "not_null_customer_email": df.index[email.isna()].tolist(),
        "not_null_order_amount": df.index[amt.isna()].tolist(),
        "order_amount_min_0": df.index[amt < 0].tolist(),
        "discount_le_order_amount": df.index[amt.notna() & (disc > amt)].tolist(),
        "region_code_in_valid_regions": df.index[~df["region_code"].isin(regions["region_code"])].tolist(),
        "unique_order_id_keep_first": df.index[df["order_id"].duplicated(keep="first")].tolist(),
        "created_at_iso_format": df.index[parsed.isna()].tolist(),
        "customer_email_format": df.index[email.notna() & ~email.fillna("").apply(lambda e: bool(EMAIL_RE.match(e)))].tolist(),
    }


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    out = tmp_path_factory.mktemp("data")
    truth = gd.generate(out, seed=42)
    return out, truth


def test_shapes(generated):
    out, _ = generated
    assert len(pd.read_csv(out / "valid_regions.csv")) == 8
    assert len(pd.read_csv(out / "sample_orders.csv")) == 20
    assert len(pd.read_csv(out / "prod_orders.csv")) == 400
    assert list(pd.read_csv(out / "prod_orders.csv").columns) == list(pd.read_csv(out / "sample_orders.csv").columns)


def test_sample_is_clean(generated):
    out, _ = generated
    sample = pd.read_csv(out / "sample_orders.csv")
    regions = pd.read_csv(out / "valid_regions.csv")
    bad = {k: v for k, v in failures(sample, regions).items() if v}
    assert bad == {}


def test_prod_failures_match_ground_truth(generated):
    out, truth = generated
    prod = pd.read_csv(out / "prod_orders.csv")
    regions = pd.read_csv(out / "valid_regions.csv")
    actual = failures(prod, regions)
    for rule, expected in truth["expected_rule_failures"].items():
        assert actual[rule] == expected["row_indices"], rule
        assert len(actual[rule]) == expected["count"], rule


def test_injected_counts(generated):
    _, truth = generated
    for name, count in gd.ANOMALY_COUNTS.items():
        assert truth["injected"][name]["count"] == count, name
    assert truth["expected_rule_failures"]["discount_le_order_amount"]["count"] == 22  # 12 + 10 negatives
    assert truth["injected"]["duplicate_order_id"]["rows_involved"] == 16


def test_anomaly_sets_are_disjoint(generated):
    _, truth = generated
    seen: set[int] = set()
    for name in gd.ANOMALY_COUNTS:
        if name == "duplicate_order_id":
            continue  # duplicate targets may be placed before or after their source row
        rows = set(truth["injected"][name]["row_indices"])
        assert not rows & seen, f"{name} overlaps another anomaly set"
        seen |= rows


def test_deterministic(tmp_path):
    gd.generate(tmp_path / "a", seed=7)
    gd.generate(tmp_path / "b", seed=7)
    for f in ["valid_regions.csv", "sample_orders.csv", "prod_orders.csv", "ground_truth.json"]:
        assert (tmp_path / "a" / f).read_bytes() == (tmp_path / "b" / f).read_bytes()


def test_ground_truth_is_valid_json(generated):
    out, _ = generated
    json.loads((out / "ground_truth.json").read_text())
