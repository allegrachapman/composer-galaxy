# Composer Galaxy

An interactive force-directed graph that maps teacher–student relationships between classical composers across history. Search for any composer, explore their lineage, and find the shortest path connecting any two composers through chains of mentorship.

![Composer Galaxy](https://img.shields.io/badge/composers-388-c8a45a) ![Python](https://img.shields.io/badge/python-3.11+-blue) ![License](https://img.shields.io/badge/license-MIT-green)

## How it works

### Data pipeline

Each composer is stored as a Markdown file with YAML frontmatter in `composers/`. The seeding pipeline builds these files automatically:

1. **Wikipedia fetch** (`app/wikipedia.py`) — fetches the article text, parses infobox data (birth, death, teachers, students), and finds a portrait thumbnail
2. **LLM extraction** (`app/extract.py`) — sends the article prose to Claude Haiku, which returns structured JSON with teachers, students, influences, and verbatim supporting quotes
3. **Merge & write** (`app/seed.py`) — merges infobox and LLM results with a priority system (infobox > wiki > llm_confirmed > llm), deduplicates names (stripping Wikipedia disambiguation suffixes like "(composer)"), and writes the Markdown file

### Validation

LLM-sourced edges can be promoted to `llm_confirmed` via two independent checks (`app/validate.py`):

- **Cross-reference** — if composer A says B is a student (llm) AND composer B says A is a teacher (any source), the edge is confirmed
- **Wikidata** — checks Wikidata properties P1066 (student of) and P802 (student) against the LLM claims

### Graph builder

`app/graph.py` reads all composer Markdown files and builds a JSON graph with nodes and edges. It deduplicates edges using source priority (infobox > wiki > manual > llm_confirmed > llm) and normalizes disambiguation suffixes so "Donald Harris (composer)" and "Donald Harris" resolve to the same node.

### Frontend

A single-page D3.js app (`web/index.html`) renders the graph on an HTML canvas:

- **Force layout** — rank-based x-positioning by birth year, with charge repulsion and link forces. Stub (unseeded) nodes inherit position from their seeded neighbors and exert zero charge to avoid distorting the layout
- **Visual encoding** — gold solid lines = Wikipedia sourced, green solid lines = LLM confirmed, blue dashed lines = LLM sourced, cyan lines = active path
- **Pathfinding** — BFS shortest-path between any two composers, showing degrees of separation
- **Search** — diacritics-insensitive (type "dvorak" to find "Dvořák")
- **Detail panel** — click any composer to see their connections, source quotes, and links to Wikipedia with Text Fragment highlighting
- **Image proxy** — external (non-Wikimedia) thumbnails are proxied through the server to avoid canvas CORS restrictions

## Setup

```bash
# Clone and enter the project
git clone https://github.com/allegrachapman/composer-galaxy.git
cd composer-galaxy

# Create virtual environment and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Add your Anthropic API key (needed for seeding new composers)
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY
```

## Usage

### Run the server

```bash
.venv/bin/python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8742
```

Then open http://127.0.0.1:8742 in your browser.

### Seed new composers

```bash
# Seed specific composers
.venv/bin/python3 -m app.seed --names "Clara Schumann,Amy Beach"

# Seed a random batch from the candidate pool
.venv/bin/python3 -m app.seed --count 20
```

After seeding, restart the server to pick up the new data.

### Validate LLM edges

```bash
# Dry run — see what would be confirmed
.venv/bin/python3 -m app.validate

# Apply confirmations to composer files
.venv/bin/python3 -m app.validate --apply
```

## Project structure

```
composer-galaxy/
├── app/
│   ├── main.py           # FastAPI server (graph API, image proxy, static files)
│   ├── seed.py           # Seeding pipeline (fetch → extract → merge → write)
│   ├── wikipedia.py      # Wikipedia article fetcher and infobox parser
│   ├── extract.py        # Claude Haiku LLM extraction with verbatim quotes
│   ├── graph.py          # Builds deduplicated graph JSON from composer files
│   ├── validate.py       # Cross-reference and Wikidata validation
│   └── composers_pool.py # Candidate composer list for random seeding
├── composers/            # 388 composer Markdown files (YAML frontmatter + notes)
├── web/
│   └── index.html        # Single-page D3.js frontend
├── pyproject.toml
└── .env.example
```

## Composer file format

Each composer is a Markdown file with YAML frontmatter:

```yaml
---
name: César Franck
wikipedia: https://en.wikipedia.org/wiki/César_Franck
thumbnail: https://upload.wikimedia.org/...
birth: '1822'
birth_source: infobox
death: '1890'
death_source: infobox
era: Romantic
era_source: llm
teachers:
- name: Anton Reicha
  source: wiki
- name: Pierre-Joseph-Guillaume Zimmerman
  source: llm_confirmed
  quote: entered the Paris Conservatoire where he studied with Zimmerman
students:
- name: Vincent d'Indy
  source: wiki
---

# César Franck

[Wikipedia](https://en.wikipedia.org/wiki/César_Franck)

## Notes

_Add your own notes here. This section is preserved across re-seeds._
```

## Edge source hierarchy

| Priority | Source | Meaning | Visual |
|----------|--------|---------|--------|
| 0 | `infobox` | From Wikipedia infobox | Gold solid |
| 1 | `wiki` | From Wikipedia article links | Gold solid |
| 2 | `manual` | Hand-added | Gold solid |
| 3 | `llm_confirmed` | LLM claim verified by cross-reference or Wikidata | Green solid |
| 4 | `llm` | LLM extraction, unverified | Blue dashed |
