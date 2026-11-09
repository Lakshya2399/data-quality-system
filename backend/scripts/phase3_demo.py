"""Phase 3 demo: run the three agents LIVE against Gemini on the sample files (no graph/DB yet).

    python backend/scripts/phase3_demo.py [--out backend/data/phase3_output.json]

Needs GEMINI_API_KEY in .env. Flow: profile -> model agent -> rule builder -> dry-run on the
clean sample -> rater. Prints the rules with their dry-run result and rating.
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.model_agent import build_data_model  # noqa: E402
from app.agents.rule_builder import build_rules, sample_rows_from_df  # noqa: E402
from app.agents.rule_rater import rate_rules, rating_warnings  # noqa: E402
from app.config import settings  # noqa: E402
from app.data_io import load_csv  # noqa: E402
from app.llm import GeminiLLM, LLM, LLMError  # noqa: E402
from app.profiling import profile_tables  # noqa: E402
from app.rule_engine import run_rule  # noqa: E402

TARGET = "sample_orders"


def run(llm: LLM, data_dir: Path) -> dict:
    sample = load_csv(data_dir / "sample_orders.csv")
    regions = load_csv(data_dir / "valid_regions.csv")
    profile = profile_tables({TARGET: sample, "valid_regions": regions})

    model = build_data_model(llm, profile)
    rules = build_rules(llm, model, sample_rows_from_df(sample), TARGET)
    dry_runs = {r.rule_id: run_rule(r, sample, {"valid_regions": regions}, "order_id") for r in rules}
    ratings = rate_rules(llm, rules, dry_runs, model)
    return {"profile": profile, "model": model, "rules": rules, "dry_runs": dry_runs,
            "ratings": ratings, "warnings": rating_warnings(ratings)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, help="write model, rules and ratings to this JSON file")
    args = ap.parse_args()

    calls: Counter = Counter()
    try:
        llm = GeminiLLM(audit=lambda agent, prompt, response: calls.update([agent]))
        print(f"Model: {llm.model}")
        out = run(llm, settings.data_dir)
    except LLMError as exc:
        sys.exit(f"LLM error: {exc}")

    print(f"LLM calls (retries included): {dict(calls)}\n")
    print(f"{'id':<4}{'type':<22}{'name':<32}{'sample fails':>12}{'score':>7}")
    ratings = {r.rule_id: r for r in out["ratings"]}
    for r in out["rules"]:
        d, rt = out["dry_runs"][r.rule_id], ratings[r.rule_id]
        fails = "ERROR" if d.status == "error" else d.fail_count
        print(f"{r.rule_id:<4}{r.type.value:<22}{r.name:<32}{fails!s:>12}{rt.score:>7}")
        print(f"      {rt.justification}")
    for w in out["warnings"]:
        print(f"\nWARNING: {w}")

    if args.out:
        payload = {
            "model": out["model"].model_dump(mode="json"),
            "rules": [r.model_dump(mode="json") for r in out["rules"]],
            "ratings": [r.model_dump(mode="json") for r in out["ratings"]],
        }
        args.out.write_text(json.dumps(payload, indent=2))
        print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
