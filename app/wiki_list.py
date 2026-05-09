"""Scrape Wikipedia's 'List of music students by teacher' pages into a lookup table.

Returns teacher→students and student→teachers dicts, plus source URLs for each edge.
No LLM calls — pure HTML/wikitext parsing.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

_PAGES = [
    "List_of_music_students_by_teacher:_A_to_B",
    "List_of_music_students_by_teacher:_C_to_F",
    "List_of_music_students_by_teacher:_G_to_J",
    "List_of_music_students_by_teacher:_K_to_M",
    "List_of_music_students_by_teacher:_N_to_Q",
    "List_of_music_students_by_teacher:_R_to_S",
    "List_of_music_students_by_teacher:_T_to_Z",
]

_TEACHER_RE = re.compile(r'===\[\[([^\]|]+)(?:\|[^\]]+)?\]\]===')
_STUDENT_RE = re.compile(r'\{\{LMSTA\|([^|}]+)\|([^|}]+)')
_HEADERS = {"User-Agent": "ComposerGalaxy/1.0 (research project)"}
_API = "https://en.wikipedia.org/w/api.php"


def _page_url(page_title: str) -> str:
    return f"https://en.wikipedia.org/wiki/{page_title}"


def _parse_page(wikitext: str, page_title: str) -> list[tuple[str, str, str]]:
    """Parse one page's wikitext, return list of (teacher, student, source_url)."""
    url = _page_url(page_title)
    current_teacher = None
    edges = []

    for line in wikitext.split("\n"):
        tm = _TEACHER_RE.search(line)
        if tm:
            current_teacher = tm.group(1).strip()
            continue
        if current_teacher:
            sm = _STUDENT_RE.search(line)
            if sm:
                first = sm.group(1).strip()
                last = sm.group(2).strip()
                if "link=" in last:
                    last = last.split("|")[0]
                student = f"{first} {last}"
                edges.append((current_teacher, student, url))

    return edges


def fetch_all() -> dict[str, Any]:
    """Fetch all 7 pages and return structured lookup data.

    Returns:
        {
            "by_teacher": {teacher_name: [student_name, ...]},
            "by_student": {student_name: [teacher_name, ...]},
            "source_url": {(teacher, student): url},
        }
    """
    by_teacher: dict[str, list[str]] = {}
    by_student: dict[str, list[str]] = {}
    source_url: dict[tuple[str, str], str] = {}

    client = httpx.Client(timeout=30.0, headers=_HEADERS)
    try:
        for i, page in enumerate(_PAGES):
            if i > 0:
                time.sleep(2)
            params = {
                "action": "parse",
                "page": page,
                "prop": "wikitext",
                "format": "json",
            }
            for attempt in range(5):
                r = client.get(_API, params=params)
                if r.status_code == 429:
                    wait = 5 * (attempt + 1)
                    print(f"  Rate limited on {page}, waiting {wait}s...")
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                break
            else:
                raise RuntimeError(f"Failed to fetch {page} after 5 retries")
            wikitext = r.json()["parse"]["wikitext"]["*"]
            print(f"  Parsed {page}")

            for teacher, student, url in _parse_page(wikitext, page):
                by_teacher.setdefault(teacher, []).append(student)
                by_student.setdefault(student, []).append(teacher)
                source_url[(teacher, student)] = url
    finally:
        client.close()

    return {
        "by_teacher": by_teacher,
        "by_student": by_student,
        "source_url": source_url,
    }


if __name__ == "__main__":
    data = fetch_all()
    teachers = len(data["by_teacher"])
    edges = sum(len(v) for v in data["by_teacher"].values())
    print(f"Teachers: {teachers}")
    print(f"Total teacher→student edges: {edges}")
    print(f"Unique students: {len(data['by_student'])}")
