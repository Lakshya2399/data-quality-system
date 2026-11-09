import pytest
from pydantic import ValidationError

from app.schemas import Rule, RuleType


def make(**kw):
    base = dict(rule_id="R1", name="x", type="not_null", columns=["a"])
    base.update(kw)
    return Rule(**base)


def test_valid_rules_for_every_type():
    make()
    make(type="unique", columns=["a", "b"], params={"keep": "all"})
    make(type="range", params={"min": 0})
    make(type="range", params={"max": 10.5})
    make(type="regex", params={"pattern": r"\d+"})
    make(type="date_format", params={"formats": ["%Y-%m-%d"]})
    make(type="date_format")
    make(type="cross_column_compare", columns=["a", "b"], params={"operator": "<="})
    make(type="lookup_in", params={"lookup_file": "x.csv", "lookup_column": "c"})


@pytest.mark.parametrize(
    "kw",
    [
        dict(columns=[]),
        dict(columns=["a", "a"]),
        dict(type="bogus"),
        dict(severity="critical"),
        dict(type="unique", params={"keep": "last"}),
        dict(type="range"),                                           # no min/max
        dict(type="range", params={"min": "zero"}),
        dict(type="range", params={"min": True}),
        dict(type="range", params={"min": 5, "max": 1}),
        dict(type="range", columns=["a", "b"], params={"min": 0}),
        dict(type="regex"),
        dict(type="regex", params={"pattern": "("}),                  # invalid regex
        dict(type="regex", params={"pattern": "a" * 301}),
        dict(type="date_format", params={"formats": []}),
        dict(type="date_format", params={"formats": ["yyyy-mm-dd"]}),  # not a strptime format
        dict(type="cross_column_compare", columns=["a"], params={"operator": "<="}),
        dict(type="cross_column_compare", columns=["a", "b"], params={"operator": "~"}),
        dict(type="lookup_in", params={"lookup_file": "x.csv"}),
    ],
)
def test_invalid_rules_rejected(kw):
    with pytest.raises(ValidationError):
        make(**kw)


def test_round_trip_json():
    r = make(type="range", params={"min": 0})
    assert Rule.model_validate_json(r.model_dump_json()) == r
    assert r.type is RuleType.RANGE
