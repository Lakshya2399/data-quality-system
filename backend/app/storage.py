"""Per-run file storage. Inputs are copied under  <workdir>/runs/<run_id>/  so a run can be
executed later (even after a restart) without the original uploads:

    runs/<run_id>/<sample>.csv            the clean sample
    runs/<run_id>/<lookup>.csv            each lookup table
    runs/<run_id>/executions/<exec_id>/<target>.csv   the production file of one execution
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from .config import settings
from .data_io import load_csv

_RUN_ID = re.compile(r"[0-9a-f]{12}")


def table_name(filename: str) -> str:
    """'data/valid_regions.csv' -> 'valid_regions'. Only [A-Za-z0-9_.-] survive (no path tricks)."""
    stem = Path(str(filename).replace("\\", "/")).name
    if stem.lower().endswith(".csv"):
        stem = stem[:-4]
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", stem).strip(".")
    if not stem:
        raise ValueError(f"cannot derive a table name from {filename!r}")
    return stem


def run_dir(run_id: str, workdir: Path | str | None = None) -> Path:
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError(f"invalid run id {run_id!r}")
    return Path(workdir or settings.data_dir) / "runs" / run_id


def save_table(directory: Path, name: str, df: pd.DataFrame) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{table_name(name)}.csv"
    df.to_csv(path, index=False)
    return path


def load_table(directory: Path, name: str) -> pd.DataFrame:
    path = directory / f"{table_name(name)}.csv"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found")
    return load_csv(path)
