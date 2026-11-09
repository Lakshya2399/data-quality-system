"""Regression guards for prompt guidance learned from live runs (a live LLM is not available in CI,
so these only check that the guidance is present, not that the model obeys it)."""
from app.prompts import render


def test_rater_is_told_csv_types_are_not_enforced():
    text = render("rule_rater", model_summary="x", rules="y")
    assert "every value is text" in text and "NOT redundant" in text
    assert "cannot show how often a rule will fire in production" in text


def test_builder_is_told_to_cover_required_columns_and_formats():
    text = render("rule_builder", target_table="t", model="m", target_columns="a", lookup_tables="l",
                  sample_rows="[]", min_rules="5", max_rules="8", feedback_section="")
    assert "ONE multi-column `not_null` rule" in text
    assert "format check" in text and "0.00001" in text


def test_model_summary_marks_datetime_columns_as_text(data_model):
    summary = data_model.summary_text()
    assert "created_at (datetime stored as text" in summary
    assert "order_amount (float" in summary
