"""Shared helper: which books are clubs actually engaging with right now.

Used by generate_prompts.py and fetch_content_warnings.py so LLM/API spend
stays scoped to books on club Current Reads and TBRs instead of the whole
988-book catalog.
"""

import json
import urllib.request

BASE = "https://firestore.googleapis.com/v1/projects/audiobook-catalog/databases/(default)/documents"
API_KEY = "AIzaSyDgAblkxzVxl7nFbd7jXOo6PpuNPsJw11Y"  # public web API key
COLLECTIONS = ("clubs", "clubs_dev")


def fetch(path):
    url = f"{BASE}/{path}?key={API_KEY}&pageSize=300"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read()).get("documents", [])


def gv(fields, name, kind="stringValue", default=""):
    return fields.get(name, {}).get(kind, default)


def club_book_titles(include_tbr=True, include_finished=False):
    """Set of (title, author) tuples clubs are reading / planning to read."""
    books = set()
    for coll in COLLECTIONS:
        try:
            clubs = fetch(coll)
        except Exception as e:
            print(f"[WARN] listing {coll} failed: {e}")
            continue
        for club in clubs:
            cid = club["name"].split("/")[-1]
            try:
                reads = fetch(f"{coll}/{cid}/reads")
            except Exception:
                reads = []
            for r in reads:
                f = r["fields"]
                if include_finished or gv(f, "status") == "active":
                    title = gv(f, "bookTitle")
                    if title:
                        books.add((title, gv(f, "bookAuthor")))
            if include_tbr:
                try:
                    tbr = fetch(f"{coll}/{cid}/tbr")
                except Exception:
                    tbr = []
                for item in tbr:
                    f = item["fields"]
                    title = gv(f, "bookTitle")
                    if title:
                        books.add((title, gv(f, "bookAuthor")))
    return books
