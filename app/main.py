"""FastAPI app: serves the graph JSON and the static frontend."""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import unquote

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

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
