"""Export REAL API payloads as fixtures for the frontend tests (no Gemini needed: a scripted fake LLM is used).

    python backend/scripts/export_ui_fixtures.py

Writes frontend/test/fixtures/*.json. tests/test_ui_contract.py fails when the API's response shape
drifts from these files, which is the signal to re-run this script and update the UI.
"""
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND), str(BACKEND / "tests"), str(BACKEND / "scripts")]

import generate_data as gd  # noqa: E402
from api_helpers import make_client  # noqa: E402
from app.data_io import load_csv  # noqa: E402
from app.profiling import profile_tables  # noqa: E402
from pipeline_helpers import GARBAGE, happy_script  # noqa: E402

FIXTURE_DIR = BACKEND.parent / "frontend" / "test" / "fixtures"


def build_payloads(workdir: Path) -> dict:
    data = workdir / "data"
    gd.generate(data, seed=42)
    profile = profile_tables({"sample_orders": load_csv(data / "sample_orders.csv"),
                              "valid_regions": load_csv(data / "valid_regions.csv")})

    def client(name, *scripts):
        return make_client(SimpleNamespace(db=workdir / f"{name}.sqlite", work=workdir / name), data, *scripts)

    good = client("good", happy_script(profile))
    run_id = good.post("/pipeline/run").json()["run_id"]
    exec_id = good.post(f"/runs/{run_id}/execute").json()["exec_id"]

    low = client("low", happy_script(profile, [3] * 8))                  # nothing rated >= 4
    low_id = low.post("/pipeline/run").json()["run_id"]
    low_exec = low.post(f"/runs/{low_id}/execute").json()["exec_id"]

    bad = client("bad", GARBAGE)
    bad_id = bad.post("/pipeline/run").json()["run_id"]

    return {
        "health": good.get("/health").json(),
        "runs": good.get("/runs").json(),
        "run": good.get(f"/runs/{run_id}").json(),
        "model": good.get(f"/runs/{run_id}/model").json(),
        "rules": good.get(f"/runs/{run_id}/rules").json(),
        "executions": good.get(f"/runs/{run_id}/executions").json(),
        "execution": good.get(f"/executions/{exec_id}").json(),
        "run_low_scores": low.get(f"/runs/{low_id}").json(),
        "rules_low_scores": low.get(f"/runs/{low_id}/rules").json(),
        "execution_failed": low.get(f"/executions/{low_exec}").json(),
        "run_failed": bad.get(f"/runs/{bad_id}").json(),
    }


def shape(obj):
    """Key structure only (values and null-vs-value are ignored): a drift alarm, not a schema."""
    if isinstance(obj, dict):
        return {k: shape(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [shape(obj[0])] if obj else []
    return "value"


def main() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        payloads = build_payloads(Path(tmp))
    for name, payload in payloads.items():
        (FIXTURE_DIR / f"{name}.json").write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {len(payloads)} fixtures to {FIXTURE_DIR}")


if __name__ == "__main__":
    main()
