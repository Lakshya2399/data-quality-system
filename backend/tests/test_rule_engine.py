import numpy as np
import pandas as pd

from app.rule_engine import check_rule_columns, run_rule, run_rules
from app.schemas import Rule


def rule(type_, columns, **params):
    return Rule(rule_id="R", name="t", type=type_, columns=columns, params=params)


def df_of(**cols):
    return pd.DataFrame(cols)


# ---- not_null ---------------------------------------------------------------
def test_not_null_flags_nan_none_and_blank():
    df = df_of(a=["x", None, np.nan, "  ", ""])
    res = run_rule(rule("not_null", ["a"]), df)
    assert res.failed_row_indices == [1, 2, 3, 4]
    assert res.pass_count == 1 and res.fail_count == 4


def test_not_null_multiple_columns_any_null_fails():
    df = df_of(a=[1, None, 3], b=[1, 2, None])
    res = run_rule(rule("not_null", ["a", "b"]), df)
    assert res.failed_row_indices == [1, 2]


# ---- unique -----------------------------------------------------------------
def test_unique_keep_first_vs_all_and_nulls_skipped():
    df = df_of(a=["x", "y", "x", None, None, "x"])
    first = run_rule(rule("unique", ["a"]), df)
    assert first.failed_row_indices == [2, 5]
    assert first.skipped_count == 2          # repeated nulls are not duplicates
    all_ = run_rule(rule("unique", ["a"], keep="all"), df)
    assert all_.failed_row_indices == [0, 2, 5]


def test_unique_composite():
    df = df_of(a=[1, 1, 1], b=["p", "q", "p"])
    assert run_rule(rule("unique", ["a", "b"]), df).failed_row_indices == [2]


# ---- range ------------------------------------------------------------------
def test_range_bounds_inclusive_nulls_skipped_and_text_fails():
    df = df_of(a=["5", "0", "-1", "10", "11", None, "abc"])
    res = run_rule(rule("range", ["a"], min=0, max=10), df)
    assert res.failed_row_indices == [2, 4, 6]
    assert res.skipped_count == 1
    assert "not numeric" in res.sample_reasons[-1]["reason"]


def test_range_min_only():
    res = run_rule(rule("range", ["a"], min=0), df_of(a=[-0.01, 0, 100]))
    assert res.failed_row_indices == [0]


# ---- regex ------------------------------------------------------------------
def test_regex_uses_fullmatch():
    df = df_of(a=["abc123", "abc", "abc123x", None])
    res = run_rule(rule("regex", ["a"], pattern=r"[a-z]+\d+"), df)
    assert res.failed_row_indices == [1, 2]
    assert res.skipped_count == 1


# ---- date_format ------------------------------------------------------------
def test_date_format_mixed_values():
    df = df_of(a=["2026-01-05 10:00:00", "2026-01-05", "06/11/2026", "Nov 06 2026", "2026-13-01", None])
    res = run_rule(rule("date_format", ["a"]), df)      # default ISO formats
    assert res.failed_row_indices == [2, 3, 4]
    custom = run_rule(rule("date_format", ["a"], formats=["%d/%m/%Y"]), df)
    assert custom.failed_row_indices == [0, 1, 3, 4]


# ---- cross_column_compare ---------------------------------------------------
def test_compare_operators_and_null_skip():
    df = df_of(d=[1, 5, None, 3, 2], a=[2, 4, 9, None, 2])
    assert run_rule(rule("cross_column_compare", ["d", "a"], operator="<="), df).failed_row_indices == [1]
    assert run_rule(rule("cross_column_compare", ["d", "a"], operator="<"), df).failed_row_indices == [1, 4]
    assert run_rule(rule("cross_column_compare", ["d", "a"], operator="=="), df).failed_row_indices == [0, 1]
    res = run_rule(rule("cross_column_compare", ["d", "a"], operator="<="), df)
    assert res.skipped_count == 2


def test_compare_non_numeric_text_fails_with_reason():
    df = df_of(d=["1", "x"], a=[2, 2])
    res = run_rule(rule("cross_column_compare", ["d", "a"], operator="<="), df)
    assert res.failed_row_indices == [1]
    assert "non-numeric" in res.sample_reasons[0]["reason"]


# ---- lookup_in --------------------------------------------------------------
def test_lookup_in_case_sensitive_nulls_skipped_and_name_normalised():
    lk = {"valid_regions": pd.DataFrame({"region_code": ["IN-UP", "US-CA"]})}
    df = df_of(r=["IN-UP", "in-up", "XX", None, "US-CA "])
    for name in ("valid_regions", "valid_regions.csv"):
        res = run_rule(rule("lookup_in", ["r"], lookup_file=name, lookup_column="region_code"), df, lk)
        assert res.failed_row_indices == [1, 2, 4]
        assert res.skipped_count == 1


# ---- errors are reported, never raised --------------------------------------
def test_unknown_column_is_error_result():
    res = run_rule(rule("not_null", ["nope"]), df_of(a=[1]))
    assert res.status == "error" and "unknown column" in res.error and res.pass_rate is None


def test_missing_lookup_is_error_result():
    r = rule("lookup_in", ["a"], lookup_file="missing.csv", lookup_column="c")
    res = run_rule(r, df_of(a=[1]), lookups={})
    assert res.status == "error" and "unknown lookup_file" in res.error


def test_run_rules_continues_after_a_bad_rule():
    good, bad = rule("not_null", ["a"]), rule("not_null", ["zzz"])
    results = run_rules([bad, good], df_of(a=[1, None]))
    assert [r.status for r in results] == ["error", "ok"]


# ---- result bookkeeping -----------------------------------------------------
def test_ids_reasons_and_index_reset():
    df = df_of(id=["A", "B", "C"], v=[1, -1, -2]).iloc[[2, 1, 0]]       # non-default index
    res = run_rule(rule("range", ["v"], min=0), df, id_column="id", max_reasons=1)
    assert res.failed_row_indices == [0, 1]                              # positions, not original labels
    assert res.failed_row_ids == ["C", "B"]
    assert len(res.sample_reasons) == 1 and res.sample_reasons[0]["row_id"] == "C"
    assert res.pass_rate == 1 / 3


def test_without_id_column_uses_row_index():
    res = run_rule(rule("range", ["v"], min=0), df_of(v=[-1, 1]))
    assert res.failed_row_ids == ["0"]


def test_empty_dataframe():
    res = run_rule(rule("not_null", ["a"]), pd.DataFrame({"a": []}))
    assert res.status == "ok" and res.total_rows == 0 and res.fail_count == 0


def test_check_rule_columns():
    r = rule("lookup_in", ["a"], lookup_file="lk.csv", lookup_column="zz")
    assert check_rule_columns(r, ["a"], {"lk": ["c"]})
    assert not check_rule_columns(r, ["a"], {"lk": ["zz"]})
