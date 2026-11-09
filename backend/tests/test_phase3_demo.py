"""Runs the demo's run() end to end over the scripted fake client: 3 agents, 3 LLM calls."""
import json

import phase3_demo
from fakes import make_llm
from fixtures import enrichment, ratings_json, rules_json


def test_demo_pipeline_end_to_end(data_dir, profile):
    llm = make_llm([json.dumps(enrichment(profile)), rules_json(), ratings_json([5, 5, 5, 5, 5, 4, 3, 3])])
    out = phase3_demo.run(llm, data_dir)
    assert len(llm.calls) == 3
    assert [r.rule_id for r in out["rules"]] == [f"R{i}" for i in range(1, 9)]
    assert all(d.status == "ok" and d.fail_count == 0 for d in out["dry_runs"].values())
    assert [r.score for r in out["ratings"]] == [5, 5, 5, 5, 5, 4, 3, 3]
    assert out["warnings"] == []
    # the rater saw the dry-run results produced by the engine
    assert '"failed": 0' in llm.calls[2].prompt


def test_demo_surfaces_a_bad_rule_through_the_caps(data_dir, profile):
    drafts = json.loads(rules_json())["rules"]
    drafts[2]["min"] = 500.0                      # "order_amount >= 500" - wrong: fails on clean sample rows
    llm = make_llm([json.dumps(enrichment(profile)), json.dumps({"rules": drafts}), ratings_json([5] * 8)])
    out = phase3_demo.run(llm, data_dir)
    assert out["dry_runs"]["R3"].fail_count > 0
    assert out["ratings"][2].score == 2 and out["ratings"][2].llm_score == 5
