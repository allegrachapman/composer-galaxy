#!/usr/bin/env python3
"""Add corroborated_by tags to existing edges via cross-referencing.

Reads each composer file, cross-references edges across all files
(including reciprocal: A→student→B matches B→teacher→A), and tags
edges found by multiple sources with corroborated_by lists.
No network calls, no LLM calls, no API credits.

Uses targeted text insertion instead of YAML round-tripping to avoid
reformatting or data loss.

Usage:
    python scripts/add_corroboration.py           # dry-run
    python scripts/add_corroboration.py --fix     # write changes
"""

import argparse
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSERS_DIR = ROOT / "composers"

DISAMBIG_RE = re.compile(
    r"\s*\((?:composer|musician|pianist|organist|violinist|singer|conductor|cellist|flautist|musicologist)\)$",
    re.IGNORECASE,
)


def _normalize(name: str) -> str:
    return DISAMBIG_RE.sub("", name).strip().lower()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true")
    args = ap.parse_args()

    # First pass: collect all edges keyed by (composer, field, edge_name) → set of sources
    all_edges: dict[str, set[str]] = {}

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
        if not rec or not rec.get("name"):
            continue

        composer = _normalize(rec["name"])
        for field in ("teachers", "students", "mentors"):
            for edge in rec.get(field) or []:
                edge_name = _normalize(edge.get("name", ""))
                if not edge_name:
                    continue
                source = edge.get("source", "unknown")
                key = f"{composer}::{field}::{edge_name}"
                all_edges.setdefault(key, set()).add(source)

    reciprocal_map = {"teachers": "students", "students": "teachers"}

    total = 0
    updated = 0
    edges_tagged = 0

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
        if not rec or not rec.get("name"):
            continue

        total += 1
        composer = _normalize(rec["name"])
        file_changed = False

        # Build list of (edge_name_raw, sorted_sources) for edges that need tagging
        tags_to_add: list[tuple[str, str, list[str]]] = []

        for field in ("teachers", "students", "mentors"):
            for edge in rec.get(field) or []:
                edge_name = _normalize(edge.get("name", ""))
                if not edge_name:
                    continue

                sources = set()
                source = edge.get("source", "unknown")
                sources.add(source)

                fwd_key = f"{composer}::{field}::{edge_name}"
                if fwd_key in all_edges:
                    sources |= all_edges[fwd_key]

                rev_field = reciprocal_map.get(field)
                if rev_field:
                    rev_key = f"{edge_name}::{rev_field}::{composer}"
                    if rev_key in all_edges:
                        sources |= all_edges[rev_key]

                if len(sources) > 1:
                    sorted_sources = sorted(sources)
                    if edge.get("corroborated_by") != sorted_sources:
                        tags_to_add.append((edge["name"], field, sorted_sources))

        if not tags_to_add:
            continue

        # Do targeted text insertion in the raw frontmatter
        fm = parts[1]
        for edge_name_raw, field, sorted_sources in tags_to_add:
            # Remove any existing corroborated_by for this edge
            # Find the edge entry in the raw YAML text
            # We need to find "- name: <edge_name_raw>" within the field section
            # and insert/replace corroborated_by after the edge's last property

            escaped_name = re.escape(edge_name_raw)
            # Match the entire edge block: starts with "- name: ..." and continues
            # with indented lines (2+ spaces, not starting with "- ")
            edge_pattern = re.compile(
                rf"(- name: {escaped_name}\n(?:  [^\n]+\n)*)",
                re.MULTILINE,
            )

            matches = list(edge_pattern.finditer(fm))
            if not matches:
                # Try quoted name
                edge_pattern2 = re.compile(
                    rf"(- name: '[^']*{re.escape(edge_name_raw)}[^']*'\n(?:  [^\n]+\n)*)",
                    re.MULTILINE,
                )
                matches = list(edge_pattern2.finditer(fm))

            if not matches:
                continue

            # Use the first match (should be unique within a field section)
            # But we need to make sure we're in the right field section
            for match in matches:
                block = match.group(1)
                # Remove any existing corroborated_by lines from this block
                cleaned = re.sub(r"  corroborated_by:\n(?:  - [^\n]+\n)*", "", block)
                # Build the corroborated_by YAML lines
                corr_lines = "  corroborated_by:\n"
                for s in sorted_sources:
                    corr_lines += f"  - {s}\n"
                # Append corroborated_by at the end of the block
                new_block = cleaned.rstrip("\n") + "\n" + corr_lines
                fm = fm.replace(block, new_block, 1)
                file_changed = True
                edges_tagged += 1
                break

        if file_changed:
            updated += 1
            if args.fix:
                path.write_text(f"---{fm}---{parts[2]}")

    action = "Updated" if args.fix else "Would update"
    print(f"{action} {updated}/{total} files ({edges_tagged} edges tagged as corroborated).")


if __name__ == "__main__":
    main()
