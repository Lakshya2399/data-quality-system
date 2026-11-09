"""Central configuration. Environment is read here only (the API key is used only in llm.py)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = BACKEND_DIR.parent
load_dotenv(ROOT_DIR / ".env")


def _path(env_name: str, default: Path) -> Path:
    raw = os.getenv(env_name)
    if not raw:
        return default
    p = Path(raw)
    return p if p.is_absolute() else ROOT_DIR / p


@dataclass(frozen=True)
class Settings:
    # LLM
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    llm_temperature: float = 0.0

    # Paths
    data_dir: Path = _path("DATA_DIR", BACKEND_DIR / "data")
    db_path: Path = _path("DB_PATH", BACKEND_DIR / "data" / "dq.sqlite")

    # Pipeline behaviour
    rating_threshold: int = 4        # only rules rated >= this are executed on prod data
    max_rule_retries: int = 2        # rule_builder retry loop
    max_failed_ids: int = 100        # cap on failed row ids stored per rule

    # API
    max_upload_mb: float = float(os.getenv("MAX_UPLOAD_MB", "10"))


settings = Settings()
