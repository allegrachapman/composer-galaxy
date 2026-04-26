"""LLM prose extractor: reads an article extract, returns relationships tagged source=llm."""

from __future__ import annotations

import json
import re
from typing import Any

from anthropic import Anthropic

MODEL = "claude-haiku-4-5"
MAX_PROSE_CHARS = 18000

SYSTEM_PROMPT = """You extract structured relationships about classical composers from biographical prose.

Return ONLY a JSON object with these exact keys:
{
  "birth": string | null,            // year only, e.g. "1770"
  "death": string | null,            // year only, or null if living
  "nationality": string | null,      // e.g. "German", "French-Polish"
  "era": string | null,              // e.g. "Baroque", "Romantic", "20th-century"
  "teachers": [{"name": string, "quote": string}],
  "students": [{"name": string, "quote": string}],
  "influenced_by": [{"name": string, "quote": string}],
  "influenced": [{"name": string, "quote": string}]
}

For each relationship, include a "quote" field that is a VERBATIM substring copied exactly from the article text (max ~20 words). Do not paraphrase — the quote must appear character-for-character in the input.

Rules:
- Only include relationships clearly stated in the text. Do not guess.
- Names must match the form a Wikipedia article would use (e.g. "Johann Sebastian Bach", not "Bach").
- Do not include the subject composer in any list.
- Do not include non-composers (performers, conductors, patrons) unless they also composed.
- Empty lists are fine. Return the JSON and nothing else."""


def extract_relationships(composer_name: str, prose: str) -> dict[str, Any]:
    """Call Claude to extract structured relationships from article prose.

    Returns a dict with the same shape as parse_infobox. Returns empty fields on failure.
    """
    client = Anthropic()
    prose = prose[:MAX_PROSE_CHARS]
    user_msg = f"Composer: {composer_name}\n\nArticle text:\n\n{prose}"

    msg = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = msg.content[0].text if msg.content else ""
    return _parse_json(text)


def _parse_json(text: str) -> dict[str, Any]:
    empty = {
        "birth": None, "death": None, "nationality": None, "era": None,
        "teachers": [], "students": [], "influenced_by": [], "influenced": [],
    }
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return empty
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return empty
    for k in empty:
        if k not in data:
            data[k] = empty[k]
    for k in ("teachers", "students", "influenced_by", "influenced"):
        if not isinstance(data[k], list):
            data[k] = []
        normalized = []
        for x in data[k]:
            if isinstance(x, dict) and "name" in x:
                name = str(x["name"]).strip()
                if name:
                    normalized.append({"name": name, "quote": str(x.get("quote", "")).strip()})
            elif isinstance(x, str) and x.strip():
                normalized.append({"name": x.strip(), "quote": ""})
        data[k] = normalized
    return data
