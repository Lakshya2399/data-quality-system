"""Smoke test for the Gemini connection. Run from the repo root:
    python backend/scripts/check_gemini.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

from app.config import settings  # noqa: E402

if not settings.gemini_api_key:
    sys.exit("GEMINI_API_KEY is not set. Copy .env.example to .env and add your key.")

client = genai.Client(api_key=settings.gemini_api_key)

# 1) plain call
resp = client.models.generate_content(model=settings.gemini_model, contents="Reply with the single word: ok")
print("plain call  ->", resp.text.strip())

# 2) JSON-mode call (this is how the agents will return structured output later)
resp = client.models.generate_content(
    model=settings.gemini_model,
    contents='Return {"status": "ok"} as JSON.',
    config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0),
)
print("json call   ->", resp.text.strip())
