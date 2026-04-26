"""Validate LLM-sourced edges by cross-referencing and Wikidata lookup.

Two checks:
1. Cross-reference: does the reverse direction exist in another composer's file?
   e.g. A says B is student (llm) AND B says A is teacher (any source) → confirmed
2. Wikidata: does P1066 (student of) or P802 (student) confirm the relationship?

Edges passing either check get promoted from source="llm" to source="llm_confirmed".

Usage:
    python -m app.validate              # dry-run: print what would change
    python -m app.validate --apply      # write changes to composer files
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSERS_DIR = ROOT / "composers"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKI_API = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "ComposerGalaxy/1.0 (educational project)"}


def _load_all() -> dict[str, dict]:
    """Load all composer files, keyed by name."""
    records = {}
    for p in sorted(COMPOSERS_DIR.glob("*.md")):
        text = p.read_text()
        if not text.startswith("---"):
            continue
        _, fm, body = text.split("---", 2)
        rec = yaml.safe_load(fm)
        if rec and "name" in rec:
            rec["_path"] = p
            rec["_body"] = body
            records[rec["name"]] = rec
    return records


def _cross_reference(records: dict[str, dict]) -> set[tuple[str, str, str]]:
    """Find LLM edges confirmed by cross-reference.

    Returns set of (composer_name, edge_kind, related_name) tuples to promote.
    """
    confirmed = set()

    for name, rec in records.items():
        for t in rec.get("teachers") or []:
            if t.get("source") != "llm":
                continue
            other = records.get(t["name"])
            if not other:
                continue
            for s in other.get("students") or []:
                if s["name"] == name:
                    confirmed.add((name, "teachers", t["name"]))
                    break

        for s in rec.get("students") or []:
            if s.get("source") != "llm":
                continue
            other = records.get(s["name"])
            if not other:
                continue
            for t in other.get("teachers") or []:
                if t["name"] == name:
                    confirmed.add((name, "students", s["name"]))
                    break

    return confirmed


async def _get_wikidata_qid(title: str, retries: int = 3) -> str | None:
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=15.0, headers=HEADERS) as client:
                r = await client.get(WIKI_API, params={
                    "action": "query", "titles": title,
                    "prop": "pageprops", "ppprop": "wikibase_item",
                    "format": "json", "formatversion": "2", "redirects": "1",
                })
                pages = r.json().get("query", {}).get("pages", [])
                return pages[0].get("pageprops", {}).get("wikibase_item") if pages else None
        except Exception:
            if attempt < retries - 1:
                await asyncio.sleep(2)
    return None


async def _get_wikidata_relations(qid: str, retries: int = 3) -> dict[str, set[str]]:
    """Get P1066 (student of) and P802 (student) QIDs for a given entity."""
    result = {"teachers_qids": set(), "students_qids": set()}
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=15.0, headers=HEADERS) as client:
                r = await client.get(WIKIDATA_API, params={
                    "action": "wbgetclaims", "entity": qid,
                    "property": "P1066", "format": "json",
                })
                for claim in r.json().get("claims", {}).get("P1066", []):
                    try:
                        result["teachers_qids"].add(claim["mainsnak"]["datavalue"]["value"]["id"])
                    except (KeyError, TypeError):
                        pass

                r2 = await client.get(WIKIDATA_API, params={
                    "action": "wbgetclaims", "entity": qid,
                    "property": "P802", "format": "json",
                })
                for claim in r2.json().get("claims", {}).get("P802", []):
                    try:
                        result["students_qids"].add(claim["mainsnak"]["datavalue"]["value"]["id"])
                    except (KeyError, TypeError):
                        pass
                return result
        except Exception:
            if attempt < retries - 1:
                await asyncio.sleep(2)
    return result


async def _wikidata_validate(records: dict[str, dict]) -> set[tuple[str, str, str]]:
    """Check LLM edges against Wikidata P1066/P802."""
    confirmed = set()

    llm_edges = []
    for name, rec in records.items():
        for t in rec.get("teachers") or []:
            if t.get("source") == "llm":
                llm_edges.append((name, "teachers", t["name"]))
        for s in rec.get("students") or []:
            if s.get("source") == "llm":
                llm_edges.append((name, "students", s["name"]))

    if not llm_edges:
        return confirmed

    all_names = set()
    for n, kind, related in llm_edges:
        all_names.add(n)
        all_names.add(related)

    print(f"  Looking up {len(all_names)} composers on Wikidata...")
    name_to_qid = {}
    qid_to_name = {}
    for i, name in enumerate(sorted(all_names)):
        wiki_title = name.replace(" ", "_")
        rec = records.get(name)
        if rec and rec.get("wikipedia"):
            wiki_title = rec["wikipedia"].split("/wiki/")[-1]
        qid = await _get_wikidata_qid(wiki_title.replace("_", " "))
        if qid:
            name_to_qid[name] = qid
            qid_to_name[qid] = name
        if (i + 1) % 20 == 0:
            print(f"    ...{i + 1}/{len(all_names)} QIDs resolved")
        await asyncio.sleep(0.2)

    print(f"  Resolved {len(name_to_qid)} QIDs. Checking relationships...")
    checked = set()
    for name, kind, related in llm_edges:
        qid = name_to_qid.get(name)
        if not qid or qid in checked:
            continue
        checked.add(qid)

        rels = await _get_wikidata_relations(qid)

        for t in records[name].get("teachers") or []:
            if t.get("source") == "llm":
                t_qid = name_to_qid.get(t["name"])
                if t_qid and t_qid in rels["teachers_qids"]:
                    confirmed.add((name, "teachers", t["name"]))

        for s in records[name].get("students") or []:
            if s.get("source") == "llm":
                s_qid = name_to_qid.get(s["name"])
                if s_qid and s_qid in rels["students_qids"]:
                    confirmed.add((name, "students", s["name"]))

        if len(checked) % 20 == 0:
            print(f"    ...{len(checked)} composers checked on Wikidata")
            await asyncio.sleep(0.5)

    return confirmed


def _apply_promotions(records: dict[str, dict], confirmed: set[tuple[str, str, str]]):
    """Rewrite composer files, promoting confirmed LLM edges."""
    affected_files = set()
    for name, kind, related in confirmed:
        rec = records[name]
        for edge in rec.get(kind) or []:
            if edge["name"] == related and edge.get("source") == "llm":
                edge["source"] = "llm_confirmed"
                affected_files.add(name)

    for name in affected_files:
        rec = records[name]
        path = rec["_path"]
        body = rec["_body"]
        fm_data = {k: v for k, v in rec.items() if not k.startswith("_")}
        fm = yaml.safe_dump(fm_data, sort_keys=False, allow_unicode=True)
        path.write_text(f"---\n{fm}---{body}")

    return len(affected_files)


async def main(apply: bool):
    records = _load_all()
    print(f"Loaded {len(records)} composers.\n")

    print("Step 1: Cross-reference validation...")
    xref = _cross_reference(records)
    print(f"  → {len(xref)} edges confirmed by cross-reference\n")

    print("Step 2: Wikidata validation...")
    wikidata = await _wikidata_validate(records)
    print(f"  → {len(wikidata)} edges confirmed by Wikidata\n")

    all_confirmed = xref | wikidata
    both = xref & wikidata
    xref_only = xref - wikidata
    wd_only = wikidata - xref

    print(f"Summary:")
    print(f"  Confirmed by both:          {len(both)}")
    print(f"  Confirmed by cross-ref only: {len(xref_only)}")
    print(f"  Confirmed by Wikidata only:  {len(wd_only)}")
    print(f"  Total unique confirmed:      {len(all_confirmed)}")

    if all_confirmed:
        print(f"\nConfirmed edges:")
        for name, kind, related in sorted(all_confirmed):
            sources = []
            if (name, kind, related) in xref:
                sources.append("xref")
            if (name, kind, related) in wikidata:
                sources.append("wikidata")
            print(f"  {name} → {kind} → {related}  [{', '.join(sources)}]")

    if apply and all_confirmed:
        n = _apply_promotions(records, all_confirmed)
        print(f"\n✓ Updated {n} composer files.")
    elif all_confirmed:
        print(f"\nDry run — use --apply to write changes.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Write changes to files")
    args = ap.parse_args()
    asyncio.run(main(args.apply))
