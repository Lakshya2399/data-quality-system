"""Canned LLM answers for the sample/prod dataset, built from the real profile so they cannot drift."""
import json

from app.default_rules import EMAIL_PATTERN

SEMANTICS = {
    "order_id": ("identifier", "Unique order number.", ["must be unique", "must not be null"]),
    "customer_email": ("email", "Customer contact email.", ["must be a valid email address"]),
    "region_code": ("region_code", "Sales region of the order.", ["must exist in valid_regions.region_code"]),
    "order_amount": ("currency_amount", "Order total.", ["must be >= 0"]),
    "discount_amount": ("currency_amount", "Discount applied to the order.", ["must be >= 0", "must not exceed order_amount"]),
    "created_at": ("timestamp", "When the order was created.", ["must be an ISO 8601 timestamp"]),
    "country": ("country_name", "Country of the region.", []),
    "tax_cap": ("rate", "Maximum tax rate for the region.", ["must be between 0 and 1"]),
}


def enrichment(profile: dict) -> dict:
    """A correct ModelEnrichment answer for the given profile (mutate it to create bad answers)."""
    columns = []
    for t, tp in profile["tables"].items():
        for c in tp["columns"]:
            sem, desc, cons = SEMANTICS.get(c, ("unknown", f"Column {c}.", []))
            columns.append({"table": t, "column": c, "semantic_type": sem, "description": desc, "expected_constraints": cons})
    return {
        "tables": [
            {"table": "sample_orders", "role": "fact", "description": "One row per customer order.", "primary_key": ["order_id"]},
            {"table": "valid_regions", "role": "lookup", "description": "Allowed regions.", "primary_key": ["region_code"]},
        ],
        "columns": columns,
        "relationships": [
            {"child_table": r["child_table"], "child_column": r["child_column"], "parent_table": r["parent_table"],
             "parent_column": r["parent_column"], "description": "Every order must belong to a known region."}
            for r in profile["relationships"]
        ],
        "cross_column_constraints": [
            {"table": "sample_orders", "columns": ["discount_amount", "order_amount"],
             "description": "Discount cannot exceed the order amount."}
        ],
    }


def draft(name, type_, columns, severity="medium", **kw) -> dict:
    d = dict(name=name, type=type_, columns=columns, severity=severity, description=f"{name}.",
             min=None, max=None, pattern=None, formats=None, operator=None, lookup_file=None, lookup_column=None, keep=None)
    d.update(kw)
    return d


def good_drafts() -> list[dict]:
    return [
        draft("order_id_unique", "unique", ["order_id"], "high", keep="first"),
        draft("order_amount_not_null", "not_null", ["order_amount"], "high"),
        draft("order_amount_non_negative", "range", ["order_amount"], "high", min=0),
        draft("discount_not_above_amount", "cross_column_compare", ["discount_amount", "order_amount"], "high", operator="<="),
        draft("region_code_valid", "lookup_in", ["region_code"], "high", lookup_file="valid_regions", lookup_column="region_code"),
        draft("created_at_iso", "date_format", ["created_at"], "medium"),
        draft("customer_email_format", "regex", ["customer_email"], "medium", pattern=EMAIL_PATTERN),
        draft("customer_email_not_null", "not_null", ["customer_email"], "medium"),
    ]


def rules_json(drafts=None) -> str:
    return json.dumps({"rules": good_drafts() if drafts is None else drafts})


def ratings_json(scores: dict[str, int] | list[int], ids=None) -> str:
    if isinstance(scores, list):
        ids = ids or [f"R{i}" for i in range(1, len(scores) + 1)]
        scores = dict(zip(ids, scores))
    return json.dumps({"ratings": [{"rule_id": k, "score": v, "justification": f"Reason for {k}."} for k, v in scores.items()]})
