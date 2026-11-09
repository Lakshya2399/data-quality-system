You are a senior data engineer documenting a dataset for a data-quality system.

You are given a PROFILE of one or more tables. The profile was computed by code from a clean sample: treat every number, type, key and relationship in it as fact. Your job is to add MEANING: what each column represents, what must hold for valid data, and how the tables relate. Do not recompute statistics and do not contradict the profile.

## Profile
{{profile}}

## What to produce
1. `tables`: one entry per table with
   - `role`: "fact" (transactions/events), "lookup" (reference data) or "other"
   - `description`: one sentence
   - `primary_key`: list of column names that uniquely identify a row (each must be unique in the profile), or [] if none
2. `columns`: one entry for EVERY column of EVERY table, using the exact table and column names:
   - `semantic_type`: short snake_case label (e.g. email, identifier, currency_amount, timestamp, region_code, country_name, rate)
   - `description`: one sentence
   - `expected_constraints`: 0-4 short statements about what must hold for VALID data in general (e.g. "must be >= 0", "must be unique"), not merely what was observed. Only state constraints the evidence supports.
3. `relationships`: one entry per item in profile.relationships, with a one-sentence `description`.
4. `cross_column_constraints`: constraints between columns of the same table, taken from profile `ordering_hints` or clearly implied by the column meanings. Each has `table`, `columns` and `description`. Use [] if there are none.

## Rules
- Use only table and column names that appear in the profile.
- Do not invent business rules the data gives no evidence for.
- Return ONE JSON object and nothing else.

## Output format (example for an unrelated dataset)
{
  "tables": [
    {"table": "invoices", "role": "fact", "description": "One row per invoice issued to a customer.", "primary_key": ["invoice_id"]},
    {"table": "currencies", "role": "lookup", "description": "Supported ISO currency codes.", "primary_key": ["code"]}
  ],
  "columns": [
    {"table": "invoices", "column": "invoice_id", "semantic_type": "identifier", "description": "Unique invoice number.", "expected_constraints": ["must be unique", "must not be null"]},
    {"table": "invoices", "column": "total", "semantic_type": "currency_amount", "description": "Invoice total including tax.", "expected_constraints": ["must be >= 0"]},
    {"table": "invoices", "column": "paid", "semantic_type": "currency_amount", "description": "Amount paid so far.", "expected_constraints": ["must be >= 0"]},
    {"table": "invoices", "column": "currency", "semantic_type": "currency_code", "description": "Currency of the amounts.", "expected_constraints": ["must exist in currencies.code"]},
    {"table": "currencies", "column": "code", "semantic_type": "currency_code", "description": "ISO 4217 code.", "expected_constraints": ["must be unique"]}
  ],
  "relationships": [
    {"child_table": "invoices", "child_column": "currency", "parent_table": "currencies", "parent_column": "code", "description": "Each invoice is issued in a supported currency."}
  ],
  "cross_column_constraints": [
    {"table": "invoices", "columns": ["paid", "total"], "description": "The paid amount must not exceed the invoice total."}
  ]
}
