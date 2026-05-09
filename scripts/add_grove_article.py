#!/usr/bin/env python3
"""Add a Grove article to the local JSON cache.

Usage:  cat article.txt | python scripts/add_grove_article.py "Composer Name"
"""

import json, os, sys

CACHE = os.path.join(os.path.dirname(__file__), "..", "data", "grove_articles.json")

cache = {}
if os.path.exists(CACHE):
    with open(CACHE, encoding="utf-8") as f:
        cache = json.load(f)

name = sys.argv[1]
cache[name] = sys.stdin.read()

with open(CACHE, "w", encoding="utf-8") as f:
    json.dump(cache, f, ensure_ascii=False)

print(f"Added '{name}' ({len(cache[name])} chars). Cache now has {len(cache)} articles.")
