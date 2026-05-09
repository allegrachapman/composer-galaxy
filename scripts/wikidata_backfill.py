#!/usr/bin/env python3
"""Backfill missing Wikidata IDs by searching the Wikidata API.

Usage:
    python scripts/wikidata_backfill.py              # dry-run: show matches
    python scripts/wikidata_backfill.py --fix        # write QIDs to files
"""

import argparse
import re
import time
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSERS_DIR = ROOT / "composers"

WIKIDATA_SEARCH = "https://www.wikidata.org/w/api.php"
WIKIDATA_ENTITY = "https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"

FIELD_ORDER = [
    "name", "wikidata", "wikipedia", "thumbnail",
    "birth", "birth_source", "death", "death_source",
    "nationality", "nationality_source", "era", "era_source",
    "teachers", "students", "mentors", "sources",
]


def parse_year(val) -> int | None:
    if not val:
        return None
    m = re.search(r"\d{3,4}", str(val))
    return int(m.group()) if m else None


def _get_with_retry(client: httpx.Client, url: str, params: dict) -> httpx.Response:
    for attempt in range(8):
        r = client.get(url, params=params)
        if r.status_code == 429:
            wait = 10 * (attempt + 1)
            print(f"      rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r
    raise RuntimeError("Wikidata rate limited after 8 retries")


def search_wikidata(name: str, client: httpx.Client) -> list[dict]:
    """Search Wikidata for entities matching a name."""
    r = _get_with_retry(client, WIKIDATA_SEARCH, {
        "action": "wbsearchentities",
        "search": name,
        "language": "en",
        "type": "item",
        "limit": 5,
        "format": "json",
    })
    return r.json().get("search", [])


def get_entity_claims(qid: str, client: httpx.Client) -> dict | None:
    """Fetch birth/death year and occupation from a Wikidata entity."""
    r = _get_with_retry(client, WIKIDATA_SEARCH, {
        "action": "wbgetentities",
        "ids": qid,
        "props": "claims",
        "format": "json",
    })
    entities = r.json().get("entities", {})
    entity = entities.get(qid)
    if not entity:
        return None

    claims = entity.get("claims", {})
    result = {"birth": None, "death": None, "is_human": False, "is_musical": False}

    # P31 = instance of, Q5 = human
    for claim in claims.get("P31", []):
        val = claim.get("mainsnak", {}).get("datavalue", {}).get("value", {})
        if val.get("id") == "Q5":
            result["is_human"] = True

    # P106 = occupation — check for musical occupations
    musical_occupations = {
        "Q36834",    # composer
        "Q639669",   # musician
        "Q486748",   # pianist
        "Q1415090",  # film score composer
        "Q855091",   # guitarist
        "Q6625963",  # lyricist
        "Q158852",   # conductor
        "Q2865819",  # opera composer
        "Q177220",   # singer
        "Q806349",   # bandleader
        "Q1198887",  # music teacher
        "Q16145150", # organist
        "Q1075651",  # violinist
        "Q12800682", # harpsichordist
        "Q1371925",  # cellist
    }
    for claim in claims.get("P106", []):
        val = claim.get("mainsnak", {}).get("datavalue", {}).get("value", {})
        if val.get("id") in musical_occupations:
            result["is_musical"] = True

    # P569 = date of birth
    for claim in claims.get("P569", []):
        time_val = claim.get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("time", "")
        m = re.search(r"[+-](\d{4})", time_val)
        if m:
            result["birth"] = int(m.group(1))
            break

    # P570 = date of death
    for claim in claims.get("P570", []):
        time_val = claim.get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("time", "")
        m = re.search(r"[+-](\d{4})", time_val)
        if m:
            result["death"] = int(m.group(1))
            break

    return result


def match_composer(name: str, birth_year: int | None, client: httpx.Client) -> str | None:
    """Search Wikidata for a composer, return QID if confident match."""
    # Strip disambiguation suffixes before searching
    clean = re.sub(r"\s*\([^)]*\)$", "", name).strip()
    results = search_wikidata(clean, client)
    if not results and clean != name:
        results = search_wikidata(name, client)
    if not results:
        return None

    for result in results:
        qid = result["id"]
        entity = get_entity_claims(qid, client)
        if not entity:
            continue

        if not entity["is_human"]:
            continue

        # If we have a birth year, require it to match
        if birth_year and entity["birth"]:
            if abs(birth_year - entity["birth"]) > 2:
                continue
            return qid

        # If musical occupation matches, accept even without birth year
        if entity["is_musical"]:
            return qid

        # First human result with no conflicting data
        if not birth_year:
            desc = result.get("description", "").lower()
            if any(w in desc for w in ("composer", "musician", "pianist", "organist", "conductor", "singer", "violinist")):
                return qid

    return None


def reorder(rec: dict) -> dict:
    ordered = {}
    for field in FIELD_ORDER:
        if field in rec:
            ordered[field] = rec[field]
    return ordered


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true")
    args = ap.parse_args()

    missing = []
    for path in sorted(COMPOSERS_DIR.glob("*.md")):
        text = path.read_text()
        if not text.startswith("---"):
            continue
        parts = text.split("---", 2)
        if len(parts) < 3:
            continue
        try:
            rec = yaml.safe_load(parts[1])
        except Exception:
            continue
        if not rec or rec.get("wikidata"):
            continue
        missing.append((path, rec, parts[2]))

    print(f"Found {len(missing)} composers without Wikidata IDs.\n")

    matched = 0
    failed = []

    with httpx.Client(
        timeout=15.0,
        headers={"User-Agent": "ComposerGalaxy/1.0 (research project)"},
    ) as client:
        for i, (path, rec, body) in enumerate(missing):
            name = rec.get("name", path.stem)
            birth_year = parse_year(rec.get("birth"))

            qid = match_composer(name, birth_year, client)

            if qid:
                matched += 1
                print(f"  ✓ {name} → {qid}")
                if args.fix:
                    rec["wikidata"] = qid
                    ordered = reorder(rec)
                    new_yaml = yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True)
                    path.write_text(f"---\n{new_yaml}---\n{body}")
            else:
                failed.append(name)
                print(f"  ✗ {name}")

            if i < len(missing) - 1:
                time.sleep(3)

    print(f"\nMatched: {matched}/{len(missing)}")
    if failed:
        print(f"Unmatched ({len(failed)}):")
        for name in failed:
            print(f"  {name}")


if __name__ == "__main__":
    main()
