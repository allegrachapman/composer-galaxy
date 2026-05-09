"""Lookup Grove/Oxford article text for a composer from local cache."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional
from unicodedata import normalize

CACHE = Path(__file__).resolve().parent.parent / "data" / "grove_articles.json"

_cache: Optional[dict[str, str]] = None


def _load() -> dict[str, str]:
    global _cache
    if _cache is None:
        if CACHE.exists():
            _cache = json.loads(CACHE.read_text(encoding="utf-8"))
        else:
            _cache = {}
    return _cache


def _normalise(name: str) -> str:
    """Lowercase, strip accents, collapse whitespace."""
    s = normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z ]", "", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _flip_last_first(name: str) -> str:
    """Convert 'Last, First' to 'First Last'."""
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        return f"{parts[1]} {parts[0]}"
    return name


def lookup(composer: str) -> str | None:
    """Return Grove article text for *composer*, or None."""
    cache = _load()
    if not cache:
        return None

    target = _normalise(composer)
    for key, text in cache.items():
        if _normalise(key) == target or _normalise(_flip_last_first(key)) == target:
            return text

    # Fuzzy: check if target words are a subset of any key's words
    target_words = set(target.split())
    for key, text in cache.items():
        key_words = set(_normalise(key).split()) | set(_normalise(_flip_last_first(key)).split())
        if target_words and target_words <= key_words:
            return text

    return None
