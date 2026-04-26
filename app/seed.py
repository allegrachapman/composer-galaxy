"""Seed script: sample composers, fetch Wikipedia, extract relationships, write markdown.

Usage:
    python -m app.seed                  # default: 10 composers, top-200 pool
    python -m app.seed --count 20
    python -m app.seed --names "Bach,Beethoven"   # skip sampling, use explicit names
"""

from __future__ import annotations

import argparse
import asyncio
import random
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

from . import extract, wikipedia  # noqa: E402
from .composers_pool import CANDIDATE_COMPOSERS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
COMPOSERS_DIR = ROOT / "composers"


def _slug(name: str) -> str:
    s = name.lower()
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


_SOURCE_PRIORITY = {"infobox": 0, "wiki": 1, "llm_confirmed": 2, "llm": 3}

_DISAMBIG_RE = re.compile(r"\s*\((?:composer|musician|pianist|organist|violinist|singer|conductor|cellist|flautist|musicologist)\)$", re.IGNORECASE)


def _normalize_name(name: str) -> str:
    return _DISAMBIG_RE.sub("", name).strip()


def _merge_edges(
    *source_lists: tuple[list, str],
) -> list[dict[str, str]]:
    """Merge edge lists from multiple sources. Lower priority number wins.

    Each source list can contain plain strings or {"name": ..., "quote": ...} dicts.
    Names with Wikipedia disambiguation suffixes like "(composer)" are matched
    against their bare form; the higher-priority version's name is kept.
    """
    seen: dict[str, dict] = {}
    for vals, tag in source_lists:
        for v in vals:
            if isinstance(v, dict):
                name = v.get("name", "").strip()
                quote = v.get("quote", "").strip()
            else:
                name = str(v).strip()
                quote = ""
            if not name:
                continue
            key = _normalize_name(name)
            if key not in seen or _SOURCE_PRIORITY[tag] < _SOURCE_PRIORITY[seen[key]["source"]]:
                entry = {"name": name, "source": tag}
                if quote:
                    entry["quote"] = quote
                seen[key] = entry
    return list(seen.values())


def _pick_scalar(infobox_val, llm_val) -> tuple[str | None, str | None]:
    """Return (value, source) preferring infobox."""
    if infobox_val:
        return infobox_val, "infobox"
    if llm_val:
        return llm_val, "llm"
    return None, None


async def process_composer(title: str) -> dict:
    print(f"  → fetching {title}...")
    article = await wikipedia.fetch_article(title)
    canonical = article["title"]

    print(f"    parsing infobox...")
    ibox = wikipedia.parse_infobox(article["wikitext"])

    print(f"    scanning article prose...")
    prose = wikipedia.parse_prose_relationships(article["wikitext"])

    print(f"    LLM extraction...")
    llm = await asyncio.to_thread(
        extract.extract_relationships, canonical, article["extract"]
    )

    birth, birth_src = _pick_scalar(ibox["birth"], llm["birth"])
    death, death_src = _pick_scalar(ibox["death"], llm["death"])
    nat, nat_src = _pick_scalar(ibox["nationality"], llm["nationality"])
    era, era_src = _pick_scalar(ibox["era"], llm["era"])

    return {
        "name": canonical,
        "wikipedia": f"https://en.wikipedia.org/wiki/{canonical.replace(' ', '_')}",
        "thumbnail": article.get("thumbnail"),
        "birth": birth,
        "birth_source": birth_src,
        "death": death,
        "death_source": death_src,
        "nationality": nat,
        "nationality_source": nat_src,
        "era": era,
        "era_source": era_src,
        "teachers": _merge_edges(
            (ibox["teachers"], "infobox"), (prose["teachers"], "wiki"), (llm["teachers"], "llm"),
        ),
        "students": _merge_edges(
            (ibox["students"], "infobox"), (prose["students"], "wiki"), (llm["students"], "llm"),
        ),
        "influenced_by": _merge_edges(
            (ibox["influenced_by"], "infobox"), (prose["influenced_by"], "wiki"), (llm["influenced_by"], "llm"),
        ),
        "influenced": _merge_edges(
            ([], "infobox"), ([], "wiki"), (llm["influenced"], "llm"),
        ),
    }


def write_markdown(record: dict) -> Path:
    COMPOSERS_DIR.mkdir(exist_ok=True)
    path = COMPOSERS_DIR / f"{_slug(record['name'])}.md"
    body = [
        f"# {record['name']}",
        "",
        f"[Wikipedia]({record['wikipedia']})",
        "",
        "## Notes",
        "",
        "_Add your own notes here. This section is preserved across re-seeds._",
        "",
    ]

    # Preserve existing Notes section if the file exists
    if path.exists():
        existing = path.read_text()
        m = re.search(r"## Notes\s*\n(.*)", existing, re.DOTALL)
        if m:
            body = [
                f"# {record['name']}",
                "",
                f"[Wikipedia]({record['wikipedia']})",
                "",
                "## Notes",
                "",
                m.group(1).strip(),
                "",
            ]

    fm = yaml.safe_dump(record, sort_keys=False, allow_unicode=True)
    path.write_text(f"---\n{fm}---\n\n" + "\n".join(body))
    return path


async def main(count: int, names: list[str] | None):
    if names:
        chosen = names
        print(f"Using {len(chosen)} explicit composers.")
    else:
        print(f"Ranking {len(CANDIDATE_COMPOSERS)} candidates by pageviews...")
        top = await wikipedia.top_by_pageviews(CANDIDATE_COMPOSERS, limit=200)
        pool = [t for t, _ in top]
        print(f"Top-{len(pool)} pool built. Sampling {count}.")
        chosen = random.sample(pool, min(count, len(pool)))

    print(f"\nChosen:")
    for c in chosen:
        print(f"  - {c}")
    print()

    if not names:
        print("Pausing before fetching (rate-limit cooldown)...")
        await asyncio.sleep(10)

    for i, c in enumerate(chosen):
        try:
            record = await process_composer(c)
            path = write_markdown(record)
            print(f"  ✓ wrote {path.name}")
        except Exception as e:
            print(f"  ✗ {c}: {e!r}")
        if i < len(chosen) - 1:
            await asyncio.sleep(1.5)

    print(f"\nDone. {len(list(COMPOSERS_DIR.glob('*.md')))} composer files in {COMPOSERS_DIR}/")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--names", type=str, default=None,
                    help="Comma-separated explicit composer titles (skip sampling)")
    args = ap.parse_args()
    names = [n.strip() for n in args.names.split(",")] if args.names else None
    asyncio.run(main(args.count, names))
