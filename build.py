"""Static site generator: builds graph.json and downloads external thumbnails.

Usage:
    python build.py              # generate web/graph.json + download images
    python build.py --skip-images  # generate graph.json only (faster)

After running, the web/ directory is a self-contained static site.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.graph import load_graph

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
IMG_DIR = WEB_DIR / "img"


def _is_external(url: str) -> bool:
    if not url:
        return False
    return "wikimedia.org" not in url and "wikipedia.org" not in url


def _img_filename(url: str) -> str:
    ext = Path(urlparse(url).path).suffix or ".jpg"
    h = hashlib.md5(url.encode()).hexdigest()[:12]
    return f"{h}{ext}"


async def download_images(graph: dict):
    IMG_DIR.mkdir(exist_ok=True)
    external = []
    for node in graph["nodes"]:
        url = node["data"].get("thumbnail")
        if url and _is_external(url):
            external.append(node)

    if not external:
        print("  No external images to download.")
        return

    print(f"  Downloading {len(external)} external thumbnails...")
    async with httpx.AsyncClient(follow_redirects=True, timeout=15.0,
                                  headers={"User-Agent": "ComposerGalaxy/1.0"}) as client:
        for i, node in enumerate(external):
            url = node["data"]["thumbnail"]
            fname = _img_filename(url)
            dest = IMG_DIR / fname
            if dest.exists():
                node["data"]["thumbnail"] = f"img/{fname}"
                continue
            try:
                r = await client.get(url)
                if r.status_code == 200 and len(r.content) > 500:
                    dest.write_bytes(r.content)
                    node["data"]["thumbnail"] = f"img/{fname}"
                else:
                    node["data"]["thumbnail"] = None
            except Exception:
                node["data"]["thumbnail"] = None
            if (i + 1) % 20 == 0:
                print(f"    ...{i + 1}/{len(external)}")


def build(skip_images: bool = False):
    print("Building graph...")
    graph = load_graph()
    print(f"  {len(graph['nodes'])} nodes, {len(graph['edges'])} edges")

    if not skip_images:
        asyncio.run(download_images(graph))

    out = WEB_DIR / "graph.json"
    out.write_text(json.dumps(graph, ensure_ascii=False, separators=(",", ":")))
    print(f"  Wrote {out} ({out.stat().st_size // 1024} KB)")
    print("\nDone. Serve web/ with any static server:")
    print("  python -m http.server -d web 8742")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-images", action="store_true")
    args = ap.parse_args()
    build(args.skip_images)
