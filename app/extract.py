"""LLM prose extractor: reads an article extract, returns relationships tagged source=llm.

Set EXTRACT_PROVIDER=gemini in .env to use Gemini Flash instead of Claude Haiku.
Requires GEMINI_API_KEY for Gemini, ANTHROPIC_API_KEY for Claude.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

ANTHROPIC_MODEL = "claude-haiku-4-5"
GEMINI_MODEL = "gemini-2.5-pro"
GEMINI_FLASH_MODEL = "gemini-2.5-flash"
MAX_PROSE_CHARS = 30000

SYSTEM_PROMPT = """You extract structured relationships about classical composers from biographical prose.

Return ONLY a JSON object with these exact keys:
{
  "birth": string | null,            // year only, e.g. "1770"
  "death": string | null,            // year only, or null if living
  "nationality": string | null,      // e.g. "German", "French-Polish"
  "era": string | null,              // e.g. "Baroque", "Romantic", "20th-century"
  "teachers": [{"name": string, "quote": string}],
  "students": [{"name": string, "quote": string}],
  "mentors": [{"name": string, "quote": string}]
}

For each relationship, include a "quote" field that is a VERBATIM substring copied exactly from the article text (max ~20 words). Do not paraphrase — the quote must appear character-for-character in the input.

Rules:
- Include relationships that are clearly stated OR strongly implied in the text. For example, "his father taught him violin" counts as a teacher relationship even without naming a formal title. Hedging language like "probably", "likely", or "is believed to have" still counts — only exclude if the text explicitly says the relationship did NOT happen.
- A parent who provided musical training counts as a teacher.
- "teachers" and "students" mean people who gave or received INSTRUCTION — formal lessons, composition studies, conservatory classes, apprenticeships, tutoring, or direct musical training. Anyone described as a "tutor" counts as a teacher. Do NOT put mentors in teachers/students.
- "mentors" are people who significantly guided, advised, or championed the subject's musical career WITHOUT giving them formal instruction. This includes: advocates who promoted their work, senior composers who gave artistic guidance or feedback, and patrons who were themselves musicians. Do NOT include: people who merely admired or were friends with the subject, casual acquaintances, or people who only influenced the subject's style indirectly. The text should describe a direct, personal relationship — not just stylistic influence.
- Names must match the form a Wikipedia article would use (e.g. "Johann Sebastian Bach", not "Bach"). Only include people whose full name can be determined from the text. Skip entries like "his father", "a local teacher", or other descriptions that cannot be resolved to a specific person's name.
- Do not include the subject composer in any list.
- Include any person involved in music — composers, performers, instrumentalists, conductors, music teachers, and singers all count. Only exclude people with no musical role (patrons, nobles, politicians, etc.).
- If a sentence mentions multiple people in the same relationship (e.g. "his students included X, Y, and Z"), create a SEPARATE entry for EACH person. The same quote can be reused across entries. Never bundle multiple people into one entry.
- Be careful with nested clauses. "X's student Y, whose pupil was Z" means Z studied with Y, NOT with X. Only extract direct relationships with the subject composer.
- Empty lists are fine. Return the JSON and nothing else."""


def _use_gemini() -> bool:
    return os.environ.get("EXTRACT_PROVIDER", "").lower() == "gemini"


def _call_anthropic(user_msg: str) -> str:
    from anthropic import Anthropic
    client = Anthropic()
    msg = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    return msg.content[0].text if msg.content else ""


def _call_gemini(user_msg: str) -> str:
    import time
    from google import genai
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    for attempt in range(6):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=f"{SYSTEM_PROMPT}\n\n{user_msg}",
            )
            return response.text or ""
        except Exception as e:
            if "503" in str(e) or "429" in str(e) or "UNAVAILABLE" in str(e):
                wait = 15 * (attempt + 1)
                print(f"      Gemini {e.__class__.__name__}, retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("Gemini unavailable after 6 retries")


def extract_relationships(composer_name: str, prose: str) -> dict[str, Any]:
    """Call an LLM to extract structured relationships from article prose.

    Returns a dict with the same shape as parse_infobox. Returns empty fields on failure.
    """
    prose = prose[:MAX_PROSE_CHARS]
    user_msg = f"Composer: {composer_name}\n\nIMPORTANT: Extract relationships from ALL text sections below, including any supplementary sources. Do not skip any section.\n\nArticle text:\n\n{prose}"

    if _use_gemini():
        text = _call_gemini(user_msg)
    else:
        text = _call_anthropic(user_msg)
    return _parse_json(text)


def extract_relationships_flash(composer_name: str, prose: str) -> dict[str, Any]:
    """Tier 2: extract relationships from Wikipedia using Gemini Flash (cheap)."""
    prose = prose[:MAX_PROSE_CHARS]
    user_msg = (
        f"Composer: {composer_name}\n\n"
        f"IMPORTANT: Extract relationships from ALL text sections below, "
        f"including any supplementary sources. Do not skip any section.\n\n"
        f"Article text:\n\n{prose}"
    )
    text = _call_gemini_flash(SYSTEM_PROMPT, user_msg)
    return _parse_json(text)


QUOTE_PROMPT = """You are given a composer's Wikipedia article and a list of known relationships (teachers and students). For each person listed, find a VERBATIM quote from the article text (max ~20 words) that supports the relationship. If no supporting text exists in the article, return null for that person's quote.

Return ONLY a JSON object like:
{
  "quotes": {"Person Name": "verbatim quote from article" | null, ...}
}

The quote must appear character-for-character in the input article text. Do not paraphrase. Return the JSON and nothing else."""


def find_quotes(composer_name: str, prose: str, names: list[str]) -> dict[str, str | None]:
    if not names:
        return {}
    prose = prose[:MAX_PROSE_CHARS]
    names_str = "\n".join(f"- {n}" for n in names)
    user_msg = f"Composer: {composer_name}\n\nKnown relationships (find supporting quotes):\n{names_str}\n\nArticle text:\n\n{prose}"

    if _use_gemini():
        text = _call_gemini_with_prompt(QUOTE_PROMPT, user_msg)
    else:
        text = _call_anthropic_with_prompt(QUOTE_PROMPT, user_msg)

    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return data.get("quotes", {})


def find_quotes_flash(composer_name: str, prose: str, names: list[str]) -> dict[str, str | None]:
    """Tier 2: find supporting quotes using Gemini Flash (cheap)."""
    if not names:
        return {}
    prose = prose[:MAX_PROSE_CHARS]
    names_str = "\n".join(f"- {n}" for n in names)
    user_msg = f"Composer: {composer_name}\n\nKnown relationships (find supporting quotes):\n{names_str}\n\nArticle text:\n\n{prose}"
    text = _call_gemini_flash(QUOTE_PROMPT, user_msg)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return data.get("quotes", {})


def _call_anthropic_with_prompt(system: str, user_msg: str) -> str:
    from anthropic import Anthropic
    client = Anthropic()
    msg = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1500,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    return msg.content[0].text if msg.content else ""


def _call_gemini_with_prompt(system: str, user_msg: str) -> str:
    import time
    from google import genai
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    for attempt in range(4):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=f"{system}\n\n{user_msg}",
            )
            return response.text or ""
        except Exception as e:
            if "503" in str(e) or "429" in str(e) or "UNAVAILABLE" in str(e):
                wait = 10 * (attempt + 1)
                print(f"      Gemini {e.__class__.__name__}, retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("Gemini unavailable after 4 retries")


def _parse_json(text: str) -> dict[str, Any]:
    empty = {
        "birth": None, "death": None, "nationality": None, "era": None,
        "teachers": [], "students": [], "mentors": [],
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
    for k in ("teachers", "students", "mentors"):
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


def _call_gemini_flash(system: str, user_msg: str) -> str:
    import time
    from google import genai
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    for attempt in range(6):
        try:
            response = client.models.generate_content(
                model=GEMINI_FLASH_MODEL,
                contents=f"{system}\n\n{user_msg}",
            )
            return response.text or ""
        except Exception as e:
            if "503" in str(e) or "429" in str(e) or "UNAVAILABLE" in str(e):
                wait = 15 * (attempt + 1)
                print(f"      Flash {e.__class__.__name__}, retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("Gemini Flash unavailable after 6 retries")


def extract_grove(composer_name: str, grove_text: str) -> dict[str, Any]:
    """Extract relationships from a Grove/Oxford article using Gemini Flash.

    Returns the same shape as extract_relationships(). Tagged as source=grove by the caller.
    """
    grove_text = grove_text[:MAX_PROSE_CHARS]
    user_msg = (
        f"Composer: {composer_name}\n\n"
        f"This is from the Oxford/Grove Music Online article, a highly authoritative musicological source. "
        f"Extract all relationships carefully.\n\n"
        f"Article text:\n\n{grove_text}"
    )
    text = _call_gemini_flash(SYSTEM_PROMPT, user_msg)
    return _parse_json(text)


VERIFY_PROMPT = """You verify whether a claimed musical relationship is supported by article text.

For each claim, respond ONLY with a JSON object:
{
  "results": {
    "Person Name": true | false,
    ...
  }
}

Return true if the article text clearly supports the relationship (direct statement or strong implication). Return false if there is no supporting evidence, or the text contradicts the claim. Return the JSON and nothing else."""


def verify_edges_haiku(
    composer_name: str,
    prose: str,
    edges: list[dict[str, str]],
) -> dict[str, bool]:
    """Tier 3: ask Haiku to verify a batch of uncorroborated edges.

    Each edge dict has keys: name, relationship (teacher/student/mentor).
    Returns {name: True/False}.
    """
    if not edges:
        return {}
    prose = prose[:MAX_PROSE_CHARS]
    claims = "\n".join(
        f"- {e['name']} was a {e['relationship']} of {composer_name}"
        for e in edges
    )
    user_msg = (
        f"Composer: {composer_name}\n\n"
        f"Claims to verify:\n{claims}\n\n"
        f"Article text:\n\n{prose}"
    )
    text = _call_anthropic_with_prompt(VERIFY_PROMPT, user_msg)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return data.get("results", {})
