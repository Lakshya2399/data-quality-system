"""Generate the three input CSVs plus ground_truth.json.

    python backend/scripts/generate_data.py [--seed 42] [--out-dir backend/data] [--prod-rows 400]

Outputs (in --out-dir):
    valid_regions.csv   lookup table, 8 rows
    sample_orders.csv   20 clean rows (no anomalies of any kind)
    prod_orders.csv     N rows with anomalies injected at KNOWN counts
    ground_truth.json   exactly which rows were corrupted, and what each reference rule
                        should flag, so the rule engine can be verified against known answers

Design notes
- Anomaly row sets are disjoint, so each injected defect maps to one reference-rule failure,
  with ONE documented exception: negative-amount rows also fail the discount<=amount check,
  because their discount is 0 and 0 > -x. ground_truth.json accounts for this.
- Everything is driven by a seeded RNG, so the same seed gives byte-identical files.
- row_index is the 0-based position of the data row (header excluded), i.e. the pandas index.
"""
from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "data"

ISO_FORMAT = "%Y-%m-%d %H:%M:%S"
NON_ISO_FORMATS = ["%d/%m/%Y", "%b %d %Y", "%m-%d-%Y"]  # e.g. 06/11/2026, Nov 06 2026, 11-06-2026

REGIONS = [
    ("IN-UP", "India", 0.18),
    ("IN-MH", "India", 0.18),
    ("IN-KA", "India", 0.18),
    ("US-CA", "United States", 0.0975),
    ("US-NY", "United States", 0.08875),
    ("GB-ENG", "United Kingdom", 0.20),
    ("DE-BE", "Germany", 0.19),
    ("AU-NSW", "Australia", 0.10),
]

# Look plausible but are not in valid_regions.csv (includes case and format mismatches)
INVALID_REGIONS = ["XX-00", "IN-ZZ", "US-TX", "UK-LON", "in-up", "FR-IDF", "ZZ-99", "IN_UP", "US-CA ", "BR-SP"]

MALFORMED_EMAILS = [
    "john.doe#example.com",   # '#' instead of '@'
    "jane@",                  # missing domain
    "@example.com",           # missing local part
    "bob smith@example.com",  # whitespace
    "alice@@example.com",     # double '@'
    "carol.example.com",      # no '@'
]

FIRST = ["priya", "arjun", "neha", "rahul", "ananya", "vikram", "sara", "john", "emma", "liam",
         "olivia", "noah", "mia", "lucas", "aisha", "omar", "chen", "yuki", "hans", "elena"]
LAST = ["sharma", "verma", "patel", "singh", "gupta", "smith", "johnson", "brown", "garcia", "khan",
        "muller", "rossi", "tanaka", "wang", "kumar", "mishra", "yadav", "jones", "lee", "clark"]
DOMAINS = ["gmail.com", "outlook.com", "yahoo.com", "example.org", "mail.co"]

# Number of rows to corrupt per anomaly type (prod file)
ANOMALY_COUNTS = {
    "negative_order_amount": 10,
    "discount_gt_order_amount": 12,
    "invalid_region_code": 15,
    "duplicate_order_id": 8,
    "non_iso_created_at": 20,
    "null_customer_email": 10,
    "null_order_amount": 5,
    "malformed_email": 6,
}


# --------------------------------------------------------------------------- clean data
def make_regions() -> pd.DataFrame:
    return pd.DataFrame(REGIONS, columns=["region_code", "country", "tax_cap"])


def make_clean_orders(rng: random.Random, n: int, id_start: int) -> pd.DataFrame:
    start = datetime(2026, 1, 1)
    span = int((datetime(2026, 10, 31) - start).total_seconds())
    stamps = sorted(start + timedelta(seconds=rng.randrange(span)) for _ in range(n))
    region_codes = [r[0] for r in REGIONS]

    rows = []
    for k, ts in enumerate(stamps):
        amount = round(rng.uniform(15, 900), 2)
        discount = 0.0 if rng.random() < 0.4 else round(amount * rng.uniform(0.02, 0.30), 2)
        email = f"{rng.choice(FIRST)}.{rng.choice(LAST)}{rng.randint(1, 99)}@{rng.choice(DOMAINS)}"
        rows.append(
            {
                "order_id": f"ORD-{id_start + k}",
                "customer_email": email,
                "region_code": rng.choice(region_codes),
                "order_amount": amount,
                "discount_amount": discount,
                "created_at": ts.strftime(ISO_FORMAT),
            }
        )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- anomalies
def inject_anomalies(df: pd.DataFrame, rng: random.Random) -> tuple[pd.DataFrame, dict]:
    """Corrupt a copy of df. Returns (corrupted_df, bookkeeping of row indices per anomaly)."""
    df = df.copy()
    n = len(df)
    needed = sum(ANOMALY_COUNTS.values()) + ANOMALY_COUNTS["duplicate_order_id"]  # + dup source rows
    if needed > n:
        raise ValueError(f"Need at least {needed} rows to inject anomalies, got {n}")

    perm = list(range(n))
    rng.shuffle(perm)
    cursor = 0

    def take(k: int) -> list[int]:
        nonlocal cursor
        picked = sorted(perm[cursor:cursor + k])
        cursor += k
        return picked

    idx = {name: take(count) for name, count in ANOMALY_COUNTS.items()}
    dup_sources = take(ANOMALY_COUNTS["duplicate_order_id"])  # untouched clean rows that get copied

    for i in idx["negative_order_amount"]:
        df.at[i, "order_amount"] = -abs(df.at[i, "order_amount"])
        df.at[i, "discount_amount"] = 0.0

    for i in idx["discount_gt_order_amount"]:
        df.at[i, "discount_amount"] = round(df.at[i, "order_amount"] * rng.uniform(1.1, 1.8), 2)

    for i in idx["invalid_region_code"]:
        df.at[i, "region_code"] = rng.choice(INVALID_REGIONS)

    for j, i in enumerate(idx["non_iso_created_at"]):
        ts = datetime.strptime(df.at[i, "created_at"], ISO_FORMAT)
        df.at[i, "created_at"] = ts.strftime(NON_ISO_FORMATS[j % len(NON_ISO_FORMATS)])

    for i in idx["null_customer_email"]:
        df.at[i, "customer_email"] = None

    for i in idx["null_order_amount"]:
        df.at[i, "order_amount"] = float("nan")

    for j, i in enumerate(idx["malformed_email"]):
        df.at[i, "customer_email"] = MALFORMED_EMAILS[j % len(MALFORMED_EMAILS)]

    # Exact duplicate rows: copy a clean source row over the target row
    dup_flagged = []  # the LATER occurrence of each pair (what unique-with-keep-first flags)
    for target, source in zip(idx["duplicate_order_id"], dup_sources):
        for col in df.columns:
            df.at[target, col] = df.at[source, col]
        dup_flagged.append(max(target, source))

    idx["_duplicate_pairs"] = [[t, s] for t, s in zip(idx["duplicate_order_id"], dup_sources)]
    idx["_duplicate_flagged_keep_first"] = sorted(dup_flagged)
    return df, idx


# --------------------------------------------------------------------------- ground truth
def build_ground_truth(df: pd.DataFrame, idx: dict, seed: int) -> dict:
    def ids(rows: list[int]) -> list[str]:
        return [str(df.at[i, "order_id"]) for i in rows]

    def entry(rows: list[int]) -> dict:
        rows = sorted(rows)
        return {"count": len(rows), "row_indices": rows, "order_ids": ids(rows)}

    injected = {name: entry(idx[name]) for name in ANOMALY_COUNTS}
    injected["duplicate_order_id"]["pairs_row_indices"] = idx["_duplicate_pairs"]
    injected["duplicate_order_id"]["rows_involved"] = 2 * len(idx["_duplicate_pairs"])

    # What each reference rule should flag on prod_orders.csv
    expected = {
        "not_null_customer_email": entry(idx["null_customer_email"]),
        "not_null_order_amount": entry(idx["null_order_amount"]),
        "order_amount_min_0": entry(idx["negative_order_amount"]),
        # discount > amount: injected rows PLUS negative-amount rows (discount 0 > negative amount).
        # Rows with null order_amount are skipped (comparison undefined).
        "discount_le_order_amount": entry(idx["discount_gt_order_amount"] + idx["negative_order_amount"]),
        "region_code_in_valid_regions": entry(idx["invalid_region_code"]),
        "unique_order_id_keep_first": entry(idx["_duplicate_flagged_keep_first"]),
        "created_at_iso_format": entry(idx["non_iso_created_at"]),
        "customer_email_format": entry(idx["malformed_email"]),
    }

    return {
        "seed": seed,
        "row_count": len(df),
        "conventions": {
            "row_index": "0-based position of the data row (header excluded)",
            "iso_format": ISO_FORMAT,
            "null_handling": "Null values are flagged only by not_null rules; range, regex, compare, "
                             "lookup and date rules skip nulls.",
            "unique_rule": "Flags repeat occurrences (keep='first'); duplicate_order_id.rows_involved "
                           "gives the count if both rows of each pair are flagged (keep=False).",
            "overlap_note": "negative_order_amount rows also fail discount_le_order_amount.",
        },
        "injected": injected,
        "expected_rule_failures": expected,
    }


# --------------------------------------------------------------------------- orchestration
def generate(out_dir: Path, seed: int = 42, prod_rows: int = 400, sample_rows: int = 20) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)

    regions = make_regions()
    sample = make_clean_orders(rng, sample_rows, id_start=10001)
    prod_clean = make_clean_orders(rng, prod_rows, id_start=20001)
    prod, idx = inject_anomalies(prod_clean, rng)
    truth = build_ground_truth(prod, idx, seed)

    regions.to_csv(out_dir / "valid_regions.csv", index=False)
    sample.to_csv(out_dir / "sample_orders.csv", index=False)
    prod.to_csv(out_dir / "prod_orders.csv", index=False)
    (out_dir / "ground_truth.json").write_text(json.dumps(truth, indent=2))
    return truth


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--prod-rows", type=int, default=400)
    args = ap.parse_args()

    truth = generate(args.out_dir, seed=args.seed, prod_rows=args.prod_rows)
    print(f"Wrote files to {args.out_dir}\n")
    print(f"{'Reference rule':<34}{'expected failures':>18}")
    print("-" * 52)
    for name, e in truth["expected_rule_failures"].items():
        print(f"{name:<34}{e['count']:>18}")


if __name__ == "__main__":
    main()
