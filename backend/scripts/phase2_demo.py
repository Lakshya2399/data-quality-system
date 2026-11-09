"""Phase 2 demo (no LLM): profile the sample, run the reference rules on prod, compare to ground truth.

    python backend/scripts/phase2_demo.py            # from the repo root
    python backend/scripts/phase2_demo.py --json     # also dump the full profile JSON
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.data_io import load_csv  # noqa: E402
from app.default_rules import default_rules  # noqa: E402
from app.profiling import profile_tables  # noqa: E402
from app.rule_engine import run_rules  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--json", action="store_true", help="print the full profile JSON")
args = ap.parse_args()

d = settings.data_dir
for f in ("valid_regions.csv", "sample_orders.csv", "prod_orders.csv", "ground_truth.json"):
    if not (d / f).exists():
        sys.exit(f"{d / f} not found - run: python backend/scripts/generate_data.py")

regions, sample, prod = (load_csv(d / f) for f in ("valid_regions.csv", "sample_orders.csv", "prod_orders.csv"))
truth = json.loads((d / "ground_truth.json").read_text())

profile = profile_tables({"sample_orders": sample, "valid_regions": regions})
print("== Relationships detected on the sample ==")
for r in profile["relationships"]:
    print(f"  {r['child_table']}.{r['child_column']} -> {r['parent_table']}.{r['parent_column']}  "
          f"(overlap {r['overlap_ratio']:.0%}, {r['cardinality']}, confidence {r['confidence']})")
print("== Ordering hints ==")
for h in profile["tables"]["sample_orders"]["ordering_hints"]:
    print(f"  {h['left']} {h['op']} {h['right']}  (holds on {h['rows_compared']} rows)")
if args.json:
    print(json.dumps(profile, indent=2))

lookups = {"valid_regions": regions}
rules = default_rules()
print("\n== Reference rules on the CLEAN sample (expect 0 failures) ==")
for rule, res in zip(rules, run_rules(rules, sample, lookups, "order_id")):
    print(f"  {rule.name:<34}{res.fail_count:>4}")

print("\n== Reference rules on prod_orders.csv vs ground truth ==")
print(f"  {'rule':<34}{'expected':>9}{'actual':>8}  match")
all_ok = True
for rule, res in zip(rules, run_rules(rules, prod, lookups, "order_id")):
    exp = truth["expected_rule_failures"][rule.name]
    ok = res.status == "ok" and res.failed_row_indices == exp["row_indices"]
    all_ok &= ok
    print(f"  {rule.name:<34}{exp['count']:>9}{res.fail_count:>8}  {'OK' if ok else 'MISMATCH'}")
    if res.sample_reasons:
        print(f"      e.g. {res.sample_reasons[0]['row_id']}: {res.sample_reasons[0]['reason']}")
print("\nALL RULES MATCH GROUND TRUTH" if all_ok else "\nMISMATCH - investigate")
