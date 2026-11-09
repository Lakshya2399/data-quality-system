"""Prompt templates live next to this file as <name>.md and use {{placeholder}} variables."""
from __future__ import annotations

import re
from pathlib import Path

_DIR = Path(__file__).parent
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def render(name: str, **values: str) -> str:
    template = (_DIR / f"{name}.md").read_text(encoding="utf-8")
    needed = set(_PLACEHOLDER.findall(template))
    if needed != set(values):
        raise KeyError(f"prompt '{name}': missing {sorted(needed - set(values))}, unexpected {sorted(set(values) - needed)}")
    # single pass, so data that happens to contain '{{x}}' is never re-interpreted
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], template)
