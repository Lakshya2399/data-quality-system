"""End-to-end CLI: sample pipeline (Graph A, live Gemini) then rule execution on the prod file (Graph B).

    python backend/scripts/run_pipeline.py
    python backend/scripts/run_pipeline.py --skip-execute
    python backend/scripts/run_pipeline.py --db backend/data/dq.sqlite --threshold 4

Everything is persisted in SQLite (default backend/data/dq.sqlite): runs, data models, rules,
ratings, executions, execution results and the LLM audit trail.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db, services  # noqa: E402
from app.config import settings  # noqa: E402
from app.data_io import load_csv  # noqa: E402
from app.llm import GeminiLLM, LLMError  # noqa: E402

MAX_IDS_SHOWN = 5


def print_run(run: dict, db_path) -> None:
    with db.get_db(db_path) as conn:
        rules = db.list_rules(conn, run["run_id"])
        calls = conn.execute(
            "SELECT agent, COUNT(*) AS n FROM llm_calls WHERE run_id = ? GROUP BY agent", (run["run_id"],)
        ).fetchall()
    print(f"Run {run['run_id']}: {run['status']}   LLM calls: {({c['agent']: c['n'] for c in calls})}")
    if run["error"]:
        print(f"  ERROR: {run['error']}")
    for w in run["warnings"]:
        print(f"  WARNING: {w}")
    if not rules:
        return
    print(f"\n{'id':<4}{'score':>5}  {'type':<22}{'name':<32}{'sample fails':>12}")
    for r in rules:
        print(f"{r['rule_key']:<4}{r['score']!s:>5}  {r['type']:<22}{r['name']:<32}{r['sample_fail_count']!s:>12}")
        print(f"          {r['justification']}")


def print_execution(ex: dict) -> None:
    print(f"\nExecution #{ex['exec_id']} on {ex['target_file']}: {ex['status']}, "
          f"{ex['total_rows']} rows, {len(ex['results'])} rules executed")
    if ex["error"]:
        print(f"  ERROR: {ex['error']}")
    for r in ex["results"]:
        if r["status"] == "error":
            print(f"  [{r['rule_key']}] {r['rule_name']}: COULD NOT RUN - {r['error']}")
            continue
        print(f"  [{r['rule_key']}] {r['rule_name']} (score {r['score']}): "
              f"{r['fail_count']} failed / {r['total_rows']} rows")
        if r["fail_count"]:
            ids = r["failed_row_ids"][:MAX_IDS_SHOWN]
            more = f" (+{r['fail_count'] - len(ids)} more)" if r["fail_count"] > len(ids) else ""
            print(f"        failed ids: {', '.join(ids)}{more}")
            print(f"        e.g. {r['sample_reasons'][0]['reason']}")


def main(argv=None, llm=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=settings.data_dir)
    ap.add_argument("--sample", default="sample_orders.csv")
    ap.add_argument("--lookup", default="valid_regions.csv")
    ap.add_argument("--prod", default="prod_orders.csv")
    ap.add_argument("--db", type=Path, default=None, help="SQLite file (default: DB_PATH from .env)")
    ap.add_argument("--workdir", type=Path, default=None, help="where run inputs are stored (default: data dir)")
    ap.add_argument("--threshold", type=int, default=None, help="minimum rating to execute (default 4)")
    ap.add_argument("--skip-execute", action="store_true", help="stop after the sample pipeline")
    args = ap.parse_args(argv)

    try:
        llm = llm or GeminiLLM()
        sample = load_csv(args.data_dir / args.sample)
        lookup = load_csv(args.data_dir / args.lookup)
    except (LLMError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}")
        return 1

    paths = {"db_path": args.db, "workdir": args.workdir}
    run = services.run_sample_pipeline(sample, args.sample, {args.lookup: lookup}, llm,
                                       rating_threshold=args.threshold, **paths)
    print_run(run, args.db)
    if run["status"] != "completed":
        return 1
    if args.skip_execute:
        return 0

    try:
        prod = load_csv(args.data_dir / args.prod)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        return 1
    ex = services.run_execution(run["run_id"], prod, args.prod, rating_threshold=args.threshold, **paths)
    print_execution(ex)
    return 0 if ex["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
