"""Load composer markdown files → Cytoscape-style nodes and edges JSON."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_DISAMBIG_RE = re.compile(r"\s*\((?:composer|musician|pianist|organist|violinist|singer|conductor|cellist|flautist|musicologist)\)$", re.IGNORECASE)

ROOT = Path(__file__).resolve().parent.parent
COMPOSERS_DIR = ROOT / "composers"

SOURCE_MAP = {
    "manual":   ("manual",    "human"),
    "infobox":  ("wikipedia", "parser"),
    "wiki":     ("wikipedia", "regex"),
    "llm":      ("wikipedia", "llm"),
    "grove":    ("grove",     "llm"),
    "wikidata": ("wikidata",  "api"),
}
BASE_CONFIDENCE = {
    "human": 0.95, "api": 0.90, "parser": 0.85, "regex": 0.85, "llm": 0.70,
}
MOD_QUOTE = 0.0

WOMEN_COMPOSERS = {
    "Adelina de Lara", "Agathe Backer Grøndahl", "Alma Mahler",
    "Amanda Röntgen-Maier", "Amélie-Julie Candeille", "Amy Beach",
    "Anna S. Þorvaldsdóttir", "Annette von Droste-Hülshoff", "Antonia Bembo",
    "Augusta Holmès", "Augusta Read Thomas", "Barbara Strozzi", "Betsy Jolas",
    "Teresa Carreño",
    "Bettina von Arnim", "Camilla de Rossi", "Caroline Shaw", "Chen Yi",
    "Chiara Margarita Cozzolani", "Clara Schumann", "Corona Schröter",
    "Cécile Chaminade", "Dora Pejačević", "Du Yun", "Eleanor Alberga", "Estela Cabezas Espinoza",
    "Elena Ruehr", "Elizabeth Maconchy", "Ellen Taaffe Zwilich", "Emilie Mayer",
    "Errollyn Wallen", "Ethel Smyth", "Fanny Davies", "Fanny Hensel",
    "Fanny Mendelssohn", "Florence Price", "Francesca Caccini",
    "Gabriela Lena Frank", "Galina Ustvolskaya", "Germaine Tailleferre",
    "Gloria Coates", "Grażyna Bacewicz", "Henriëtte Bosmans",
    "Hildegard of Bingen", "Hortense de Beauharnais", "Hélène de Montgeroult",
    "Hélène Liebmann", "Ilona Eibenschütz", "Ingeborg Bronsart von Schellendorf",
    "Isabelle Vengerova", "Isabella Leonarda", "Jessie Montgomery",
    "Joan Tower", "Josepha Barbara Auenbrugger", "Josephine Lang",
    "Judith Bingham", "Judith Weir", "Julia Perry", "Julia Wolfe",
    "Julie Guicciardi", "Kaija Saariaho", "Kassia", "Kim Jin-hi",
    "Leokadiya Kashperova", "Libby Larsen", "Lili Boulanger",
    "Louise Farrenc", "Margaret Bonds", "Maria Anna Mozart",
    "Maria Szymanowska", "Maria Theresia von Paradis", "Marianna Martines",
    "Marianna von Martines", "Marion Bauer", "Marta Ptaszynska",
    "Mathilde Kralik", "Mekhla Kumar", "Mel Bonis", "Meredith Monk",
    "Missy Mazzoli", "Morfydd Llwyn Owen", "Nadia Boulanger", "Nadia Judd",
    "Nina Simone", "Olga Neuwirth", "Pauline Duchambge", "Pauline Oliveros",
    "Pauline Viardot", "Peggy Glanville-Hicks", "Priaulx Rainier",
    "Rebecca Clarke", "Rebecca Saunders", "Rosa García Ascot",
    "Rosa Giacinta Badalla", "Ruth Crawford Seeger", "Sofia Gubaidulina",
    "Sophie Gail", "Tania León", "Thea Musgrave", "Unsuk Chin",
    "Valerie Coleman", "Violet Archer", "Vítězslava Kaprálová",
    "Younghi Pagh-Paan", "Élisabeth Jacquet de La Guerre",
}


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
                "woman": clean_name in WOMEN_COMPOSERS,
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

    def edge(src: str, tgt: str, kind: str, source: str, source_url: str | None = None, quote: str | None = None, file_owner: str | None = None, field: str | None = None, edge_name: str | None = None, corroborated_by: list | None = None, verified: bool = False):
        ensure_node(src)
        ensure_node(tgt)
        origin, method = SOURCE_MAP.get(source, ("unknown", "unknown"))
        if verified:
            conf = 0.95
        else:
            conf = BASE_CONFIDENCE.get(method, 0.50)
            if corroborated_by and len(corroborated_by) > 1:
                conf = min(conf + 0.10, 1.0)
        d = {
            "id": f"{_node_id(src)}__{kind}__{_node_id(tgt)}",
            "source": _node_id(src),
            "target": _node_id(tgt),
            "kind": kind,
            "source_tag": source,
            "source_url": source_url,
            "origin": origin,
            "method": method,
            "confidence": round(conf, 2),
            "verified": verified,
        }
        if quote:
            d["quote"] = quote
        if corroborated_by and len(corroborated_by) > 1:
            d["corroborated_by"] = corroborated_by
        if file_owner:
            d["file_owner"] = file_owner
            d["field"] = field
            d["edge_name"] = edge_name
        edges.append({"data": d})

    wiki_urls = {_node_id(rec["name"]): rec.get("wikipedia") for rec in records}

    for rec in records:
        name = rec["name"]
        wiki_url = rec.get("wikipedia")
        owner_id = _node_id(name)
        for t in rec.get("teachers") or []:
            if t.get("flagged"):
                continue
            url = t.get("source_url") or wiki_urls.get(_node_id(t["name"])) or wiki_url
            edge(t["name"], name, "teacher", t["source"], url, t.get("quote"), owner_id, "teachers", t["name"], t.get("corroborated_by"), t.get("verified", False))
        for s in rec.get("students") or []:
            if s.get("flagged"):
                continue
            url = s.get("source_url") or wiki_urls.get(_node_id(s["name"])) or wiki_url
            edge(name, s["name"], "teacher", s["source"], url, s.get("quote"), owner_id, "students", s["name"], s.get("corroborated_by"), s.get("verified", False))
        for m in rec.get("mentors") or []:
            if m.get("flagged"):
                continue
            url = m.get("source_url") or wiki_urls.get(_node_id(m["name"])) or wiki_url
            edge(m["name"], name, "mentor", m["source"], url, m.get("quote"), owner_id, "mentors", m["name"], m.get("corroborated_by"), m.get("verified", False))

    # Dedupe edges (same src/tgt/kind): prefer version with a quote, then by source priority
    priority = {"infobox": 0, "wiki": 1, "manual": 2, "llm": 3}
    best: dict[tuple, dict] = {}
    for e in edges:
        d = e["data"]
        key = (d["source"], d["target"], d["kind"])
        if key not in best:
            best[key] = e
        else:
            old = best[key]["data"]
            new_has_quote = bool(d.get("quote"))
            old_has_quote = bool(old.get("quote"))
            if new_has_quote and not old_has_quote:
                best[key] = e
            elif not new_has_quote and old_has_quote:
                pass
            elif priority.get(d["source_tag"], 99) < priority.get(old["source_tag"], 99):
                best[key] = e

    # Only include nodes that have at least one edge
    used_ids: set[str] = set()
    for e in best.values():
        used_ids.add(e["data"]["source"])
        used_ids.add(e["data"]["target"])
    nodes = [n for n in node_map.values() if n["data"]["id"] in used_ids]

    return {"nodes": nodes, "edges": list(best.values())}
