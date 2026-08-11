# app/core/catalog_overrides.py
# The catalog corrections layer.
#
# scripts/catalog_overrides.json corrects any field the pipeline derives from m4b
# tags - title, author, narrator, year, genre, series, series_index - without
# touching the audio files. app/metadata.py:extract_metadata() calls
# apply_overrides() after tag extraction and title parsing, so a correction is
# always the last word.
#
# Two independent things happen here:
#   1. an "overrides" entry replaces named fields on a matched book;
#   2. "canonical_series" folds variant spellings of a series name onto one
#      canonical form, for every book, matched or not.
#
# Both are no-ops when the JSON is missing or malformed, so the catalog build
# never depends on this file being present or well-formed.
#
# Keying, and why it survives a re-sync: prefer match.asin (the CDEK atom, the
# Audible product identity - immune to both renames and retagging); otherwise
# match.title + match.author, which survives renames and re-downloads. match.file
# is a tiebreaker only, because filenames drift. See _keying in the JSON.

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

OVERRIDES_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "catalog_overrides.json"

# The only fields a correction may touch. Anything else in a "set" block is
# ignored, so a typo cannot quietly invent a new column.
CORRECTABLE_FIELDS = ("title", "author", "narrator", "year", "genre", "series", "series_index")


def _norm(s: Optional[str]) -> str:
    """Lowercase, collapse whitespace. Used for every match comparison."""
    if not s:
        return ""
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def _load(path: Path = OVERRIDES_PATH) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"canonical_series": {}, "overrides": []}
    except Exception:
        # A malformed corrections file must never take the catalog build down.
        return {"canonical_series": {}, "overrides": []}

    canonical = {_norm(k): v for k, v in (data.get("canonical_series") or {}).items()}
    overrides: List[Dict[str, Any]] = [
        e for e in (data.get("overrides") or []) if isinstance(e, dict) and isinstance(e.get("match"), dict) and e.get("set")
    ]
    return {"canonical_series": canonical, "overrides": overrides}


_DATA = _load()


def reload_overrides(path: Path = OVERRIDES_PATH) -> None:
    """Re-read the JSON. Used by tests and ad-hoc tooling."""
    global _DATA
    _DATA = _load(path)


def canonicalize_series(series: Optional[str]) -> Optional[str]:
    """Fold a variant spelling onto the canonical one. Unknown names pass through."""
    if not series:
        return series
    return _DATA["canonical_series"].get(_norm(series), series)


def _author_matches(wanted: str, actual: Optional[str]) -> bool:
    """True if `wanted` is one of the comma-separated names in `actual`."""
    want = _norm(wanted)
    return any(_norm(part) == want for part in (actual or "").split(","))


def find_override(
    title: Optional[str] = None,
    author: Optional[str] = None,
    filename: Optional[str] = None,
    asin: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return the first entry whose every declared match field matches, else None."""
    for entry in _DATA["overrides"]:
        m = entry["match"]
        if m.get("asin") and _norm(m["asin"]) != _norm(asin):
            continue
        if m.get("title") and _norm(m["title"]) != _norm(title):
            continue
        if m.get("author") and not _author_matches(m["author"], author):
            continue
        if m.get("file") and _norm(m["file"]) != _norm(filename):
            continue
        return entry
    return None


def apply_overrides(
    row: Dict[str, Optional[str]],
    path: Optional[Path] = None,
    asin: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """
    Return a copy of `row` with any matching correction applied and the series
    name canonicalized.

    `row` is matched on its PRE-correction values, so an entry may safely correct
    the very field it is keyed on. Precedence: correction > extracted tags/title.
    A field set to "" is corrected to empty on purpose - that is how an unknown
    volume is recorded, rather than invented.
    """
    out = dict(row)
    entry = find_override(
        title=row.get("title"),
        author=row.get("author"),
        filename=path.name if path is not None else None,
        asin=asin,
    )
    if entry is not None:
        for field, value in entry["set"].items():
            if field in CORRECTABLE_FIELDS:
                out[field] = "" if value is None else str(value)

    out["series"] = canonicalize_series(out.get("series"))
    return out
