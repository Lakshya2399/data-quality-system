import json

import run_pipeline
from app.llm import LLMError
from fixtures import enrichment, ratings_json, rules_json
from fakes import make_llm
from pipeline_helpers import GARBAGE, SCORES


def args(data_dir, env, *extra):
    return ["--data-dir", str(data_dir), "--db", str(env.db), "--workdir", str(env.work), *extra]


def test_cli_end_to_end(data_dir, env, profile, capsys):
    llm = make_llm([json.dumps(enrichment(profile)), rules_json(), ratings_json(SCORES)])
    assert run_pipeline.main(args(data_dir, env), llm=llm) == 0
    out = capsys.readouterr().out
    assert "completed" in out and "'model_agent': 1" in out
    assert "Execution #1 on prod_orders.csv: completed, 400 rows, 6 rules executed" in out
    assert "discount_not_above_amount (score 5): 22 failed / 400 rows" in out
    assert "failed ids: ORD-" in out and "(+" in out
    assert out.isascii()                                   # safe for Windows consoles


def test_cli_skip_execute(data_dir, env, profile, capsys):
    llm = make_llm([json.dumps(enrichment(profile)), rules_json(), ratings_json(SCORES)])
    assert run_pipeline.main(args(data_dir, env, "--skip-execute"), llm=llm) == 0
    assert "Execution" not in capsys.readouterr().out


def test_cli_reports_pipeline_failure_with_exit_code_1(data_dir, env, capsys):
    assert run_pipeline.main(args(data_dir, env), llm=make_llm(GARBAGE)) == 1
    out = capsys.readouterr().out
    assert "failed" in out and "ERROR: LLMOutputError: model_agent" in out and "Execution" not in out


def test_cli_missing_input_file(env, tmp_path, capsys):
    assert run_pipeline.main(["--data-dir", str(tmp_path), "--db", str(env.db)], llm=make_llm([])) == 1
    assert "sample_orders.csv" in capsys.readouterr().out


def test_cli_missing_api_key(data_dir, env, capsys, monkeypatch):
    def no_key(*a, **k):
        raise LLMError("GEMINI_API_KEY is not set")

    monkeypatch.setattr(run_pipeline, "GeminiLLM", no_key)
    assert run_pipeline.main(args(data_dir, env)) == 1
    assert "GEMINI_API_KEY" in capsys.readouterr().out
