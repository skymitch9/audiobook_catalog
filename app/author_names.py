"""Author name -> shelf folder. The single source of truth for both sorters.

Two questions look identical and are not:

  1. **Which local folder does this file belong in?**  Answered here, from the
     file's own `©ART` tag, then through `scripts/author_shelf_aliases.json`.
  2. **Which Google Drive folder does that local folder upload to?**  Answered
     by `scripts/author_aliases.json` in `sync_to_drive.resolve_alias()`, keyed
     on the *folder name* this module produced.

They run in that order and they are allowed to disagree. Drive has one folder
per human being; a shelf has one folder per body of work. Collapsing the two
maps into one is what caused the 2026-08-09 incident: a Drive-routing line
saying William D. Arand and Randi Darren are the same person was executed as a
shelving instruction, and it merged two separate bibliographies.
See `docs/info/author-folder-audit.md`.

`get_author_name` lived in `app/tools/book_sort.py`, a superseded whole-library
sorter. It is here so that script can eventually be archived, and so the
pipeline sorter and any hand-run of the old one cannot drift apart again.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

from mutagen.mp4 import MP4

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
PRIORITY_AUTHORS_PATH = SCRIPTS_DIR / "priority_authors.json"
SHELF_ALIASES_PATH = SCRIPTS_DIR / "author_shelf_aliases.json"

# iTunes atom for author
K_ARTIST = "\xa9ART"


def _bytes_to_str(b: bytes) -> str:
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            return b.decode(enc).strip()
        except Exception:
            pass
    return b.decode("utf-8", errors="ignore").strip()


def _first_str(val) -> Optional[str]:
    v = val[0] if isinstance(val, list) and val else val
    if v is None:
        return None
    if isinstance(v, bytes):
        return _bytes_to_str(v)
    return str(v).strip()


@lru_cache(maxsize=1)
def load_priority_authors() -> tuple[str, ...]:
    """Authors who win when a tag credits several people. Lowercased, ranked."""
    if PRIORITY_AUTHORS_PATH.exists():
        try:
            with open(PRIORITY_AUTHORS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return tuple(a.lower() for a in data.get("priority_authors", []))
        except Exception:
            pass
    return ()


def get_author_name(file_path: Path) -> Optional[str]:
    """
    Returns a normalized primary author string from MP4/M4B tags.
    - Reads ©ART / \xa9ART (iTunes 'Artist' a.k.a. Author in many audiobook rips)
    - For multi-author fields, uses priority-author classification to select primary.
    - Falls back to first author before comma if no priority author found.

    Capitalises each word but leaves all-caps tokens of <=5 chars alone, so
    "A.J.", "T." and "XX" survive. It never adds or removes spaces — every
    spacing variant in the library is data, and lives in the shelf alias map.
    """
    try:
        audio = MP4(str(file_path))
        tags = audio.tags or {}
        author_field = tags.get(K_ARTIST)
        if not author_field:
            return None
        raw = _first_str(author_field)
        if not raw:
            return None

        # Split all authors and normalize each
        parts = re.split(r"[;,/&]| and ", raw, flags=re.IGNORECASE)
        authors = []
        for p in parts:
            name = p.strip()
            if not name:
                continue
            name_parts = name.split()
            if not name_parts:
                continue
            normalized = " ".join(
                w if (w.isupper() and len(w) <= 5) else w.capitalize()
                for w in name_parts
            )
            authors.append(normalized)

        if not authors:
            return None

        # Check if any author is in the priority list — pick highest rank
        priority = load_priority_authors()
        best_author = None
        best_rank = len(priority) + 1
        for author in authors:
            if author.lower() in priority:
                rank = priority.index(author.lower())
                if rank < best_rank:
                    best_rank = rank
                    best_author = author
        if best_author:
            return best_author

        # Default to first author
        return authors[0]
    except Exception as e:
        print(f"[WARN] Metadata read failed: {file_path} - {e}")
        return None


@lru_cache(maxsize=1)
def load_shelf_aliases() -> dict[str, str]:
    """Load the LOCAL SHELVING map, lowercased for case-insensitive lookup.

    Deliberately a different file from `scripts/author_aliases.json`. That one
    routes a folder to a Drive folder and is free to say "these two names are
    one person"; this one says "these two names are one shelf", which is a
    stronger claim. Keeping them separate makes the dangerous direction
    fail-safe: a new Drive-routing entry can never silently rearrange 1,100
    files on disk, which is exactly how the 2026-08-09 incident happened.
    """
    if not SHELF_ALIASES_PATH.exists():
        return {}
    try:
        with open(SHELF_ALIASES_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        print(f"[WARN] Could not read {SHELF_ALIASES_PATH.name}: {e}")
        return {}
    return {
        k.lower(): v
        for k, v in raw.items()
        # "_" keys are the file's own documentation. The __FOLDER_ID__ guard is
        # belt-and-braces against someone copying a row over from the Drive map:
        # a folder ID is not a folder name and must never become one.
        if not k.startswith("_")
        and isinstance(v, str)
        and not v.startswith("__FOLDER_ID__")
    }


def resolve_shelf_author(author: str, aliases: dict[str, str] | None = None) -> str:
    """Map a tag-derived author name to the folder its books are shelved under."""
    if not author:
        return author
    if aliases is None:
        aliases = load_shelf_aliases()
    return aliases.get(author.lower(), author)
