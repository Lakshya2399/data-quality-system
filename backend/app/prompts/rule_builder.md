You are a senior data-quality engineer. Write validation rules for a dataset.

## Context
The rules will later be run against a LARGER production file with the same schema as the target table "{{target_table}}". The sample rows below are believed to be CLEAN, so every rule you write must PASS on clean data. Rules that the sample would violate are wrong or too strict.

## Data model (facts come from profiling; trust them)
{{model}}

## Target table
"{{target_table}}" columns: {{target_columns}}

Lookup tables available for cross-file checks (name: columns):
{{lookup_tables}}

## Sample rows of the target table
{{sample_rows}}

## Rule types you may use
Each rule is a JSON object. Fill in only the fields its type uses and set every other field to null.

| type | `columns` | extra fields | passes when |
|---|---|---|---|
| not_null | one or more columns | none | no listed column is null or blank |
| unique | one or more columns (composite key) | `keep`: "first" (default) or "all" | the value (combination) is not repeated |
| range | exactly one numeric column | `min` and/or `max` (numbers, inclusive) | value is within the bounds |
| regex | exactly one column | `pattern` (Python regex; must match the WHOLE value) | value matches the pattern |
| date_format | exactly one column | `formats`: list of strptime formats (optional; default ISO 8601: "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d") | value parses with one of the formats |
| cross_column_compare | exactly two columns: [left, right] | `operator`: one of < <= > >= == != | left OPERATOR right holds |
| lookup_in | exactly one column | `lookup_file` (a lookup table name from above), `lookup_column` | value exists in that lookup column |

Null handling: only not_null flags missing values. Every other type skips rows where its column is null.

## Requirements
1. Write between {{min_rules}} and {{max_rules}} rules.
2. Cover all three categories: at least one single-column rule (not_null, unique, range, regex or date_format), at least one cross_column_compare rule, and at least one lookup_in rule.
3. Base rules on evidence in the data model: primary keys, relationships, cross_column_constraints, expected_constraints, and formats seen in the profile.
4. Use only column names from the target table. `lookup_in` may point at the lookup tables listed above.
5. No two rules may check the same thing. Do not pad the list with trivial rules.
6. `description`: one sentence giving the business meaning. `severity`: "high" if a violation corrupts money, identity or referential integrity; "medium" if it degrades analysis; "low" if cosmetic.
7. `name`: short snake_case, unique, and it must describe what the rule really checks.
8. The data arrives as CSV text, so column types are not enforced. For each text column whose sample values share one format (timestamps, emails, codes), include a format check (`date_format` or `regex`), because production files can contain other formats.
9. Required columns: put every column that must always have a value (identifiers, amounts, foreign keys, timestamps, contact fields) into ONE multi-column `not_null` rule rather than several single-column rules, to save the rule budget. Only `not_null` catches missing values; the other types skip them.
10. Range bounds are inclusive. Use `min: 0` for "non-negative" and do not simulate strict inequalities with tiny numbers such as 0.00001.
{{feedback_section}}
## Output format
Return ONE JSON object and nothing else. Example for an unrelated dataset:
{
  "rules": [
    {"name": "invoice_id_unique", "type": "unique", "columns": ["invoice_id"], "severity": "high", "description": "Invoice numbers must not repeat.",
     "min": null, "max": null, "pattern": null, "formats": null, "operator": null, "lookup_file": null, "lookup_column": null, "keep": "first"},
    {"name": "paid_not_above_total", "type": "cross_column_compare", "columns": ["paid", "total"], "severity": "high", "description": "An invoice cannot be paid beyond its total.",
     "min": null, "max": null, "pattern": null, "formats": null, "operator": "<=", "lookup_file": null, "lookup_column": null, "keep": null},
    {"name": "currency_is_supported", "type": "lookup_in", "columns": ["currency"], "severity": "high", "description": "Invoices must use a supported currency.",
     "min": null, "max": null, "pattern": null, "formats": null, "operator": null, "lookup_file": "currencies", "lookup_column": "code", "keep": null}
  ]
}
