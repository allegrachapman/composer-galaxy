"""Backfill birth/death dates for stub composers from Wikipedia.

No LLM calls — just quick Wikipedia API lookups (infobox + extract fallback).

Usage:
    python -m app.backfill_dates
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_EXTRACT_DATE_RE = re.compile(
    r"(?:\d{1,2}\s+\w+\s+)?(\d{4})\s*[\u2013\u2014\-–—]\s*(?:\d{1,2}\s+\w+\s+)?(\d{4})"
)

from . import wikipedia  # noqa: E402
from .graph import load_graph  # noqa: E402
from .seed import _slug, write_markdown  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
COMPOSERS_DIR = ROOT / "composers"


_SKIP = {"fortification", "St. Petersburg Conservatory", "Russian Musical Society"}


def find_stubs() -> list[str]:
    """Return names of stub nodes (referenced but no .md file)."""
    graph = load_graph()
    existing = {p.stem for p in COMPOSERS_DIR.glob("*.md")}
    stubs = []
    for node in graph["nodes"]:
        d = node["data"]
        if not d["stub"]:
            continue
        name = d["id"]
        if _slug(name) in existing:
            continue
        if name in _SKIP or len(name) < 4:
            continue
        stubs.append(name)
    return stubs


async def fetch_dates(title: str) -> dict:
    """Fetch just birth/death/nationality/era from Wikipedia infobox + extract fallback."""
    article = await wikipedia.fetch_article(title)
    canonical = article["title"]
    ibox = wikipedia.parse_infobox(article["wikitext"])

    birth = ibox["birth"]
    death = ibox["death"]

    # Fallback: extract dates from the opening paragraph
    if not birth:
        m = _EXTRACT_DATE_RE.search(article["extract"][:500])
        if m:
            birth = m.group(1)
            death = death or m.group(2)

    return {
        "name": canonical,
        "wikipedia": f"https://en.wikipedia.org/wiki/{canonical.replace(' ', '_')}",
        "thumbnail": article.get("thumbnail"),
        "birth": birth,
        "death": death,
        "nationality": ibox["nationality"],
        "era": ibox["era"],
        "teachers": [],
        "students": [],
        "influenced_by": [],
        "influenced": [],
    }


async def main():
    stubs = find_stubs()
    if not stubs:
        print("No stubs to backfill.")
        return

    print(f"Found {len(stubs)} stubs to backfill.\n")

    success = 0
    for i, name in enumerate(stubs):
        print(f"  [{i+1}/{len(stubs)}] {name}...", end=" ", flush=True)
        try:
            record = await fetch_dates(name)
            if record["birth"]:
                write_markdown(record)
                print(f"b.{record['birth']}")
                success += 1
            else:
                print("no birth year found, skipping")
        except Exception as e:
            print(f"error: {e!r}")
        # Small delay to avoid rate limiting
        if (i + 1) % 10 == 0:
            await asyncio.sleep(2)

    print(f"\nDone. Backfilled {success}/{len(stubs)} stubs.")


if __name__ == "__main__":
    asyncio.run(main())
