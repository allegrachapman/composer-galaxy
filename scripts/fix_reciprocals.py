#!/usr/bin/env python3
"""Add missing reciprocal edges to composer files.

If A lists B as teacher but B doesn't list A as student, this script
adds the reverse edge to B's file with the same source and quote.

Usage:
    python scripts/fix_reciprocals.py          # dry-run: show what would change
    python scripts/fix_reciprocals.py --fix    # write changes
"""

import argparse
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSERS_DIR = ROOT / "composers"

FIELD_ORDER = [
    "name", "wikidata", "wikipedia", "thumbnail",
    "birth", "birth_source", "death", "death_source",
    "nationality", "nationality_source", "era", "era_source",
    "teachers", "students", "mentors", "sources",
]


def _normalize_name(name: str) -> str:
    return re.sub(
        r"\s*\((?:composer|musician|pianist|organist|violinist|singer|conductor|cellist|flautist|musicologist)\)$",
        "", name, flags=re.IGNORECASE,
    ).strip()


def _reorder(rec: dict) -> dict:
    ordered = {}
    for field in FIELD_ORDER:
        if field in rec:
            ordered[field] = rec[field]
    return ordered


def load_all() -> dict[str, tuple[Path, dict, str]]:
    """Load all composer files. Returns {normalized_name: (path, record, body)}."""
    composers = {}
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
            key = _normalize_name(rec["name"]).lower()
            composers[key] = (path, rec, parts[2])
    return composers


def find_missing(composers: dict) -> list[dict]:
    """Find edges that exist in one direction but not the other."""
    existing = set()
    for key, (path, rec, body) in composers.items():
        name = _normalize_name(rec["name"])
        for edge in rec.get("teachers") or []:
            existing.add((name.lower(), _normalize_name(edge["name"]).lower(), "teacher"))
        for edge in rec.get("students") or []:
            existing.add((name.lower(), _normalize_name(edge["name"]).lower(), "student"))

    missing = []
    for key, (path, rec, body) in composers.items():
        name = _normalize_name(rec["name"])

        for edge in rec.get("teachers") or []:
            teacher_name = _normalize_name(edge["name"])
            if teacher_name.lower() not in composers:
                continue
            if (teacher_name.lower(), name.lower(), "student") not in existing:
                missing.append({
                    "target_key": teacher_name.lower(),
                    "target_name": teacher_name,
                    "add_to_field": "students",
                    "add_name": name,
                    "source": edge.get("source", "unknown"),
                    "quote": edge.get("quote"),
                    "source_url": edge.get("source_url"),
                    "origin": f"{rec['name']}'s teacher list",
                })

        for edge in rec.get("students") or []:
            student_name = _normalize_name(edge["name"])
            if student_name.lower() not in composers:
                continue
            if (student_name.lower(), name.lower(), "teacher") not in existing:
                missing.append({
                    "target_key": student_name.lower(),
                    "target_name": student_name,
                    "add_to_field": "teachers",
                    "add_name": name,
                    "source": edge.get("source", "unknown"),
                    "quote": edge.get("quote"),
                    "source_url": edge.get("source_url"),
                    "origin": f"{rec['name']}'s student list",
                })

    return missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true")
    args = ap.parse_args()

    composers = load_all()
    missing = find_missing(composers)

    print(f"Found {len(missing)} missing reciprocal edges.\n")

    changes = {}
    for m in missing:
        target_key = m["target_key"]
        changes.setdefault(target_key, []).append(m)

    for target_key, additions in sorted(changes.items()):
        path, rec, body = composers[target_key]
        print(f"{rec['name']} ({path.name}):")
        for a in additions:
            print(f"  + {a['add_to_field']}: {a['add_name']} [{a['source']}] (from {a['origin']})")

        if args.fix:
            for a in additions:
                entry = {"name": a["add_name"], "source": a["source"]}
                if a.get("quote"):
                    entry["quote"] = a["quote"]
                if a.get("source_url"):
                    entry["source_url"] = a["source_url"]
                rec.setdefault(a["add_to_field"], []).append(entry)

            ordered = _reorder(rec)
            new_yaml = yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True)
            path.write_text(f"---\n{new_yaml}---\n{body}")

    print(f"\n{'Fixed' if args.fix else 'Would fix'} {len(changes)} composer files with {len(missing)} new edges.")


if __name__ == "__main__":
    main()
