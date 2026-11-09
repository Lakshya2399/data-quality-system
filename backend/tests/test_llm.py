import pytest
from pydantic import BaseModel

from app.llm import GeminiLLM, LLMError, LLMOutputError, extract_json
from app.prompts import render
from fakes import TransientError, make_llm


class Out(BaseModel):
    answer: int


# ---- extract_json -----------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    ['{"a": 1}', '```json\n{"a": 1}\n```', '```\n{"a": 1}\n```', 'Sure! Here you go:\n{"a": 1}\nHope that helps.'],
)
def test_extract_json_tolerant(text):
    assert extract_json(text) == {"a": 1}


@pytest.mark.parametrize("text", ["", "   ", "no json here", "{broken"])
def test_extract_json_rejects(text):
    with pytest.raises(ValueError):
        extract_json(text)


# ---- generate_json ----------------------------------------------------------
def test_success_first_try_sends_json_mode_config_and_audits():
    llm = make_llm(['{"answer": 1}'])
    audit = []
    llm.audit = lambda agent, prompt, response: audit.append((agent, prompt, response))
    assert llm.generate_json("P", Out, agent="demo").answer == 1
    cfg = llm.calls[0].config
    assert cfg.response_mime_type == "application/json" and cfg.temperature == 0.0
    assert "JSON" in cfg.system_instruction
    assert audit == [("demo", "P", '{"answer": 1}')]


def test_fenced_json_needs_no_retry():
    llm = make_llm(['```json\n{"answer": 3}\n```'])
    assert llm.generate_json("P", Out).answer == 3 and len(llm.calls) == 1


def test_retries_invalid_json_with_feedback():
    llm = make_llm(["not json at all", '{"answer": 5}'])
    assert llm.generate_json("P", Out).answer == 5
    retry = llm.calls[1].prompt
    assert retry.startswith("P") and "REJECTED" in retry and "not valid JSON" in retry and "not json at all" in retry


def test_retries_schema_violation_and_names_the_field():
    llm = make_llm(['{"answer": "x"}', '{"answer": 1}'])
    llm.generate_json("P", Out)
    assert "answer" in llm.calls[1].prompt and "REJECTED" in llm.calls[1].prompt


def test_empty_response_is_retried():
    llm = make_llm(["", '{"answer": 1}'])
    assert llm.generate_json("P", Out).answer == 1
    assert "empty response" in llm.calls[1].prompt


def test_validator_problems_are_fed_back():
    llm = make_llm(['{"answer": 1}', '{"answer": 2}'])
    got = llm.generate_json("P", Out, validator=lambda o: [] if o.answer == 2 else ["answer must be 2"])
    assert got.answer == 2 and "answer must be 2" in llm.calls[1].prompt


def test_exhausted_attempts_raise_with_problems_and_last_output():
    llm = make_llm(['{"answer": 1}'] * 3)
    with pytest.raises(LLMOutputError) as ei:
        llm.generate_json("P", Out, validator=lambda o: ["still wrong"], agent="demo")
    assert ei.value.problems == ["still wrong"] and ei.value.last_output == '{"answer": 1}'
    assert ei.value.agent == "demo" and len(llm.calls) == 3


def test_max_attempts_configurable():
    llm = make_llm(["bad", "bad"], max_attempts=2)
    with pytest.raises(LLMOutputError):
        llm.generate_json("P", Out)
    assert len(llm.calls) == 2


# ---- transport errors -------------------------------------------------------
def test_transient_errors_retried_with_exponential_backoff():
    llm = make_llm([TransientError(503), TransientError(429), '{"answer": 1}'])
    assert llm.generate_json("P", Out).answer == 1
    assert len(llm.calls) == 3 and llm.sleeps == [1.0, 2.0]


def test_non_transient_error_fails_immediately():
    llm = make_llm([TransientError(400)])
    with pytest.raises(LLMError, match="Gemini call failed"):
        llm.generate_json("P", Out)
    assert len(llm.calls) == 1 and llm.sleeps == []


def test_transient_errors_eventually_give_up():
    llm = make_llm([TransientError(503)] * 3, transport_retries=2)
    with pytest.raises(LLMError):
        llm.generate_json("P", Out)
    assert len(llm.calls) == 3 and llm.sleeps == [1.0, 2.0]


def test_missing_api_key_is_a_clear_error():
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        GeminiLLM(api_key="")


# ---- prompt rendering -------------------------------------------------------
def test_render_requires_exact_variables():
    with pytest.raises(KeyError):
        render("rule_rater", model_summary="x")                       # 'rules' missing
    with pytest.raises(KeyError):
        render("rule_rater", model_summary="x", rules="y", extra="z")  # unexpected variable


def test_render_does_not_reinterpret_data():
    out = render("rule_rater", model_summary="{{rules}}", rules="R")
    assert "{{rules}}" in out
