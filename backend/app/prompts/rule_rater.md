You are a principal data-quality engineer reviewing candidate validation rules before they run on production data.

Rate EACH rule from 1 to 5 and justify the score in one or two sentences that cite concrete evidence: the rule itself, the data model, or its dry-run result.

## Rubric
- 5 = Critical and executable. Guards identity, money or referential integrity (for example key uniqueness, required amounts, foreign keys) and ran cleanly in the dry-run.
- 4 = Important and executable. Catches a realistic, damaging defect and ran cleanly.
- 3 = Useful but weak: low impact, very narrow, or only thinly supported by the data.
- 2 = Marginal: likely too strict or too loose, heavily overlaps a stronger rule, or fails on the clean sample.
- 1 = Redundant, wrong, or not executable.

## How to rate
- The dry-run executed the rule against the CLEAN sample. A rule that errors scores 1; a rule that fails on clean rows scores at most 2 (this cap is also enforced automatically).
- `skipped` counts rows the rule did not evaluate because the value was null; that is normal.
- The data comes from CSV files, so every value is text. A column labelled "datetime", "integer" or "float" in the data model only means its sample values parse that way; nothing enforces it, and the larger production file may contain other formats, missing values or malformed numbers. Format, type, null and range checks are therefore NOT redundant just because the data model gives a column a type.
- The sample is clean, so it cannot show how often a rule will fire in production. Judge a rule by the damage a violation would do, not by the fact that the sample contains no violations.
- If two rules overlap, give the weaker one the lower score.
- Use the full scale. Reserve 5 for the few rules that matter most. If every rule gets the same score you have not discriminated.
- Rate every rule exactly once and copy its `rule_id` exactly.

## Data model summary
{{model_summary}}

## Candidate rules with dry-run results
{{rules}}

## Output format
Return ONE JSON object and nothing else:
{"ratings": [{"rule_id": "R1", "score": 5, "justification": "Primary key; ran cleanly and duplicates would double count revenue."}]}
