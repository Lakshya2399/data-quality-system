"""The frontend is tested against fixtures captured from the real API. If this fails, the API's
response shape changed: run `python backend/scripts/export_ui_fixtures.py` and update the UI."""
import json

import pytest

import export_ui_fixtures as ex


@pytest.fixture(scope="module")
def fresh(tmp_path_factory):
    return ex.build_payloads(tmp_path_factory.mktemp("ui_contract"))


def test_committed_fixtures_match_the_live_api_shape(fresh):
    assert ex.FIXTURE_DIR.exists(), "run scripts/export_ui_fixtures.py"
    for name, payload in fresh.items():
        committed = json.loads((ex.FIXTURE_DIR / f"{name}.json").read_text())
        assert ex.shape(committed) == ex.shape(payload), f"{name}.json is stale: re-run export_ui_fixtures.py"


def test_fixtures_cover_the_ui_states(fresh):
    assert fresh["run"]["status"] == "completed" and fresh["run_failed"]["status"] == "failed"
    assert fresh["execution"]["status"] == "completed" and fresh["execution_failed"]["status"] == "failed"
    assert fresh["rules_low_scores"]["warnings"], "the low-score run must carry warnings"
