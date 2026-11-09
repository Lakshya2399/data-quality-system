import json

from app import db, services
from app.data_io import load_csv
from fakes import make_llm
from fixtures import enrichment, ratings_json, rules_json

SCORES = [5, 5, 5, 5, 5, 4, 3, 3]        # R1-R6 are rated >= 4; R7, R8 are not
# fixtures.good_drafts() rule names -> the ground_truth.json key for the same check
GT_KEY = {
    "order_id_unique": "unique_order_id_keep_first",
    "order_amount_not_null": "not_null_order_amount",
    "order_amount_non_negative": "order_amount_min_0",
    "discount_not_above_amount": "discount_le_order_amount",
    "region_code_valid": "region_code_in_valid_regions",
    "created_at_iso": "created_at_iso_format",
    "customer_email_format": "customer_email_format",          # rated 3 in happy_script
    "customer_email_not_null": "not_null_customer_email",      # rated 3 in happy_script
}
GARBAGE = ["not json"] * 3               # one agent invocation that never produces valid output


def happy_script(profile, scores=SCORES):
    return [json.dumps(enrichment(profile)), rules_json(), ratings_json(scores)]


def run_pipeline(env, data_dir, script, **kwargs):
    llm = make_llm(script)
    run = services.run_sample_pipeline(
        load_csv(data_dir / "sample_orders.csv"), "sample_orders.csv",
        {"valid_regions.csv": load_csv(data_dir / "valid_regions.csv")}, llm,
        db_path=env.db, workdir=env.work, **kwargs)
    return run, llm


def rows(env, sql, *args):
    with db.get_db(env.db) as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
