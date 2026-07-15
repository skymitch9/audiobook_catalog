# app/additions_log.py
# Persistent record of when each book first entered the catalog.
#
# site/additions_log.json is the source of truth for "Recently Added" and the
# upload-history view. Unlike file mtimes (which change when books are moved,
# re-tagged, or re-synced), a book's logged date never changes once written —
# internal reshuffles are invisible to it.
#
# Entry sources:
#   pipeline  - seen for the first time by a normal catalog build
#   git       - backfilled from the commit that first added it to catalog.csv
#   purchase  - backfilled from the Audible purchase date
#   baseline  - present before any tracking existed; dated at the first commit
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

LOG_NAME = "additions_log.json"


def book_key(title: str, author: str) -> str:
    """Same identity key detect_new_books.py uses for the Discord snapshot."""
    return f"{title or ''}|{author or ''}"


def log_path(site_dir: Path) -> Path:
    return site_dir / LOG_NAME


def load_log(site_dir: Path) -> Dict[str, dict]:
    """Load the log as {book_key: entry}. Missing/corrupt file -> empty."""
    path = log_path(site_dir)
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {e["key"]: e for e in data.get("entries", []) if e.get("key")}
    except Exception:
        return {}


def save_log(site_dir: Path, entries: Dict[str, dict]) -> None:
    """Write entries sorted newest-first (stable order keeps git diffs small)."""
    ordered: List[dict] = sorted(
        entries.values(), key=lambda e: (e.get("added", ""), e.get("key", "")), reverse=True
    )
    site_dir.mkdir(parents=True, exist_ok=True)
    with open(log_path(site_dir), "w", encoding="utf-8") as f:
        json.dump({"entries": ordered}, f, indent=1, ensure_ascii=False)


def update_additions_log(
    rows: List[Dict[str, str]], site_dir: Path, today: Optional[str] = None
) -> Dict[str, dict]:
    """
    Append any catalog row not yet in the log, dated today. Existing entries
    are never modified, so moving files around cannot change a book's date.
    Returns the full {book_key: entry} map for rendering.
    """
    today = today or date.today().isoformat()
    entries = load_log(site_dir)
    new_count = 0
    for r in rows:
        key = book_key(r.get("title", ""), r.get("author", ""))
        if key == "|" or key in entries:
            continue
        entries[key] = {
            "key": key,
            "title": r.get("title", ""),
            "author": r.get("author", ""),
            "added": today,
            "source": "pipeline",
        }
        new_count += 1
    if new_count:
        save_log(site_dir, entries)
        print(f"[additions-log] Logged {new_count} new book(s) dated {today}")
    return entries
