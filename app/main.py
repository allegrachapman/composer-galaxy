"""FastAPI app: serves the graph JSON and the static frontend."""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote

import httpx
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

from . import graph, seed  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"

app = FastAPI(title="Composer Galaxy")


@app.get("/graph")
def get_graph():
    return graph.load_graph()


@app.post("/expand/{name:path}")
async def expand_composer(name: str):
    """Seed a single composer on demand (click-to-expand stub nodes)."""
    try:
        record = await seed.process_composer(name)
        seed.write_markdown(record)
        return graph.load_graph()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


class VerifyEdge(BaseModel):
    composer: str  # node id of the composer whose file we edit
    target: str    # name of the teacher/student
    field: str     # "teachers" or "students"


@app.post("/verify-edge")
def verify_edge(body: VerifyEdge):
    """Add verified: true to an edge using targeted text insertion (no YAML round-trip)."""
    import re as _re
    composers_dir = ROOT / "composers"
    from .graph import _node_id
    for path in composers_dir.glob("*.md"):
        text = path.read_text()
        if not text.startswith("---"):
            continue
        parts = text.split("---", 2)
        if len(parts) < 3:
            continue
        meta = yaml.safe_load(parts[1])
        if not meta or _node_id(meta.get("name", "")) != body.composer:
            continue
        for entry in meta.get(body.field) or []:
            if _node_id(entry.get("name", "")) != body.target:
                continue
            if entry.get("verified"):
                return {"ok": True, "already": True}
            escaped = _re.escape(entry["name"])
            pattern = _re.compile(
                rf"(- name: (?:'{escaped}'|{escaped})\n(?:  [^\n]+\n)*)",
                _re.MULTILINE,
            )
            fm = parts[1]
            match = pattern.search(fm)
            if not match:
                raise HTTPException(status_code=404, detail="Edge not found in raw text")
            block = match.group(1)
            new_block = block.rstrip("\n") + "\n  verified: true\n"
            fm = fm.replace(block, new_block, 1)
            path.write_text(f"---{fm}---{parts[2]}")
            return {"ok": True}
        raise HTTPException(status_code=404, detail="Edge not found")
    raise HTTPException(status_code=404, detail="Composer file not found")


class FlagEdge(BaseModel):
    composer: str
    target: str
    field: str


@app.post("/flag-edge")
def flag_edge(body: FlagEdge):
    """Add flagged: true to an edge using targeted text insertion (no YAML round-trip)."""
    import re as _re
    composers_dir = ROOT / "composers"
    from .graph import _node_id
    for path in composers_dir.glob("*.md"):
        text = path.read_text()
        if not text.startswith("---"):
            continue
        parts = text.split("---", 2)
        if len(parts) < 3:
            continue
        meta = yaml.safe_load(parts[1])
        if not meta or _node_id(meta.get("name", "")) != body.composer:
            continue
        for entry in meta.get(body.field) or []:
            if _node_id(entry.get("name", "")) != body.target:
                continue
            if entry.get("flagged"):
                return {"ok": True, "already": True}
            escaped = _re.escape(entry["name"])
            pattern = _re.compile(
                rf"(- name: (?:'{escaped}'|{escaped})\n(?:  [^\n]+\n)*)",
                _re.MULTILINE,
            )
            fm = parts[1]
            match = pattern.search(fm)
            if not match:
                raise HTTPException(status_code=404, detail="Edge not found in raw text")
            block = match.group(1)
            new_block = block.rstrip("\n") + "\n  flagged: true\n"
            fm = fm.replace(block, new_block, 1)
            path.write_text(f"---{fm}---{parts[2]}")
            return {"ok": True}
        raise HTTPException(status_code=404, detail="Edge not found")
    raise HTTPException(status_code=404, detail="Composer file not found")


def _build_graph_summary() -> str:
    """Build a compact text summary of the graph for LLM context."""
    g = graph.load_graph()
    teachers_of = defaultdict(list)
    students_of = defaultdict(list)
    for e in g["edges"]:
        d = e["data"]
        if d["kind"] == "teacher":
            teachers_of[d["target"]].append(d["source"])
            students_of[d["source"]].append(d["target"])

    lines = []
    for n in g["nodes"]:
        d = n["data"]
        if d.get("stub"):
            continue
        name = d["label"]
        t = ",".join(teachers_of.get(name, []))
        s = ",".join(students_of.get(name, []))
        parts = [name, d.get("birth") or "", d.get("death") or "", d.get("era") or ""]
        if t:
            parts.append("T:" + t)
        if s:
            parts.append("S:" + s)
        lines.append("|".join(parts))
    return "\n".join(lines)


_ASK_SYSTEM = """\
You are a knowledgeable music history assistant for "Composer Galaxy," an interactive \
visualization of classical composer teacher/student relationships.

You have access to a dataset of composers and their pedagogical connections. The data \
is in a compact format: Name|Birth|Death|Era|T:teacher1,teacher2|S:student1,student2

Answer the user's question based on this data. Be concise but thorough. When listing \
composers, include their birth/death years. If the question involves counting or ranking, \
show your work briefly. If the data doesn't contain enough information to answer, say so.

<graph-data>
{graph_data}
</graph-data>"""


class AskRequest(BaseModel):
    question: str


@app.post("/ask")
async def ask_question(body: AskRequest):
    from google import genai

    summary = _build_graph_summary()
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"{_ASK_SYSTEM.format(graph_data=summary)}\n\nQuestion: {body.question}",
        )
        return {"answer": response.text or "No response."}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/img-proxy")
async def img_proxy(url: str = Query(...)):
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
            r = await client.get(url, headers={"User-Agent": "ComposerGalaxy/1.0"})
            ct = r.headers.get("content-type", "image/jpeg")
            return Response(content=r.content, media_type=ct)
    except Exception:
        raise HTTPException(status_code=502, detail="Failed to fetch image")


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")
