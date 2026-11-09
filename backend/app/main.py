"""FastAPI application.   Run from the backend/ directory:   uvicorn app.main:app --reload

Run a SINGLE server process: pipeline/execution jobs run as in-process background tasks, and at
startup any job left 'running' by a previous process is marked failed.
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

from fastapi import FastAPI

from . import db
from .api import ApiConfig, router
from .config import settings
from .llm import GeminiLLM, LLM

log = logging.getLogger(__name__)
VERSION = "0.5.0"


def create_app(
    *,
    llm_factory: Callable[[], LLM] | None = None,
    db_path: Path | str | None = None,
    workdir: Path | str | None = None,
    data_dir: Path | str | None = None,
    max_upload_mb: float | None = None,
) -> FastAPI:
    """Build the app. Everything is injectable so tests can use a fake LLM and temp storage."""
    config = ApiConfig(
        llm_factory=llm_factory or GeminiLLM,              # called per request, so a missing key is a clean 503
        db_path=Path(db_path) if db_path else None,
        workdir=Path(workdir) if workdir else None,
        data_dir=Path(data_dir) if data_dir else settings.data_dir,
        max_upload_bytes=int((settings.max_upload_mb if max_upload_mb is None else max_upload_mb) * 1024 * 1024),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with config.conn() as conn:
            recovered = db.fail_interrupted(conn)
        if any(recovered.values()):
            log.warning("marked interrupted jobs as failed: %s", recovered)
        yield

    app = FastAPI(
        title="Multi-Agent Data Quality System",
        version=VERSION,
        description="Infer a data model from a clean sample, generate and rate DQ rules with Gemini, "
                    "then execute the best rules against a production file.",
        lifespan=lifespan,
    )
    app.state.config = config
    app.include_router(router)

    @app.get("/health", tags=["meta"])
    def health():
        return {
            "status": "ok",
            "version": VERSION,
            "gemini_configured": bool(settings.gemini_api_key),
            "model": settings.gemini_model,
            "db_path": str(config.db_path or settings.db_path),
        }

    return app


app = create_app()
