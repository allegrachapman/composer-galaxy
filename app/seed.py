"""Seed script: sample composers, fetch Wikipedia, extract relationships, write markdown.

Usage:
    python -m app.seed                  # default: 10 composers, top-200 pool
    python -m app.seed --count 20
    python -m app.seed --names "Bach,Beethoven"   # skip sampling, use explicit names
    python -m app.seed --no-llm --count 100       # LLM-free pass (zero tokens)
    python -m app.seed --no-llm --before 1850 --count 100  # pre-1850 composers, no LLM
    python -m app.seed --report         # show composers with thin data
"""

from __future__ import annotations

import argparse
import asyncio
import random
import re
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv

load_dotenv()

from . import extract, grove, wikipedia  # noqa: E402
from .composers_pool import CANDIDATE_COMPOSERS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
COMPOSERS_DIR = ROOT / "composers"

_FETCH_HEADERS = {"User-Agent": "ComposerGalaxy/1.0 (research project)"}


async def _fetch_source_text(url: str) -> str:
    """Fetch a URL and return plain text content (HTML tags stripped)."""
    async with httpx.AsyncClient(timeout=20.0, headers=_FETCH_HEADERS, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
    html = r.text
    # Try to extract just the main content area
    for tag in (r"<article[^>]*>(.*?)</article>", r"<main[^>]*>(.*?)</main>",
                r'<div[^>]*class="[^"]*content[^"]*"[^>]*>(.*?)</div>'):
        m = re.search(tag, html, re.DOTALL | re.IGNORECASE)
        if m and len(m.group(1)) > 200:
            html = m.group(1)
            break
    text = re.sub(r"<nav[^>]*>.*?</nav>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<header[^>]*>.*?</header>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<footer[^>]*>.*?</footer>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:15000]


def _load_existing_manual(name: str) -> dict:
    """Load manual-source edges and era from an existing composer file."""
    slug = _slug(name)
    path = COMPOSERS_DIR / f"{slug}.md"
    result = {"teachers": [], "students": [], "mentors": [], "era": None, "era_source": None}
    if not path.exists():
        return result
    text = path.read_text()
    if not text.startswith("---"):
        return result
    parts = text.split("---", 2)
    if len(parts) < 3:
        return result
    try:
        rec = yaml.safe_load(parts[1])
    except Exception:
        return result
    if not rec:
        return result
    for field in ("teachers", "students", "mentors"):
        for entry in rec.get(field) or []:
            if entry.get("source") == "manual":
                result[field].append(entry)
    if rec.get("era_source") == "manual":
        result["era"] = rec.get("era")
        result["era_source"] = "manual"
    return result


def _load_existing_sources(name: str) -> list[str]:
    """Load the sources list from an existing composer file, if any."""
    slug = _slug(name)
    path = COMPOSERS_DIR / f"{slug}.md"
    if not path.exists():
        return []
    text = path.read_text()
    if not text.startswith("---"):
        return []
    parts = text.split("---", 2)
    if len(parts) < 3:
        return []
    try:
        rec = yaml.safe_load(parts[1])
    except Exception:
        return []
    return rec.get("sources") or []


def _slug(name: str) -> str:
    s = name.lower()
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


_SOURCE_PRIORITY = {"manual": -1, "infobox": 0, "grove": 0, "wiki": 1, "llm": 2}

_DISAMBIG_RE = re.compile(r"\s*\((?:composer|musician|pianist|organist|violinist|singer|conductor|cellist|flautist|musicologist)\)$", re.IGNORECASE)


def _normalize_name(name: str) -> str:
    return _DISAMBIG_RE.sub("", name).strip()


def _merge_edges(
    *source_lists: tuple[list, str],
) -> list[dict[str, str]]:
    """Merge edge lists from multiple sources. Lower priority number wins.

    Each source list can contain plain strings or {"name": ..., "quote": ...} dicts.
    Names with Wikipedia disambiguation suffixes like "(composer)" are matched
    against their bare form; the higher-priority version's name is kept.

    Wiki-sourced (prose regex) edges that the LLM did not independently find are
    dropped — the regex can match names mentioned near relationship keywords without
    the name actually being in that relationship.
    """
    # First pass: collect all names per source tag
    by_tag: dict[str, set[str]] = {}
    for vals, tag in source_lists:
        keys = set()
        for v in vals:
            name = v.get("name", "").strip() if isinstance(v, dict) else str(v).strip()
            if name:
                keys.add(_normalize_name(name))
        by_tag[tag] = keys

    corroborated = by_tag.get("llm", set()) | by_tag.get("manual", set()) | by_tag.get("infobox", set()) | by_tag.get("grove", set())

    # Track which distinct sources found each edge
    found_by: dict[str, set[str]] = {}
    for vals, tag in source_lists:
        for v in vals:
            name = v.get("name", "").strip() if isinstance(v, dict) else str(v).strip()
            if name:
                found_by.setdefault(_normalize_name(name), set()).add(tag)

    seen: dict[str, dict] = {}
    for vals, tag in source_lists:
        for v in vals:
            if isinstance(v, dict):
                name = v.get("name", "").strip()
                quote = v.get("quote", "").strip()
                source_url = v.get("source_url", "").strip()
            else:
                name = str(v).strip()
                quote = ""
                source_url = ""
            if not name:
                continue
            key = _normalize_name(name)
            # Drop wiki prose edges not corroborated by LLM (but keep wiki-list edges)
            if tag == "wiki" and not source_url and key not in corroborated:
                continue
            if key not in seen or _SOURCE_PRIORITY[tag] < _SOURCE_PRIORITY[seen[key]["source"]]:
                entry = {"name": name, "source": tag}
                if quote:
                    entry["quote"] = quote
                if source_url:
                    entry["source_url"] = source_url
                sources = found_by.get(key, {tag})
                if len(sources) > 1:
                    entry["corroborated_by"] = sorted(sources)
                seen[key] = entry
    return list(seen.values())


def _pick_scalar(*sources: tuple[str | None, str]) -> tuple[str | None, str | None]:
    """Return (value, source_tag) from the first non-empty source."""
    for val, tag in sources:
        if val:
            return val, tag
    return None, None


def _parse_year(val) -> int | None:
    if not val:
        return None
    m = re.search(r"\d{3,4}", str(val))
    return int(m.group()) if m else None


def _load_dates_index() -> dict[str, tuple[int | None, int | None]]:
    """Load birth/death years for all existing composers."""
    index: dict[str, tuple[int | None, int | None]] = {}
    for path in COMPOSERS_DIR.glob("*.md"):
        text = path.read_text()
        if not text.startswith("---"):
            continue
        parts = text.split("---", 2)
        if len(parts) < 3:
            continue
        try:
            meta = yaml.safe_load(parts[1])
        except Exception:
            continue
        if not meta:
            continue
        name = _normalize_name(meta.get("name", ""))
        birth = _parse_year(meta.get("birth"))
        death = _parse_year(meta.get("death"))
        index[name] = (birth, death)
    return index


def _qa_filter(record: dict) -> dict:
    """Remove edges that fail sanity checks: impossible timelines and self-loops."""
    dates_index = _load_dates_index()
    my_birth = _parse_year(record.get("birth"))
    my_death = _parse_year(record.get("death"))
    my_name = _normalize_name(record.get("name", ""))

    teacher_map = {_normalize_name(t["name"]): t for t in record.get("teachers") or []}
    student_map = {_normalize_name(s["name"]): s for s in record.get("students") or []}

    dropped = []

    for field in ("teachers", "students"):
        cleaned = []
        for entry in record.get(field) or []:
            if entry.get("source") == "manual":
                cleaned.append(entry)
                continue
            name = _normalize_name(entry["name"])

            # Skip self-references
            if name == my_name:
                dropped.append(f"    ✂ {field}: {entry['name']} (self-reference)")
                continue

            # Check teacher-student loops: same person in both lists.
            # Only drop if this side has no quote (low confidence).
            # Keep both if both have quotes — legitimate bidirectional relationships exist.
            if field == "teachers" and name in student_map:
                other = student_map[name]
                if not entry.get("quote") and other.get("quote"):
                    dropped.append(f"    ✂ {field}: {entry['name']} (also listed as student, no supporting quote)")
                    continue
            if field == "students" and name in teacher_map:
                other = teacher_map[name]
                if not entry.get("quote") and other.get("quote"):
                    dropped.append(f"    ✂ {field}: {entry['name']} (also listed as teacher, no supporting quote)")
                    continue

            # Check impossible timelines against known dates
            other = dates_index.get(name)
            if other:
                other_birth, other_death = other
                if field == "teachers":
                    # Teacher must have been alive when student was born
                    if my_birth and other_death and my_birth > other_death:
                        dropped.append(f"    ✂ {field}: {entry['name']} (died {other_death}, student born {my_birth})")
                        continue
                    # Teacher must have been born before student died
                    if my_death and other_birth and other_birth > my_death:
                        dropped.append(f"    ✂ {field}: {entry['name']} (born {other_birth}, student died {my_death})")
                        continue
                else:  # students
                    # Student must have been alive when teacher was alive
                    if other_birth and my_death and other_birth > my_death:
                        dropped.append(f"    ✂ {field}: {entry['name']} (born {other_birth}, teacher died {my_death})")
                        continue
                    if my_birth and other_death and my_birth > other_death:
                        dropped.append(f"    ✂ {field}: {entry['name']} (died {other_death}, teacher born {my_birth})")
                        continue
                    # Student born well before teacher is suspicious
                    if other_birth and my_birth and other_birth < my_birth - 10:
                        dropped.append(f"    ✂ {field}: {entry['name']} (born {other_birth}, before teacher born {my_birth})")
                        continue

            cleaned.append(entry)
        record[field] = cleaned

    if dropped:
        for msg in dropped:
            print(msg)

    return record


async def process_composer(title: str, wiki_list_data: dict | None = None, no_llm: bool = False, hybrid: bool = False) -> dict:
    print(f"  → fetching {title}...")
    article = await wikipedia.fetch_article(title)
    canonical = article["title"]

    print(f"    parsing infobox...")
    ibox = wikipedia.parse_infobox(article["wikitext"])

    print(f"    scanning article prose...")
    prose = wikipedia.parse_prose_relationships(article["wikitext"])

    extra_sources = _load_existing_sources(title) or _load_existing_sources(canonical if 'canonical' in dir() else title)

    if no_llm:
        llm = {"birth": None, "death": None, "nationality": None, "era": None,
               "teachers": [], "students": [], "mentors": []}
    else:
        extra_text = ""
        if extra_sources:
            print(f"    fetching {len(extra_sources)} extra source(s)...")
            for url in extra_sources:
                try:
                    src_text = await _fetch_source_text(url)
                    extra_text += f"\n\n--- Source: {url} ---\n\n{src_text}"
                    print(f"      ✓ {url[:60]}...")
                except Exception as e:
                    print(f"      ✗ {url[:60]}: {e}")

        combined_prose = extra_text + "\n\n--- Main Wikipedia article ---\n\n" + article["extract"] if extra_text else article["extract"]
        if hybrid:
            print(f"    Tier 2: Flash extraction (Wikipedia)...")
            llm = await asyncio.to_thread(
                extract.extract_relationships_flash, canonical, combined_prose
            )
        else:
            print(f"    LLM extraction...")
            llm = await asyncio.to_thread(
                extract.extract_relationships, canonical, combined_prose
            )

    # Build wiki-list edges for this composer
    wl_teachers: list[dict] = []
    wl_students: list[dict] = []
    if wiki_list_data:
        norm = _normalize_name(canonical)
        by_teacher = wiki_list_data["by_teacher"]
        by_student = wiki_list_data["by_student"]
        source_url = wiki_list_data["source_url"]
        # Students of this composer (this composer is the teacher)
        for key in by_teacher:
            if _normalize_name(key) == norm:
                for student in by_teacher[key]:
                    url = source_url.get((key, student), "")
                    wl_students.append({"name": student, "source": "wiki", "source_url": url})
                break
        # Teachers of this composer (this composer is the student)
        for key in by_student:
            if _normalize_name(key) == norm:
                for teacher in by_student[key]:
                    url = source_url.get((teacher, key), "")
                    wl_teachers.append({"name": teacher, "source": "wiki", "source_url": url})
                break
        if wl_teachers or wl_students:
            print(f"    wiki-list: +{len(wl_teachers)} teachers, +{len(wl_students)} students")

    # Tier 1.5: Grove/Oxford extraction via Gemini Flash
    grove_extract = {"birth": None, "death": None, "nationality": None, "era": None,
                     "teachers": [], "students": [], "mentors": []}
    grove_text = grove.lookup(canonical) or grove.lookup(title)
    if grove_text:
        if no_llm:
            print(f"    Grove article found ({len(grove_text)} chars) — skipped (no-llm mode)")
        else:
            print(f"    Grove article found ({len(grove_text)} chars), extracting with Flash...")
            grove_extract = await asyncio.to_thread(
                extract.extract_grove, canonical, grove_text
            )
            g_t = len(grove_extract.get("teachers", []))
            g_s = len(grove_extract.get("students", []))
            g_m = len(grove_extract.get("mentors", []))
            print(f"    grove: +{g_t} teachers, +{g_s} students, +{g_m} mentors")

    # Load manual edges and era from existing file
    manual = _load_existing_manual(title)
    if not manual.get("teachers") and title != canonical:
        manual = _load_existing_manual(canonical)

    birth, birth_src = _pick_scalar((ibox["birth"], "infobox"), (grove_extract["birth"], "grove"), (llm["birth"], "llm"))
    death, death_src = _pick_scalar((ibox["death"], "infobox"), (grove_extract["death"], "grove"), (llm["death"], "llm"))
    nat, nat_src = _pick_scalar((ibox["nationality"], "infobox"), (grove_extract["nationality"], "grove"), (llm["nationality"], "llm"))
    era, era_src = _pick_scalar((ibox["era"], "infobox"), (grove_extract["era"], "grove"), (llm["era"], "llm"))
    if manual.get("era_source") == "manual":
        era, era_src = manual["era"], "manual"

    teachers = _merge_edges(
        (manual["teachers"], "manual"),
        (ibox["teachers"], "infobox"), (grove_extract["teachers"], "grove"),
        (prose["teachers"], "wiki"), (wl_teachers, "wiki"), (llm["teachers"], "llm"),
    )
    students = _merge_edges(
        (manual["students"], "manual"),
        (ibox["students"], "infobox"), (grove_extract["students"], "grove"),
        (prose["students"], "wiki"), (wl_students, "wiki"), (llm["students"], "llm"),
    )
    mentors = _merge_edges(
        (manual["mentors"], "manual"),
        ([], "infobox"), (grove_extract["mentors"], "grove"),
        ([], "wiki"), (llm["mentors"], "llm"),
    )

    if not no_llm:
        quoteless = [e["name"] for e in teachers + students + mentors if not e.get("quote")]
        if quoteless:
            quote_fn = extract.find_quotes_flash if hybrid else extract.find_quotes
            print(f"    finding quotes for {len(quoteless)} edges{' (Flash)' if hybrid else ''}...")
            quotes = await asyncio.to_thread(
                quote_fn, canonical, article["extract"], quoteless
            )
            for e in teachers + students + mentors:
                if not e.get("quote") and quotes.get(e["name"]):
                    e["quote"] = quotes[e["name"]]

    # Tier 3: Haiku verification of uncorroborated Flash edges
    if hybrid and not no_llm:
        uncorroborated = []
        for field, edges in [("teacher", teachers), ("student", students), ("mentor", mentors)]:
            for e in edges:
                if e.get("source") == "llm":
                    uncorroborated.append({"name": e["name"], "relationship": field, "_field": field})
        if uncorroborated:
            print(f"    Tier 3: Haiku verifying {len(uncorroborated)} uncorroborated edges...")
            verdicts = await asyncio.to_thread(
                extract.verify_edges_haiku, canonical, article["extract"], uncorroborated
            )
            rejected = {name for name, ok in verdicts.items() if not ok}
            if rejected:
                teachers = [e for e in teachers if not (e.get("source") == "llm" and e["name"] in rejected)]
                students = [e for e in students if not (e.get("source") == "llm" and e["name"] in rejected)]
                mentors = [e for e in mentors if not (e.get("source") == "llm" and e["name"] in rejected)]
                print(f"    Haiku rejected {len(rejected)}: {', '.join(sorted(rejected))}")
            else:
                print(f"    Haiku confirmed all {len(uncorroborated)} edges")

    record = {
        "name": canonical,
        "wikipedia": f"https://en.wikipedia.org/wiki/{canonical.replace(' ', '_')}",
        "thumbnail": article.get("thumbnail"),
        "birth": birth,
        "birth_source": birth_src,
        "death": death,
        "death_source": death_src,
        "nationality": nat,
        "nationality_source": nat_src,
        "era": era,
        "era_source": era_src,
        "teachers": teachers,
        "students": students,
        "mentors": mentors,
    }
    if extra_sources:
        record["sources"] = extra_sources
    return _qa_filter(record)


def write_markdown(record: dict) -> Path:
    COMPOSERS_DIR.mkdir(exist_ok=True)
    path = COMPOSERS_DIR / f"{_slug(record['name'])}.md"
    body = [
        f"# {record['name']}",
        "",
        f"[Wikipedia]({record['wikipedia']})",
        "",
        "## Notes",
        "",
        "_Add your own notes here. This section is preserved across re-seeds._",
        "",
    ]

    # Preserve existing Notes section and sources if the file exists
    if path.exists():
        existing = path.read_text()
        if not record.get("sources") and existing.startswith("---"):
            parts = existing.split("---", 2)
            if len(parts) >= 3:
                try:
                    old_rec = yaml.safe_load(parts[1])
                    if old_rec and old_rec.get("sources"):
                        record["sources"] = old_rec["sources"]
                except Exception:
                    pass
        m = re.search(r"## Notes\s*\n(.*)", existing, re.DOTALL)
        if m:
            body = [
                f"# {record['name']}",
                "",
                f"[Wikipedia]({record['wikipedia']})",
                "",
                "## Notes",
                "",
                m.group(1).strip(),
                "",
            ]

    fm = yaml.safe_dump(record, sort_keys=False, allow_unicode=True)
    path.write_text(f"---\n{fm}---\n\n" + "\n".join(body))
    return path


def qa_all():
    """Post-seed QA pass: re-check every composer file with the full dates index."""
    dates_index = _load_dates_index()
    total_dropped = 0
    for path in sorted(COMPOSERS_DIR.glob("*.md")):
        text = path.read_text()
        if not text.startswith("---"):
            continue
        parts = text.split("---", 2)
        if len(parts) < 3:
            continue
        rec = yaml.safe_load(parts[1])
        if not rec:
            continue

        my_name = _normalize_name(rec.get("name", ""))
        my_birth = _parse_year(rec.get("birth"))
        my_death = _parse_year(rec.get("death"))
        teacher_map = {_normalize_name(t["name"]): t for t in rec.get("teachers") or []}
        student_map = {_normalize_name(s["name"]): s for s in rec.get("students") or []}
        changed = False

        for field in ("teachers", "students", "mentors"):
            cleaned = []
            for entry in rec.get(field) or []:
                name = _normalize_name(entry["name"])

                # Never drop manually curated edges
                if entry.get("source") == "manual":
                    cleaned.append(entry)
                    continue

                if name == my_name:
                    print(f"  ✂ {my_name} → {field}: {entry['name']} (self-reference)")
                    changed = True
                    total_dropped += 1
                    continue

                if field == "teachers" and name in student_map:
                    other = student_map[name]
                    if not entry.get("quote") and other.get("quote"):
                        print(f"  ✂ {my_name} → {field}: {entry['name']} (also student, no quote)")
                        changed = True
                        total_dropped += 1
                        continue
                if field == "students" and name in teacher_map:
                    other = teacher_map[name]
                    if not entry.get("quote") and other.get("quote"):
                        print(f"  ✂ {my_name} → {field}: {entry['name']} (also teacher, no quote)")
                        changed = True
                        total_dropped += 1
                        continue

                other = dates_index.get(name)
                if other:
                    other_birth, other_death = other
                    drop = False
                    if field in ("teachers", "mentors"):
                        if my_birth and other_death and my_birth > other_death:
                            print(f"  ✂ {my_name} → {field}: {entry['name']} (died {other_death}, student born {my_birth})")
                            drop = True
                        elif my_death and other_birth and other_birth > my_death:
                            print(f"  ✂ {my_name} → {field}: {entry['name']} (born {other_birth}, student died {my_death})")
                            drop = True
                    else:  # students
                        if other_birth and my_death and other_birth > my_death:
                            print(f"  ✂ {my_name} → {field}: {entry['name']} (born {other_birth}, teacher died {my_death})")
                            drop = True
                        elif my_birth and other_death and my_birth > other_death:
                            print(f"  ✂ {my_name} → {field}: {entry['name']} (died {other_death}, teacher born {my_birth})")
                            drop = True
                        elif other_birth and my_birth and other_birth < my_birth - 10:
                            print(f"  ✂ {my_name} → {field}: {entry['name']} (born {other_birth}, before teacher born {my_birth})")
                            drop = True
                    if drop:
                        changed = True
                        total_dropped += 1
                        continue

                cleaned.append(entry)
            rec[field] = cleaned

        if changed:
            new_yaml = yaml.safe_dump(rec, sort_keys=False, allow_unicode=True)
            path.write_text(f"---\n{new_yaml}---\n{parts[2]}")
            print(f"  ↻ rewrote {path.name}")

    print(f"\nQA pass complete. Dropped {total_dropped} edges.")


def crossref_all(wiki_list_data: dict | None = None):
    """Retroactive pass: add wiki-list edges to existing composer files."""
    if not wiki_list_data:
        from . import wiki_list
        print("Fetching Wikipedia students-by-teacher lists...")
        wiki_list_data = wiki_list.fetch_all()

    by_teacher = wiki_list_data["by_teacher"]
    by_student = wiki_list_data["by_student"]
    source_url = wiki_list_data["source_url"]
    total_added = 0

    for path in sorted(COMPOSERS_DIR.glob("*.md")):
        text = path.read_text()
        if not text.startswith("---"):
            continue
        parts = text.split("---", 2)
        if len(parts) < 3:
            continue
        rec = yaml.safe_load(parts[1])
        if not rec:
            continue

        name = rec.get("name", "")
        norm = _normalize_name(name)
        changed = False

        # Existing edge names for dedup
        existing_teachers = {_normalize_name(e["name"]) for e in rec.get("teachers") or []}
        existing_students = {_normalize_name(e["name"]) for e in rec.get("students") or []}

        # Find this composer as teacher in wiki list
        for key in by_teacher:
            if _normalize_name(key) == norm:
                for student in by_teacher[key]:
                    if _normalize_name(student) not in existing_students:
                        url = source_url.get((key, student), "")
                        entry = {"name": student, "source": "wiki"}
                        if url:
                            entry["source_url"] = url
                        rec.setdefault("students", []).append(entry)
                        existing_students.add(_normalize_name(student))
                        changed = True
                        total_added += 1
                        print(f"  + {name} → student: {student}")
                break

        # Find this composer as student in wiki list
        for key in by_student:
            if _normalize_name(key) == norm:
                for teacher in by_student[key]:
                    if _normalize_name(teacher) not in existing_teachers:
                        url = source_url.get((teacher, key), "")
                        entry = {"name": teacher, "source": "wiki"}
                        if url:
                            entry["source_url"] = url
                        rec.setdefault("teachers", []).append(entry)
                        existing_teachers.add(_normalize_name(teacher))
                        changed = True
                        total_added += 1
                        print(f"  + {name} → teacher: {teacher}")
                break

        # Also convert any llm_confirmed → llm while we're here
        for field in ("teachers", "students", "mentors"):
            for entry in rec.get(field) or []:
                if entry.get("source") == "llm_confirmed":
                    entry["source"] = "llm"
                    changed = True

        if changed:
            new_yaml = yaml.safe_dump(rec, sort_keys=False, allow_unicode=True)
            path.write_text(f"---\n{new_yaml}---\n{parts[2]}")

    print(f"\nCross-reference complete. Added {total_added} edges.")


async def main(count: int, names: list[str] | None, no_llm: bool = False, before: int | None = None, hybrid: bool = False):
    from . import wiki_list
    print("Fetching Wikipedia students-by-teacher lists...")
    wiki_list_data = wiki_list.fetch_all()
    print(f"  {len(wiki_list_data['by_teacher'])} teachers, "
          f"{sum(len(v) for v in wiki_list_data['by_teacher'].values())} edges loaded")

    if no_llm:
        print("  ⚡ LLM-free mode: skipping all LLM calls (zero tokens)")
    elif hybrid:
        print("  🔀 Hybrid mode: Flash for extraction, Haiku for verification")

    if names:
        chosen = names
        print(f"Using {len(chosen)} explicit composers.")
    else:
        pool_size = len(CANDIDATE_COMPOSERS) if before else 200
        print(f"Ranking {len(CANDIDATE_COMPOSERS)} candidates by pageviews...")
        top = await wikipedia.top_by_pageviews(CANDIDATE_COMPOSERS, limit=pool_size)
        pool = [t for t, _ in top]

        if before:
            print(f"Filtering for composers born before {before}...")
            filtered = []
            for title in pool:
                art = await wikipedia.fetch_article(title)
                ibox = wikipedia.parse_infobox(art["wikitext"])
                birth = _parse_year(ibox.get("birth"))
                if birth and birth < before:
                    filtered.append(title)
                elif not birth:
                    filtered.append(title)
                await asyncio.sleep(0.3)
            pool = filtered
            print(f"  {len(pool)} composers pass the filter.")

        print(f"Sampling {count} from pool of {len(pool)}.")
        chosen = random.sample(pool, min(count, len(pool)))

    print(f"\nChosen:")
    for c in chosen:
        print(f"  - {c}")
    print()

    if not names:
        print("Pausing before fetching (rate-limit cooldown)...")
        await asyncio.sleep(10)

    for i, c in enumerate(chosen):
        try:
            record = await process_composer(c, wiki_list_data, no_llm=no_llm, hybrid=hybrid)
            path = write_markdown(record)
            print(f"  ✓ wrote {path.name}")
        except Exception as e:
            print(f"  ✗ {c}: {e!r}")
        if i < len(chosen) - 1:
            await asyncio.sleep(1.5)

    print(f"\nDone. {len(list(COMPOSERS_DIR.glob('*.md')))} composer files in {COMPOSERS_DIR}/")

    print("\nRunning post-seed QA pass...")
    qa_all()


def report_thin():
    """Report composers with few or no teachers/students — candidates for extra sources."""
    entries = []
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
        name = rec.get("name", path.stem)
        n_teachers = len(rec.get("teachers") or [])
        n_students = len(rec.get("students") or [])
        total = n_teachers + n_students
        has_sources = bool(rec.get("sources"))
        entries.append((total, n_teachers, n_students, name, has_sources))

    entries.sort()
    print(f"{'Composer':<45} {'Teachers':>8} {'Students':>8} {'Total':>6}  Sources")
    print("-" * 85)
    for total, nt, ns, name, has_src in entries:
        src_flag = "  ✓" if has_src else ""
        print(f"{name:<45} {nt:>8} {ns:>8} {total:>6}{src_flag}")
    print(f"\n{len(entries)} composers total.")
    no_edges = sum(1 for t, *_ in entries if t == 0)
    thin = sum(1 for t, *_ in entries if 0 < t <= 2)
    print(f"{no_edges} with NO teachers or students.")
    print(f"{thin} with only 1-2 teachers/students.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--names", type=str, default=None,
                    help="Comma-separated explicit composer titles (skip sampling)")
    ap.add_argument("--qa-only", action="store_true",
                    help="Skip seeding, just run the QA pass on existing files")
    ap.add_argument("--crossref", action="store_true",
                    help="Cross-reference existing files against Wikipedia student lists, then QA")
    ap.add_argument("--no-llm", action="store_true",
                    help="Skip all LLM calls (zero tokens); use only Wikipedia + manual data")
    ap.add_argument("--before", type=int, default=None,
                    help="Only seed composers born before this year (e.g. --before 1850)")
    ap.add_argument("--hybrid", action="store_true",
                    help="Hybrid pipeline: Gemini Flash for extraction, Haiku for verification")
    ap.add_argument("--report", action="store_true",
                    help="Show composers with thin teacher/student data")
    args = ap.parse_args()
    if args.report:
        report_thin()
    elif args.crossref:
        crossref_all()
        qa_all()
    elif args.qa_only:
        qa_all()
    else:
        names = [n.strip() for n in args.names.split(",")] if args.names else None
        asyncio.run(main(args.count, names, no_llm=args.no_llm, before=args.before, hybrid=args.hybrid))
