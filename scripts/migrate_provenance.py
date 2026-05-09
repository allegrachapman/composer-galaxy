#!/usr/bin/env python3
"""Migrate source field to separate origin + method fields.

Old:  source: llm
New:  origin: wikipedia, method: llm

Keeps the old `source` field during transition. Run with --drop-source
to remove it once all code reads the new fields.

Usage:
    python scripts/migrate_provenance.py           # dry-run
    python scripts/migrate_provenance.py --fix     # add origin + method
    python scripts/migrate_provenance.py --fix --drop-source  # also remove old source
"""

import argparse
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

# Mapping: old source → (origin, method)
SOURCE_MAP = {
    "manual":   ("manual",    "human"),
    "infobox":  ("wikipedia", "parser"),
    "wiki":     ("wikipedia", "regex"),
    "llm":      ("wikipedia", "llm"),
    "grove":    ("grove",     "llm"),
    "wikidata": ("wikidata",  "api"),
}

# For scalar _source fields
SCALAR_SOURCE_MAP = {
    "manual":   ("manual",    "human"),
    "infobox":  ("wikipedia", "parser"),
    "llm":      ("wikipedia", "llm"),
    "grove":    ("grove",     "llm"),
    "wikidata": ("wikidata",  "api"),
}

SCALAR_FIELDS = ["birth", "death", "nationality", "era"]


def migrate_edge(edge: dict, drop_source: bool) -> dict:
    """Add origin + method to an edge dict."""
    old = edge.get("source", "unknown")
    origin, method = SOURCE_MAP.get(old, ("unknown", "unknown"))

    # Special case: wiki edges with source_url are from wiki list pages
    # origin is still wikipedia, method is still regex
    # But we can note the specific list page in source_url

    edge["origin"] = origin
    edge["method"] = method

    if drop_source and "source" in edge:
        del edge["source"]

    return edge


def migrate_record(rec: dict, drop_source: bool) -> dict:
    """Migrate all fields in a composer record."""
    for field in SCALAR_FIELDS:
        src_key = f"{field}_source"
        old = rec.get(src_key)
        if old:
            origin, method = SCALAR_SOURCE_MAP.get(old, ("unknown", "unknown"))
            rec[f"{field}_origin"] = origin
            rec[f"{field}_method"] = method
            if drop_source:
                del rec[src_key]

    for field in ("teachers", "students", "mentors"):
        for edge in rec.get(field) or []:
            migrate_edge(edge, drop_source)

    return rec


def reorder(rec: dict) -> dict:
    """Reorder fields, inserting new origin/method fields after their scalar."""
    ordered = {}

    for field in FIELD_ORDER:
        if field in ("birth", "death", "nationality", "era"):
            if field in rec:
                ordered[field] = rec[field]
            # Add all variants of the source/origin/method fields
            for suffix in ("_source", "_origin", "_method"):
                key = f"{field}{suffix}"
                if key in rec:
                    ordered[key] = rec[key]
        elif field in rec:
            ordered[field] = rec[field]

    return ordered


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--drop-source", action="store_true",
                    help="Remove old source fields (only after all code is updated)")
    args = ap.parse_args()

    if args.drop_source and not args.fix:
        print("--drop-source requires --fix")
        return

    total = 0
    migrated = 0
    already_done = 0
    edge_count = 0
    scalar_count = 0

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
        if not rec:
            continue

        total += 1

        # Check if already migrated
        has_new = any(rec.get(f"{f}_origin") for f in SCALAR_FIELDS)
        has_edge_origin = any(
            e.get("origin") for field in ("teachers", "students", "mentors")
            for e in (rec.get(field) or [])
        )
        if has_new and has_edge_origin and not args.drop_source:
            already_done += 1
            continue

        # Count what we're migrating
        for f in SCALAR_FIELDS:
            if rec.get(f"{f}_source"):
                scalar_count += 1
        for field in ("teachers", "students", "mentors"):
            for e in rec.get(field) or []:
                if e.get("source"):
                    edge_count += 1

        if args.fix:
            rec = migrate_record(rec, args.drop_source)
            ordered = reorder(rec)
            new_yaml = yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True)
            path.write_text(f"---\n{new_yaml}---\n{parts[2]}")

        migrated += 1

    action = "Migrated" if args.fix else "Would migrate"
    print(f"{action} {migrated}/{total} files ({already_done} already done).")
    print(f"  {scalar_count} scalar fields, {edge_count} edge fields.")
    if args.drop_source:
        print("  Old `source` / `_source` fields removed.")
    else:
        print("  Old `source` / `_source` fields preserved (run with --drop-source to remove).")


if __name__ == "__main__":
    main()
