"""Wikipedia client: pageview ranking, article fetch, infobox parsing."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

WIKI_API = "https://en.wikipedia.org/w/api.php"
PAGEVIEWS_API = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article"
USER_AGENT = "composer-galaxy/0.1 (local research app; contact chapman.allegra@gmail.com)"

HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}


async def _pageviews_for(client: httpx.AsyncClient, title: str, days: int = 60) -> int:
    """Return total pageviews over the last `days` days. Returns 0 on failure."""
    end = datetime.now(timezone.utc) - timedelta(days=2)
    start = end - timedelta(days=days)
    article = title.replace(" ", "_")
    url = (
        f"{PAGEVIEWS_API}/en.wikipedia/all-access/all-agents/"
        f"{httpx.URL(article).raw_path.decode()}/daily/"
        f"{start.strftime('%Y%m%d')}/{end.strftime('%Y%m%d')}"
    )
    try:
        r = await client.get(url, headers=HEADERS, timeout=15.0)
        if r.status_code != 200:
            return 0
        items = r.json().get("items", [])
        return sum(item.get("views", 0) for item in items)
    except Exception:
        return 0


async def top_by_pageviews(titles: list[str], limit: int = 200) -> list[tuple[str, int]]:
    """Rank candidate titles by recent pageviews, return top N as (title, views)."""
    sem = asyncio.Semaphore(10)

    async def fetch(client: httpx.AsyncClient, t: str) -> tuple[str, int]:
        async with sem:
            return (t, await _pageviews_for(client, t))

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[fetch(client, t) for t in titles])
    ranked = sorted(results, key=lambda x: x[1], reverse=True)
    return ranked[:limit]


_NON_PORTRAIT_KEYWORDS = [
    "grave", "tomb", "manuscript", "score", "plaque", "monument",
    "building", "house", "statue", "bust", "memorial", "birthplace",
    "museum", "church", "cathedral", "chapel", "cemetery", "stamp",
    "coin", "medal", "logo", "cover", "title_page", "titlepage",
    "autograph", "signature", "handwriting", "map", "flag", "coat_of_arms",
    "exterior", "interior", "facade", "organ", "piano", "instrument",
    "sheet_music", "frontispiece", "engraving_of_a",
    "centre", "center", "hall", "theatre", "theater", "school", "stift",
    "academy", "park", "street", "square", "bridge", "garden",
    "compared", "diagram", "motive", "analysis", "notation",
    "var1", "var2", "var3", "var4", "var5", "var6", "var7", "var8", "var9", "var10",
    "variation", "theme", "movement", "measures", "bars", "excerpt",
    "fugue", "sonata", "concerto", "symphony", "quartet", "prelude",
    "waltz", "etude", "nocturne", "opus", "overture", "cantata",
    "requiem", "aria", "libretto", "ages", "canzon", "motet",
    "madrigal", "mass_", "toccata", "ricercar", "magnificat",
    "report", "article", "newspaper", "letter", "document", "program",
    "programme", "leaflet", "poster", "advertisement", "spedalier",
    "naxos", "spotify", "itunes", "amazon", "imslp", "banner", "header",
    "icon", "widget", "nav", "footer", "sidebar",
]


def _is_non_portrait(url: str) -> bool:
    name = url.rsplit("/", 1)[-1].lower()
    return any(kw in name for kw in _NON_PORTRAIT_KEYWORDS)


_PORTRAIT_HINTS = ["headshot", "portrait", "photo", "face"]


async def _find_portrait_in_article(title: str, canonical: str) -> str | None:
    """Search article images for a likely portrait when pageimages returns nothing."""
    async with httpx.AsyncClient(timeout=15.0, headers=HEADERS) as client:
        r = await client.get(WIKI_API, params={
            "action": "query", "titles": title,
            "prop": "images", "imlimit": "30",
            "format": "json", "formatversion": "2", "redirects": "1",
        })

    pages = r.json().get("query", {}).get("pages", [])
    images = pages[0].get("images", []) if pages else []

    # Filter to jpg/png, exclude non-portraits
    candidates = [
        img["title"] for img in images
        if img["title"].lower().endswith((".jpg", ".jpeg", ".png"))
        and not _is_non_portrait(img["title"])
    ]
    if not candidates:
        return None

    # Prefer images whose filename contains the person's name or portrait hints
    parts = canonical.lower().split() if canonical else []
    surname = parts[-1] if parts else ""
    firstname = parts[0] if parts else ""
    _DEMOTE_WORDS = {"pere", "père", "father", "mother", "family", "fils", "son",
                     "brother", "sister", "wife", "husband", "parent"}
    def score(fname: str) -> int:
        fl = fname.lower()
        s = 0
        if surname and surname in fl:
            s += 10
        if firstname and len(firstname) > 3 and firstname in fl:
            s += 8
        for hint in _PORTRAIT_HINTS:
            if hint in fl:
                s += 5
        fwords = set(re.split(r"[\s\-_.,]+", fl))
        if fwords & _DEMOTE_WORDS:
            s -= 15
        return s

    candidates.sort(key=score, reverse=True)
    best = candidates[0]

    # Only use it if it scores > 0 (has surname or portrait hint)
    if score(best) == 0:
        return None

    # Get the thumbnail URL
    async with httpx.AsyncClient(timeout=15.0, headers=HEADERS) as client:
        r = await client.get(WIKI_API, params={
            "action": "query", "titles": best,
            "prop": "imageinfo", "iiprop": "url", "iiurlwidth": "200",
            "format": "json", "formatversion": "2",
        })

    pages = r.json().get("query", {}).get("pages", [])
    info = pages[0].get("imageinfo", [{}])[0] if pages else {}
    return info.get("thumburl") or info.get("url")


WIKIDATA_API = "https://www.wikidata.org/w/api.php"


async def _find_wikidata_image(title: str) -> str | None:
    """Look up the Wikidata P18 (image) property via the Wikipedia article."""
    async with httpx.AsyncClient(timeout=15.0, headers=HEADERS) as client:
        r = await client.get(WIKI_API, params={
            "action": "query", "titles": title,
            "prop": "pageprops", "ppprop": "wikibase_item",
            "format": "json", "formatversion": "2", "redirects": "1",
        })
        pages = r.json().get("query", {}).get("pages", [])
        qid = pages[0].get("pageprops", {}).get("wikibase_item") if pages else None
        if not qid:
            return None

        r2 = await client.get(WIKIDATA_API, params={
            "action": "wbgetclaims", "entity": qid,
            "property": "P18", "format": "json",
        })
        claims = r2.json().get("claims", {}).get("P18", [])
        if not claims:
            return None
        filename = claims[0]["mainsnak"]["datavalue"]["value"]
        if _is_non_portrait(filename):
            return None

        r3 = await client.get(WIKI_API, params={
            "action": "query", "titles": f"File:{filename}",
            "prop": "imageinfo", "iiprop": "url", "iiurlwidth": "200",
            "format": "json", "formatversion": "2",
        })
        pages = r3.json().get("query", {}).get("pages", [])
        info = pages[0].get("imageinfo", [{}])[0] if pages else {}
        return info.get("thumburl") or info.get("url")


_OFFICIAL_SITE_PATTERNS = [
    re.compile(r"official\s*(web)?site", re.I),
    re.compile(r"\.com/?$|\.org/?$|\.net/?$|\.nl/?$|\.de/?$|\.fr/?$|\.co\.uk/?$"),
]

_SKIP_DOMAINS = {
    "viaf.org", "isni.org", "musicbrainz.org", "d-nb.info", "worldcat.org",
    "naxos.com", "chandos.net", "hyperion-records.co.uk", "prestomusic.com",
    "catalogue.bnf.fr", "data.bnf.fr", "loc.gov", "trove.nla.gov.au",
    "wikidata.org", "wikipedia.org", "web.archive.org", "imdb.com",
    "allmusic.com", "discogs.com", "idref.fr", "deutsche-biographie.de",
    "deutsche-digitale-bibliothek.de", "opac.kbr.be", "nli.org.il",
    "spotify.com", "apple.com", "youtube.com", "twitter.com", "facebook.com",
    "instagram.com", "ircam.fr", "bibliotheken.nl", "imslp.org",
    "gramophone.co.uk", "classicfm.com", "medici.tv", "bachtrack.com",
    "boosey.com", "bruceduffie.com", "kcstudio.com", "daifujikura.com",
    "goldbergfestival.pl", "libraries.ucsd.edu", "oboeclassics.com",
}

_IMG_RE = re.compile(
    r'<img[^>]+src=["\']([^"\']+\.(?:jpg|jpeg|png|webp))["\']',
    re.I,
)


def _is_official_site(url: str, canonical: str = "") -> bool:
    """Heuristic: likely an official/personal website, not a database or social media."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
    except Exception:
        return False
    if any(host.endswith(d) or host == d for d in _SKIP_DOMAINS):
        return False
    # Prefer sites with the person's surname in the domain
    surname = canonical.split()[-1].lower() if canonical else ""
    if surname and len(surname) > 3 and surname in host.lower():
        return True
    # Accept generic personal-site TLDs but deprioritize news/reference sites
    path = url.lower()
    if any(kw in host for kw in ["news", "article", "blog", "magazine", "review"]):
        return False
    return True


async def _find_portrait_on_official_site(title: str, canonical: str) -> str | None:
    """Find a portrait on the person's official website linked from Wikipedia."""
    async with httpx.AsyncClient(timeout=15.0, headers=HEADERS, follow_redirects=True) as client:
        r = await client.get(WIKI_API, params={
            "action": "query", "titles": title,
            "prop": "extlinks", "ellimit": "30",
            "format": "json", "formatversion": "2", "redirects": "1",
        })
        pages = r.json().get("query", {}).get("pages", [])
        ext_links = pages[0].get("extlinks", []) if pages else []
        urls = [link.get("url", link) if isinstance(link, dict) else link for link in ext_links]

        # Filter to likely official sites, prioritize those with surname in domain
        from urllib.parse import urlparse
        candidates = [u for u in urls if _is_official_site(u, canonical)]
        surname = canonical.split()[-1].lower() if canonical else ""
        surname_sites = [u for u in candidates
                         if surname and len(surname) > 3
                         and surname in (urlparse(u).hostname or "").lower()]
        if surname_sites:
            candidates = surname_sites
        if not candidates:
            return None

        surname = canonical.split()[-1].lower() if canonical else ""

        for site_url in candidates[:3]:
            try:
                r2 = await client.get(site_url, timeout=10.0)
                # If the specific URL 404s, try the site root
                if r2.status_code != 200:
                    root_url = f"{urlparse(site_url).scheme}://{urlparse(site_url).hostname}/"
                    if root_url != site_url:
                        r2 = await client.get(root_url, timeout=10.0)
                if r2.status_code != 200:
                    continue
            except Exception:
                continue

            html = r2.text
            img_urls = _IMG_RE.findall(html)
            if not img_urls:
                continue

            from urllib.parse import urljoin
            for img_url in img_urls:
                full_url = urljoin(str(r2.url), img_url)
                fname = full_url.rsplit("/", 1)[-1].lower()
                if _is_non_portrait(fname):
                    continue
                # Prefer images with surname in filename
                if surname and surname in fname:
                    return full_url
                # Accept images with portrait-like hints
                for hint in _PORTRAIT_HINTS:
                    if hint in fname:
                        return full_url

            # Skip generic fallback — too many false positives (logos, banners)

    return None


async def fetch_article(title: str, retries: int = 3) -> dict[str, Any]:
    """Fetch plain-text extract + wikitext for a Wikipedia article."""
    r1 = r2 = r3 = None
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=30.0, headers=HEADERS) as client:
                r1, r2, r3 = await asyncio.gather(
                    client.get(WIKI_API, params={
                        "action": "query", "prop": "extracts", "titles": title,
                        "explaintext": "1", "format": "json", "formatversion": "2", "redirects": "1",
                    }),
                    client.get(WIKI_API, params={
                        "action": "parse", "page": title, "prop": "wikitext",
                        "format": "json", "formatversion": "2", "redirects": "1",
                    }),
                    client.get(WIKI_API, params={
                        "action": "query", "prop": "pageimages", "titles": title,
                        "pithumbsize": 200, "format": "json", "formatversion": "2", "redirects": "1",
                    }),
                )
            r1.json()
            r2.json()
            break
        except Exception:
            if attempt < retries - 1:
                await asyncio.sleep(3 * (attempt + 1))
            else:
                raise

    pages = r1.json().get("query", {}).get("pages", [])
    extract = pages[0].get("extract", "") if pages else ""
    canonical = pages[0].get("title", title) if pages else title
    wikitext = r2.json().get("parse", {}).get("wikitext", "") if r2.status_code == 200 else ""
    try:
        thumb_pages = r3.json().get("query", {}).get("pages", []) if r3.text.strip() else []
    except Exception:
        thumb_pages = []
    thumbnail = thumb_pages[0].get("thumbnail", {}).get("source") if thumb_pages else None
    if thumbnail and _is_non_portrait(thumbnail):
        thumbnail = None

    # Fallback 1: scan article images for a likely portrait
    if not thumbnail:
        thumbnail = await _find_portrait_in_article(title, canonical)

    # Fallback 2: check Wikidata P18 image property
    if not thumbnail:
        thumbnail = await _find_wikidata_image(title)

    # Fallback 3: scan official website linked from Wikipedia
    if not thumbnail:
        thumbnail = await _find_portrait_on_official_site(title, canonical)

    return {"title": canonical, "extract": extract, "wikitext": wikitext, "thumbnail": thumbnail}


# ---- Infobox parsing ----

_INFOBOX_RE = re.compile(r"\{\{\s*Infobox[^\n]*\n(.*?)^\}\}", re.DOTALL | re.MULTILINE)
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]*)?\]\]")


def _extract_infobox(wikitext: str) -> str | None:
    """Return the raw body of the first Infobox template, or None."""
    # Brace-counting approach (regex fails on nested templates)
    idx = wikitext.lower().find("{{infobox")
    if idx == -1:
        return None
    depth = 0
    i = idx
    while i < len(wikitext) - 1:
        if wikitext[i:i + 2] == "{{":
            depth += 1
            i += 2
        elif wikitext[i:i + 2] == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                return wikitext[idx:i]
        else:
            i += 1
    return None


def _field(body: str, *names: str) -> str | None:
    """Find a `| name = value` field in an infobox body."""
    for name in names:
        pattern = re.compile(
            rf"^\|\s*{re.escape(name)}\s*=\s*(.*?)(?=^\|\s*\w+\s*=|\Z)",
            re.DOTALL | re.MULTILINE | re.IGNORECASE,
        )
        m = pattern.search(body)
        if m:
            val = m.group(1).strip().rstrip("|").strip()
            if val:
                return val
    return None


def _links(value: str) -> list[str]:
    """Extract wikilink targets from a field value, deduped, order-preserving."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _WIKILINK_RE.finditer(value):
        target = m.group(1).strip()
        # Strip disambiguation suffixes like "John Adams (composer)" → keep
        if target.startswith("File:") or target.startswith("Image:"):
            continue
        if target not in seen:
            seen.add(target)
            out.append(target)
    return out


def _strip_wikitext(value: str) -> str:
    """Loose plain-text from a field value (for birth/death/nationality)."""
    v = re.sub(r"\{\{[^}]*?\|([^}]+)\}\}", r"\1", value)  # templates
    v = re.sub(r"\[\[([^\]|]+\|)?([^\]]+)\]\]", r"\2", v)  # links
    v = re.sub(r"<[^>]+>", "", v)  # html tags
    v = re.sub(r"\{\{[^}]*\}\}", "", v)  # any leftover templates
    return v.strip().strip("|").strip()


def _extract_year(value: str) -> str | None:
    m = re.search(r"(1[0-9]{3}|20[0-2][0-9])", value)
    return m.group(1) if m else None


def parse_infobox(wikitext: str) -> dict[str, Any]:
    """Extract structured fields from a composer infobox. All edges tagged source=infobox."""
    body = _extract_infobox(wikitext)
    result: dict[str, Any] = {
        "birth": None,
        "death": None,
        "nationality": None,
        "era": None,
        "teachers": [],
        "students": [],
        "influenced_by": [],
        "influenced": [],
    }
    if not body:
        return result

    birth_raw = _field(body, "birth_date", "born")
    death_raw = _field(body, "death_date", "died")
    if birth_raw:
        result["birth"] = _extract_year(birth_raw) or _strip_wikitext(birth_raw)[:40]
    if death_raw:
        result["death"] = _extract_year(death_raw) or _strip_wikitext(death_raw)[:40]

    for f in ("nationality", "origin", "birth_place"):
        v = _field(body, f)
        if v:
            result["nationality"] = _strip_wikitext(v)[:60]
            break

    era = _field(body, "era", "period", "genre")
    if era:
        result["era"] = _strip_wikitext(era)[:60]

    for key, fields in [
        ("teachers", ["teachers", "teacher"]),
        ("students", ["students", "pupils", "notable_students", "notable students"]),
        ("influenced_by", ["influences", "influenced_by"]),
        ("influenced", ["influenced"]),
    ]:
        v = _field(body, *fields)
        if v:
            result[key] = _links(v)

    return result


# ---- Prose-based relationship extraction ----

_TEACHER_PATTERNS = [
    re.compile(r"(?:studied|studies|study)\s+(?:[\w,]+\s+){0,8}(?:under|with)\s+(?:[\w,]+\s+){0,8}\[\[([^\]|]+)", re.I),
    re.compile(r"(?:taught|tutelage|instruction|mentored)\s+(?:[\w,]+\s+){0,5}(?:by|with|from)\s+\[\[([^\]|]+)", re.I),
    re.compile(r"pupil\s+of\s+\[\[([^\]|]+)", re.I),
    re.compile(r"teacher\s+(?:was\s+)?\[\[([^\]|]+)", re.I),
    re.compile(r"under\s+(?:the\s+)?(?:guidance|direction|supervision)\s+of\s+\[\[([^\]|]+)", re.I),
    re.compile(r"taught\s+(?:\w+\s+){0,3}by\s+(?:.*?\[\[[^\]|]+\]\].*?,\s*)?(?:and\s+)?(?:later\s+)?(?:by\s+)?\[\[([^\]|]+)", re.I),
]

_STUDENT_PATTERNS = [
    re.compile(r"(?<!was\s)(?<!were\s)(?<!been\s)(?:taught|mentored)\s+\[\[([^\]|]+)", re.I),
    re.compile(r"(?:pupils?|students?)\s+(?!of\b)(?:\w+\s+){0,3}\[\[([^\]|]+)", re.I),
    re.compile(r"(?:pupils?|students?)\s+(?:included|such as|were)\s+\[\[([^\]|]+)", re.I),
]

_REVERSE_STUDENT_PATTERNS = [
    re.compile(r"\[\[([^\]|]+)\]\][\w,\s]{0,120}(?:studied|study|studies)\s+(?:[\w,]+\s+){0,8}(?:under|with)\b", re.I),
    re.compile(r"\[\[([^\]|]+)\]\][\w,\s]{0,120}(?:was\s+a\s+)?(?:pupil|student)\s+of\b", re.I),
    re.compile(r"\[\[([^\]|]+)\]\][\w,\s]{0,120}(?:was\s+)?(?:taught|instructed|mentored)\s+(?:[\w,]+\s+){0,3}by\b", re.I),
]

_INFLUENCE_PATTERNS = [
    re.compile(r"influenced\s+by\s+(?:\w+\s+){0,3}\[\[([^\]|]+)", re.I),
    re.compile(r"(?:under|fell under)\s+(?:the\s+)?(?:spell|influence)\s+of\s+\[\[([^\]|]+)", re.I),
    re.compile(r"(?:inspired|influenced)\s+(?:\w+\s+){0,3}by\s+\[\[([^\]|]+)", re.I),
]

_SKIP_TARGETS = {"File:", "Image:", "Category:", "Wikipedia:", "Help:", "WP:"}

_NON_PERSON_WORDS = {
    "harpsichord", "lute", "piano", "violin", "viola", "cello", "organ",
    "flute", "oboe", "clarinet", "bassoon", "trumpet", "trombone", "guitar",
    "harp", "percussion", "drums", "saxophone", "recorder", "mandolin",
    "opera", "symphony", "sonata", "concerto", "quartet", "fugue", "cantata",
    "oratorio", "suite", "overture", "prelude", "étude", "waltz", "mass",
    "conservatory", "conservatoire", "university", "school", "academy",
    "institute", "college", "seminary",
    "music", "folk", "baroque", "classical", "romantic", "neoclassicism",
    "modernism", "impressionism", "serialism", "atonality",
    "prize", "award", "diploma", "medal", "society", "theory", "form",
}


def _looks_like_person(target: str) -> bool:
    lower = target.lower()
    if len(target) < 4:
        return False
    words = set(re.split(r"[\s\-_(,)]+", lower))
    return not bool(words & _NON_PERSON_WORDS)


def _safe_link(target: str) -> bool:
    return not any(target.startswith(p) for p in _SKIP_TARGETS) and _looks_like_person(target)


def _find_list_links(wikitext: str, match_end: int, max_chars: int = 200) -> list[str]:
    """After a pattern match, grab adjacent comma/and-separated wikilinks."""
    region = wikitext[match_end:match_end + max_chars]
    names: list[str] = []
    pos = 0
    while pos < len(region):
        gap = region[pos:pos + 80]
        link_m = re.match(
            r"\s*(?:,\s*(?:and\s+)?(?:later\s+)?(?:by\s+)?|and\s+(?:later\s+)?(?:by\s+)?)\[\[([^\]|]+)",
            gap,
        )
        if not link_m:
            break
        target = link_m.group(1).strip()
        if _safe_link(target):
            names.append(target)
        pos += link_m.end()
    return names


def parse_prose_relationships(wikitext: str) -> dict[str, list[str]]:
    """Extract teacher/student/influence relationships from article wikitext prose."""
    result: dict[str, list[str]] = {
        "teachers": [], "students": [], "influenced_by": [],
    }

    for patterns, key in [
        (_TEACHER_PATTERNS, "teachers"),
        (_STUDENT_PATTERNS, "students"),
        (_REVERSE_STUDENT_PATTERNS, "students"),
        (_INFLUENCE_PATTERNS, "influenced_by"),
    ]:
        seen: set[str] = set()
        for pat in patterns:
            for m in pat.finditer(wikitext):
                target = m.group(1).strip()
                if _safe_link(target) and target not in seen:
                    seen.add(target)
                    result[key].append(target)
                end_of_first_link = wikitext.find("]]", m.end()) + 2
                if end_of_first_link > 2:
                    for name in _find_list_links(wikitext, end_of_first_link):
                        if name not in seen:
                            seen.add(name)
                            result[key].append(name)

    return result
