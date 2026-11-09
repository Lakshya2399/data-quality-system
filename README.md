# Multi-Agent Data Quality System

LangGraph + Gemini + SQLite + FastAPI + Node.js UI. See the build plan for the full design.

## Quick start

**macOS / Linux (bash)**
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                       # add your GEMINI_API_KEY
```

**Windows (PowerShell)**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1               # if blocked: Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
pip install -r requirements.txt
Copy-Item .env.example .env                # add your GEMINI_API_KEY
```

Then (use `/` or `\` paths as suits your shell):
```
python backend/scripts/generate_data.py    # CSVs + ground_truth.json -> backend/data
python backend/scripts/phase2_demo.py      # profile + run reference rules, compare to ground truth
python backend/scripts/check_gemini.py     # verify the Gemini key
python backend/scripts/phase3_demo.py      # run the 3 agents live on the sample files
python backend/scripts/run_pipeline.py     # FULL flow: pipeline -> SQLite -> execute on prod, live Gemini
cd backend
pytest -q                                  # 197 tests, no network needed
uvicorn app.main:app                       # http://localhost:8000/docs  (interactive API docs)
```

## Status
- [x] Phase 0 - setup
- [x] Phase 1 - synthetic data + ground truth
- [x] Phase 2 - profiling, rule engine, SQLite schema (no LLM; verified against ground truth)
- [x] Phase 3 - Gemini client, 3 agents, prompts (tested with a scripted fake client)
- [x] Phase 4 - LangGraph pipeline + execution graphs, persistence, services, end-to-end CLI
- [x] Phase 5 - FastAPI endpoints (background jobs, uploads, restart recovery)
- [x] Phase 6 - Node.js UI (Express proxy + vanilla JS page, 29 frontend tests)

## API

Start the server from `backend/` (single process; see note below) and open http://localhost:8000/docs.

| Method | Path | Purpose |
|---|---|---|
| POST | `/pipeline/run` | Upload `sample_file` + `lookup_file` (or neither, to use the defaults in `backend/data`). Returns **202** with a `run_id`; the pipeline runs in the background. `?wait=true` blocks and returns 200. |
| GET | `/runs`, `/runs/{id}` | List runs / poll one run (`created` -> `running` -> `completed` or `failed`, plus `error` and `warnings`) |
| GET | `/runs/{id}/model` | Inferred data model (`?include_profile=true` adds the raw profile) |
| GET | `/runs/{id}/rules` | Rules with ratings, justifications and dry-run stats; `?min_score=` marks which will execute |
| POST | `/runs/{id}/execute` | Optional `target_file` upload (default `prod_orders.csv`), `?min_score=` (default 4), `?wait=true`. Returns **202** with an `exec_id`. |
| GET | `/runs/{id}/executions`, `/executions/{exec_id}` | Execution history / per-rule pass-fail counts, failed row ids and reasons |

Pipeline and execution failures are reported as data (`status: "failed"` with `error`), not as HTTP errors.
Status codes: 400 bad file, 404 unknown id, 409 run not completed, 413 file too large, 422 bad parameters, 503 LLM not configured.

**Bash**
```bash
curl -X POST localhost:8000/pipeline/run                         # -> {"run_id": "...", "poll_url": "/runs/..."}
curl localhost:8000/runs/RUN_ID
curl localhost:8000/runs/RUN_ID/rules
curl -X POST "localhost:8000/runs/RUN_ID/execute?min_score=4"    # -> {"exec_id": 1, ...}
curl localhost:8000/executions/1
curl -X POST localhost:8000/pipeline/run -F sample_file=@backend/data/sample_orders.csv -F lookup_file=@backend/data/valid_regions.csv
```
**Windows PowerShell** (use `curl.exe`, not the `curl` alias)
```powershell
curl.exe -X POST http://localhost:8000/pipeline/run
curl.exe http://localhost:8000/runs/RUN_ID/rules
curl.exe -X POST "http://localhost:8000/runs/RUN_ID/execute?min_score=4"
curl.exe -X POST http://localhost:8000/pipeline/run -F "sample_file=@backend/data/sample_orders.csv" -F "lookup_file=@backend/data/valid_regions.csv"
```

Notes: jobs run inside the server process, so run **one** server process and avoid `--reload` while a run is in
progress (a restart kills running jobs; they are marked `failed` with an "Interrupted" error on the next start).
The default upload limit is 10 MB (`MAX_UPLOAD_MB` in `.env`).

## UI (Node.js)

Needs Node 20+. Two terminals:

```
# terminal 1 - the API (from backend/)
uvicorn app.main:app

# terminal 2 - the UI (from frontend/)
cd frontend
npm install
npm start                      # http://localhost:3000
```

**Windows PowerShell:** the same commands work; to point the UI at a different API use
`$env:API_URL = "http://127.0.0.1:8000"; npm start`.

The page has four panels: run controls (default or uploaded files, minimum score, run history), the inferred
data model, the rated rules (rules that will execute are highlighted; warnings are shown, never hidden), and the
validation report (pass/fail bars, failed row IDs, reasons). The selected run and execution are kept in the URL
hash, so a refresh or a shared link returns to the same place.

| Env var | Default | Meaning |
|---|---|---|
| `API_URL` | `http://127.0.0.1:8000` | where the FastAPI backend runs |
| `PORT` | `3000` | UI port |
| `HOST` | `127.0.0.1` | interface to bind. The API has **no authentication** and every run spends Gemini credits, so keep this on localhost unless you add auth in front. |

How it works: `frontend/server.js` serves `frontend/public/` and proxies `/api/*` to FastAPI (streaming, so uploads are
never buffered; backend outages become a JSON 502/504 the page can display). Rule descriptions come from an LLM and failed
values from your CSVs, so the page never uses `innerHTML`, and a strict Content-Security-Policy is sent.

**Frontend tests** (`cd frontend && npm test`): the proxy against a fake backend, and the real `index.html` + `app.js` in
jsdom against payloads captured from the real API (`frontend/test/fixtures`). If the API response shape changes, the Python
test `tests/test_ui_contract.py` fails; run `python backend/scripts/export_ui_fixtures.py` to refresh the fixtures.
