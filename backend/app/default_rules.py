"""Deterministic reference rule set.

Used for (1) verifying the engine against ground_truth.json and (2) as the fallback rule set
if the LLM rule builder fails after its retries. Rule names match the keys in ground_truth.json.
"""
from __future__ import annotations

from .schemas import Rule, RuleType

EMAIL_PATTERN = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+"


def default_rules() -> list[Rule]:
    return [
        Rule(rule_id="R1", name="not_null_customer_email", type=RuleType.NOT_NULL, columns=["customer_email"],
             severity="medium", description="Every order must have a customer email"),
        Rule(rule_id="R2", name="not_null_order_amount", type=RuleType.NOT_NULL, columns=["order_amount"],
             severity="high", description="Every order must have an amount"),
        Rule(rule_id="R3", name="order_amount_min_0", type=RuleType.RANGE, columns=["order_amount"],
             params={"min": 0}, severity="high", description="Order amount cannot be negative"),
        Rule(rule_id="R4", name="discount_le_order_amount", type=RuleType.CROSS_COLUMN_COMPARE,
             columns=["discount_amount", "order_amount"], params={"operator": "<="}, severity="high",
             description="Discount cannot exceed the order amount"),
        Rule(rule_id="R5", name="region_code_in_valid_regions", type=RuleType.LOOKUP_IN, columns=["region_code"],
             params={"lookup_file": "valid_regions.csv", "lookup_column": "region_code"}, severity="high",
             description="region_code must exist in valid_regions"),
        Rule(rule_id="R6", name="unique_order_id_keep_first", type=RuleType.UNIQUE, columns=["order_id"],
             params={"keep": "first"}, severity="high", description="order_id must be unique"),
        Rule(rule_id="R7", name="created_at_iso_format", type=RuleType.DATE_FORMAT, columns=["created_at"],
             severity="medium", description="created_at must be an ISO 8601 timestamp"),
        Rule(rule_id="R8", name="customer_email_format", type=RuleType.REGEX, columns=["customer_email"],
             params={"pattern": EMAIL_PATTERN}, severity="medium", description="customer_email must look like an email"),
    ]
