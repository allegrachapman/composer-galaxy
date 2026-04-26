"""Load composer markdown files → Cytoscape-style nodes and edges JSON."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_DISAMBIG_RE = re.compile(r"\s*\((?:composer|musician|pianist|organist|violinist|singer|conductor|cellist|flautist|musicologist)\)$", re.IGNORECASE)

ROOT = Path(__file__).resolve().parent.parent
COMPOSERS_DIR = ROOT / "composers"


def _load_file(path: Path) -> dict | None:
    text = path.read_text()
    if not text.startswith("---"):
        return None
    _, fm, _ = text.split("---", 2)
    return yaml.safe_load(fm)


def _node_id(name: str) -> str:
    return _DISAMBIG_RE.sub("", name).strip()


def load_graph() -> dict[str, Any]:
    """Return {nodes: [...], edges: [...]} in Cytoscape element format."""
    if not COMPOSERS_DIR.exists():
        return {"nodes": [], "edges": []}

    records: list[dict] = []
    for p in sorted(COMPOSERS_DIR.glob("*.md")):
        rec = _load_file(p)
        if rec:
            records.append(rec)

    node_map: dict[str, dict] = {}
    edges: list[dict] = []

    for rec in records:
        name = rec["name"]
        clean_name = _DISAMBIG_RE.sub("", name).strip()
        node_map[_node_id(name)] = {
            "data": {
                "id": _node_id(name),
                "label": clean_name,
                "birth": rec.get("birth"),
                "death": rec.get("death"),
                "nationality": rec.get("nationality"),
                "era": rec.get("era"),
                "stub": False,
                "wikipedia": rec.get("wikipedia"),
                "thumbnail": rec.get("thumbnail"),
            }
        }

    def ensure_node(name: str):
        nid = _node_id(name)
        if nid not in node_map:
            node_map[nid] = {
                "data": {
                    "id": nid,
                    "label": _DISAMBIG_RE.sub("", name).strip(),
                    "stub": True,
                    "era": None,
                }
            }

    def edge(src: str, tgt: str, kind: str, source: str, source_url: str | None = None, quote: str | None = None):
        ensure_node(src)
        ensure_node(tgt)
        d = {
            "id": f"{_node_id(src)}__{kind}__{_node_id(tgt)}",
            "source": _node_id(src),
            "target": _node_id(tgt),
            "kind": kind,
            "source_tag": source,
            "source_url": source_url,
        }
        if quote:
            d["quote"] = quote
        edges.append({"data": d})

    for rec in records:
        name = rec["name"]
        wiki_url = rec.get("wikipedia")
        for t in rec.get("teachers") or []:
            edge(t["name"], name, "teacher", t["source"], wiki_url, t.get("quote"))
        for s in rec.get("students") or []:
            edge(name, s["name"], "teacher", s["source"], wiki_url, s.get("quote"))

    # Dedupe edges (same src/tgt/kind): prefer infobox > wiki > manual > llm
    priority = {"infobox": 0, "wiki": 1, "manual": 2, "llm_confirmed": 3, "llm": 4}
    best: dict[tuple, dict] = {}
    for e in edges:
        d = e["data"]
        key = (d["source"], d["target"], d["kind"])
        if key not in best or priority.get(d["source_tag"], 99) < priority.get(best[key]["data"]["source_tag"], 99):
            best[key] = e

    # Only include nodes that have at least one edge
    used_ids: set[str] = set()
    for e in best.values():
        used_ids.add(e["data"]["source"])
        used_ids.add(e["data"]["target"])
    nodes = [n for n in node_map.values() if n["data"]["id"] in used_ids]

    return {"nodes": nodes, "edges": list(best.values())}
