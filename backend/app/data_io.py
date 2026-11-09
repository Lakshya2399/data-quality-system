"""Small shared helpers for loading CSVs and treating blanks as nulls."""
from __future__ import annotations

import pandas as pd


def load_csv(source) -> pd.DataFrame:
    """Read a CSV from a path or file-like object (e.g. a FastAPI upload). Column names are stripped."""
    df = pd.read_csv(source)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def null_mask(s: pd.Series) -> pd.Series:
    """True where a value is missing: NaN/None, or an empty/whitespace-only string."""
    mask = s.isna()
    if pd.api.types.is_object_dtype(s) or pd.api.types.is_string_dtype(s):
        mask = mask | s.astype(str).str.strip().eq("")
    return mask.astype(bool)
