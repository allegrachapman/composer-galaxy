#!/usr/bin/env python3
"""Enforce canonical field order and schema on all composer files.

Usage:
    python scripts/canonicalize.py             # dry-run: report issues
    python scripts/canonicalize.py --fix       # rewrite files with canonical order
"""

import argparse
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSERS_DIR = ROOT / "composers"

# Canonical field order — this IS the schema
FIELD_ORDER = [
    "name",             # str, required
    "wikidata",         # str (Q-id) or null
    "wikipedia",        # str (URL) or null
    "thumbnail",        # str (URL) or null
    "birth",            # str (year) or null
    "birth_source",     # str or null
    "death",            # str (year) or null
    "death_source",     # str or null
    "nationality",      # str or null
    "nationality_source",  # str or null
    "era",              # str or null
    "era_source",       # str or null
    "teachers",         # list of edge dicts
    "students",         # list of edge dicts
    "mentors",          # list of edge dicts
    "sources",          # list of URLs (optional)
]

REQUIRED_FIELDS = {"name", "teachers", "students", "mentors"}

VALID_SOURCES = {"manual", "infobox", "grove", "wiki", "llm", "wikidata"}

VALID_ERAS = {
    "Medieval", "Renaissance", "Baroque", "Classical", "Romantic",
    "20th-century", "21st-century", "Contemporary",
}


def validate(rec: dict, fname: str) -> list[str]:
    issues = []

    for field in REQUIRED_FIELDS:
        if field not in rec:
            issues.append(f"missing required field: {field}")

    for key in rec:
        if key not in FIELD_ORDER:
            issues.append(f"unknown field: {key}")

    if rec.get("wikidata") and not re.match(r"^Q\d+$", str(rec["wikidata"])):
        issues.append(f"invalid wikidata ID: {rec['wikidata']}")

    for src_field in ("birth_source", "death_source", "nationality_source", "era_source"):
        val = rec.get(src_field)
        if val and val not in VALID_SOURCES:
            issues.append(f"{src_field} has unknown source: {val}")

    for field in ("teachers", "students", "mentors"):
        for i, edge in enumerate(rec.get(field) or []):
            if not isinstance(edge, dict):
                issues.append(f"{field}[{i}]: not a dict")
                continue
            if not edge.get("name"):
                issues.append(f"{field}[{i}]: missing name")
            if edge.get("source") and edge["source"] not in VALID_SOURCES:
                issues.append(f"{field}[{i}] ({edge.get('name')}): unknown source '{edge['source']}'")

    return issues


def reorder(rec: dict) -> dict:
    ordered = {}
    for field in FIELD_ORDER:
        if field in rec:
            ordered[field] = rec[field]
        elif field in REQUIRED_FIELDS:
            if field in ("teachers", "students", "mentors"):
                ordered[field] = []
            else:
                ordered[field] = None
        elif field in ("wikidata", "wikipedia", "thumbnail"):
            ordered[field] = rec.get(field)
    return ordered


def process_file(path: Path, fix: bool) -> list[str]:
    text = path.read_text()
    if not text.startswith("---"):
        return [f"no frontmatter"]

    parts = text.split("---", 2)
    if len(parts) < 3:
        return [f"malformed frontmatter"]

    try:
        rec = yaml.safe_load(parts[1])
    except Exception as e:
        return [f"YAML parse error: {e}"]

    if not rec:
        return [f"empty frontmatter"]

    issues = validate(rec, path.name)

    if fix:
        ordered = reorder(rec)
        new_yaml = yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True)
        path.write_text(f"---\n{new_yaml}---\n{parts[2]}")

    return issues


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true", help="Rewrite files with canonical field order")
    args = ap.parse_args()

    total = 0
    with_issues = 0
    total_issues = 0
    fields_missing = {"wikidata": 0, "wikipedia": 0, "birth": 0, "death": 0, "era": 0}

    for path in sorted(COMPOSERS_DIR.glob("*.md")):
        total += 1
        issues = process_file(path, args.fix)

        # Count missing optional fields
        text = path.read_text()
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                try:
                    rec = yaml.safe_load(parts[1])
                    if rec:
                        for f in fields_missing:
                            if not rec.get(f):
                                fields_missing[f] += 1
                except Exception:
                    pass

        if issues:
            with_issues += 1
            total_issues += len(issues)
            print(f"{path.name}:")
            for issue in issues:
                print(f"  - {issue}")

    print(f"\n{'Fixed' if args.fix else 'Checked'} {total} files.")
    if with_issues:
        print(f"{with_issues} files with {total_issues} issues.")
    else:
        print("All files valid.")

    print(f"\nMissing data summary:")
    for field, count in fields_missing.items():
        pct = count / total * 100 if total else 0
        print(f"  {field}: {count}/{total} missing ({pct:.0f}%)")


if __name__ == "__main__":
    main()
