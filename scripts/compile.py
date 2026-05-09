#!/usr/bin/env python3
"""Compile composer markdown files into normalized JSON tables.

Produces output matching data/schema.json — with provenance (origin + method),
confidence scores, and consistency flags.

Usage:
    python scripts/compile.py              # compile + report inconsistencies
    python scripts/compile.py --check      # check only, don't write output

Outputs:
    data/compiled.json  — single file with composers, edges, and metadata
"""

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSERS_DIR = ROOT / "composers"
DATA_DIR = ROOT / "data"

# old source → (origin, method)
SOURCE_MAP = {
    "manual":   ("manual",    "human"),
    "infobox":  ("wikipedia", "parser"),
    "wiki":     ("wikipedia", "regex"),
    "llm":      ("wikipedia", "llm"),
    "grove":    ("grove",     "llm"),
    "wikidata": ("wikidata",  "api"),
}

BASE_CONFIDENCE = {
    "human":  0.95,
    "api":    0.90,
    "parser": 0.85,
    "regex":  0.85,
    "llm":    0.70,
}

MOD_QUOTE = 0.10
MOD_CORROBORATED = 0.10
MOD_RECIPROCAL = 0.05
MOD_HAIKU_VERIFIED = 0.05


def _parse_year(val) -> str | None:
    if not val:
        return None
    m = re.search(r"\d{3,4}", str(val))
    return m.group() if m else None


def _slug(name: str) -> str:
    s = name.lower()
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def _normalize_name(name: str) -> str:
    return re.sub(
        r"\s*\((?:composer|musician|pianist|organist|violinist|singer|conductor|cellist|flautist|musicologist)\)$",
        "", name, flags=re.IGNORECASE,
    ).strip()


def _stable_id(rec: dict) -> str:
    if rec.get("wikidata"):
        return rec["wikidata"]
    return f"slug:{_slug(rec['name'])}"


def _map_source(old_source: str) -> tuple[str, str]:
    return SOURCE_MAP.get(old_source, ("unknown", "unknown"))


def _base_confidence(method: str) -> float:
    return BASE_CONFIDENCE.get(method, 0.50)


def _provenance(value, source: str | None) -> dict | None:
    if value is None:
        return None
    origin, method = _map_source(source or "unknown")
    conf = _base_confidence(method)
    return {
        "value": str(value),
        "origin": origin,
        "method": method,
        "confidence": round(conf, 2),
    }


def load_all() -> list[tuple[Path, dict]]:
    results = []
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
        if rec and rec.get("name"):
            results.append((path, rec))
    return results


def compile_composers(records: list[tuple[Path, dict]]) -> dict:
    composers = {}
    for path, rec in records:
        sid = _stable_id(rec)
        composers[sid] = {
            "id": sid,
            "name": rec["name"],
            "slug": _slug(rec["name"]),
            "file": path.name,
            "wikidata": rec.get("wikidata"),
            "wikipedia": rec.get("wikipedia"),
            "thumbnail": rec.get("thumbnail"),
            "birth": _provenance(rec.get("birth"), rec.get("birth_source")),
            "death": _provenance(rec.get("death"), rec.get("death_source")),
            "nationality": _provenance(rec.get("nationality"), rec.get("nationality_source")),
            "era": _provenance(rec.get("era"), rec.get("era_source")),
        }
    return composers


def compile_edges(records: list[tuple[Path, dict]], composer_index: dict) -> list[dict]:
    name_to_id = {}
    for sid, c in composer_index.items():
        name_to_id[_normalize_name(c["name"]).lower()] = sid

    edges = []
    seen = set()

    for path, rec in records:
        from_id = _stable_id(rec)
        from_name = rec["name"]

        for field, edge_type in [("teachers", "teacher"), ("students", "student"), ("mentors", "mentor")]:
            for edge in rec.get(field) or []:
                edge_name = _normalize_name(edge["name"])
                to_id = name_to_id.get(edge_name.lower())
                key = (from_id, to_id or edge_name, edge_type)
                if key in seen:
                    continue
                seen.add(key)

                old_source = edge.get("source", "unknown")
                origin, method = _map_source(old_source)
                conf = _base_confidence(method)

                if edge.get("quote"):
                    conf += MOD_QUOTE

                edges.append({
                    "from": from_id,
                    "from_name": from_name,
                    "to": to_id,
                    "to_name": edge_name,
                    "type": edge_type,
                    "origin": origin,
                    "method": method,
                    "confidence": round(conf, 2),
                    "quote": edge.get("quote"),
                    "source_url": edge.get("source_url"),
                    "haiku_verified": False,
                    "corroborated": False,
                    "has_reciprocal": False,
                })

    # Second pass: compute corroboration, reciprocals
    edge_lookup = {}
    for e in edges:
        key = (_normalize_name(e["from_name"]).lower(), _normalize_name(e["to_name"]).lower(), e["type"])
        edge_lookup.setdefault(key, []).append(e)

    # Corroboration: same (from, to, type) found by multiple origins
    for key, group in edge_lookup.items():
        origins = {e["origin"] for e in group}
        if len(origins) > 1:
            for e in group:
                e["corroborated"] = True
                e["confidence"] = round(min(e["confidence"] + MOD_CORROBORATED, 1.0), 2)

    # Reciprocals
    reciprocal_map = {
        "teacher": "student",
        "student": "teacher",
    }
    for e in edges:
        if not e.get("to"):
            continue
        reverse_type = reciprocal_map.get(e["type"])
        if not reverse_type:
            continue
        reverse_key = (_normalize_name(e["to_name"]).lower(), _normalize_name(e["from_name"]).lower(), reverse_type)
        if reverse_key in edge_lookup:
            e["has_reciprocal"] = True
            if e["confidence"] < 1.0:
                e["confidence"] = round(min(e["confidence"] + MOD_RECIPROCAL, 1.0), 2)

    return edges


def check_consistency(composer_index: dict, edges: list[dict]) -> list[str]:
    issues = []

    edge_set = set()
    for e in edges:
        if e.get("to"):
            edge_set.add((e["from"], e["to"], e["type"]))

    for e in edges:
        if not e.get("to"):
            continue
        if e["type"] == "teacher":
            if (e["to"], e["from"], "student") not in edge_set:
                issues.append(
                    f"missing reciprocal: {e['from_name']} lists {e['to_name']} as teacher, "
                    f"but {e['to_name']} doesn't list {e['from_name']} as student"
                )
        elif e["type"] == "student":
            if (e["to"], e["from"], "teacher") not in edge_set:
                issues.append(
                    f"missing reciprocal: {e['from_name']} lists {e['to_name']} as student, "
                    f"but {e['to_name']} doesn't list {e['from_name']} as teacher"
                )

    dangling = [e for e in edges if not e["to"]]
    for e in dangling:
        issues.append(f"dangling edge: {e['from_name']} → {e['to_name']} ({e['type']}, no file)")

    seen_names = {}
    for sid, c in composer_index.items():
        norm = _normalize_name(c["name"]).lower()
        if norm in seen_names:
            issues.append(f"duplicate name: {c['name']} ({sid}) vs {seen_names[norm]}")
        seen_names[norm] = f"{c['name']} ({sid})"

    return issues


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="Check only, don't write output files")
    args = ap.parse_args()

    records = load_all()
    print(f"Loaded {len(records)} composer files.")

    composer_index = compile_composers(records)
    edges = compile_edges(records, composer_index)

    resolved = sum(1 for e in edges if e["to"])
    dangling = sum(1 for e in edges if not e["to"])
    print(f"Compiled {len(edges)} edges ({resolved} resolved, {dangling} dangling).")

    by_type = {}
    for e in edges:
        by_type[e["type"]] = by_type.get(e["type"], 0) + 1
    for t, count in sorted(by_type.items()):
        print(f"  {t}: {count}")

    print(f"\nEdge origins:")
    by_origin = {}
    for e in edges:
        by_origin[e["origin"]] = by_origin.get(e["origin"], 0) + 1
    for s, count in sorted(by_origin.items(), key=lambda x: -x[1]):
        print(f"  {s}: {count}")

    print(f"\nExtraction methods:")
    by_method = {}
    for e in edges:
        by_method[e["method"]] = by_method.get(e["method"], 0) + 1
    for m, count in sorted(by_method.items(), key=lambda x: -x[1]):
        print(f"  {m}: {count}")

    # Confidence distribution
    brackets = {"0.95-1.0": 0, "0.85-0.94": 0, "0.70-0.84": 0, "<0.70": 0}
    for e in edges:
        c = e["confidence"]
        if c >= 0.95:
            brackets["0.95-1.0"] += 1
        elif c >= 0.85:
            brackets["0.85-0.94"] += 1
        elif c >= 0.70:
            brackets["0.70-0.84"] += 1
        else:
            brackets["<0.70"] += 1
    print(f"\nConfidence distribution:")
    for bracket, count in brackets.items():
        print(f"  {bracket}: {count}")

    corroborated = sum(1 for e in edges if e["corroborated"])
    with_reciprocal = sum(1 for e in edges if e["has_reciprocal"])
    with_quote = sum(1 for e in edges if e.get("quote"))
    print(f"\nQuality signals:")
    print(f"  With quotes: {with_quote}")
    print(f"  Corroborated: {corroborated}")
    print(f"  Has reciprocal: {with_reciprocal}")

    issues = check_consistency(composer_index, edges)
    if issues:
        reciprocal_issues = [i for i in issues if i.startswith("missing reciprocal")]
        dangling_issues = [i for i in issues if i.startswith("dangling edge")]
        duplicate_issues = [i for i in issues if i.startswith("duplicate name")]

        print(f"\nConsistency issues: {len(issues)} total")
        print(f"  Missing reciprocals: {len(reciprocal_issues)}")
        print(f"  Dangling edges: {len(dangling_issues)}")
        print(f"  Duplicate names: {len(duplicate_issues)}")

        if duplicate_issues:
            print(f"\nDuplicate names:")
            for i in duplicate_issues:
                print(f"  {i}")

        if reciprocal_issues[:10]:
            print(f"\nSample missing reciprocals:")
            for i in reciprocal_issues[:10]:
                print(f"  {i}")
    else:
        print("\nNo consistency issues (excluding dangling edges).")

    if not args.check:
        def clean(obj):
            if isinstance(obj, dict):
                return {k: clean(v) for k, v in obj.items() if v is not None}
            if isinstance(obj, list):
                return [clean(x) for x in obj]
            return obj

        output = {
            "composers": clean(composer_index),
            "edges": clean(edges),
            "metadata": {
                "version": "2.0",
                "compiled_at": datetime.now(timezone.utc).isoformat(),
                "composer_count": len(composer_index),
                "edge_count": len(edges),
                "confidence_model": {
                    "base_scores": BASE_CONFIDENCE,
                    "modifiers": {
                        "has_verified_quote": MOD_QUOTE,
                        "corroborated": MOD_CORROBORATED,
                        "has_reciprocal": MOD_RECIPROCAL,
                        "haiku_verified": MOD_HAIKU_VERIFIED,
                    },
                    "max": 1.0,
                },
            },
        }

        DATA_DIR.mkdir(exist_ok=True)
        with open(DATA_DIR / "compiled.json", "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        print(f"\nWrote data/compiled.json")
        print(f"  {len(composer_index)} composers, {len(edges)} edges")


if __name__ == "__main__":
    main()
