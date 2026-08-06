"""Prompts live in files, not in Python strings, so they can be edited and
diffed without touching code."""

from functools import lru_cache
from pathlib import Path

DIR = Path(__file__).parent


@lru_cache(maxsize=8)
def load(name: str) -> str:
    path = DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"No prompt named {name} at {path}")
    return path.read_text(encoding="utf-8")
