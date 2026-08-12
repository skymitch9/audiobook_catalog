# app/core/book_lookup.py
# "Which book does the owner mean, and what does the corrections layer see for it?"
#
# Two sources, and the difference between them is the whole point:
#
#   site/catalog.csv  - the PUBLISHED listing. What the site shows, i.e. what
#                       the owner is looking at when they say a book is wrong.
#                       These values are POST-correction.
#   the .m4b's tags   - what the pipeline derives BEFORE the corrections layer
#                       runs. An override matches on THESE values.
#
# ⚠️ Keying an entry on the published title is the one mistake that produces an
# entry which validates, reads correctly, and never fires: if an existing entry
# already corrects the title, the published title is the corrected one, and
# match.title compares against the raw ©nam. So the editor keys on the m4b
# whenever it can find it, and says so loudly when it cannot.

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import EXTS, ROOT_DIR, SITE_DIR, SITE_CSV_NAME

CATALOG_CSV = SITE_DIR / SITE_CSV_NAME

# The atoms worth recording in an entry's evidence.tags_read. Same names and
# same "absent" convention as the entries written by hand on 2026-08-11.
EVIDENCE_ATOMS = ("\xa9nam", "\xa9alb", "\xa9ART", "\xa9wrt", "\xa9day", "\xa9gen", "trkn", "SRNM", "SRSQ", "CDEK")


@dataclass
class Book:
    """One catalog row, plus whatever the file itself says."""

    row: Dict[str, str]
    path: Optional[Path] = None
    # Pre-correction values, straight from the tags. None when the file is missing.
    uncorrected: Optional[Dict[str, Optional[str]]] = None
    asin: Optional[str] = None
    tags_read: Dict[str, Any] = field(default_factory=dict)

    @property
    def title(self) -> str:
        return self.row.get("title", "")

    @property
    def author(self) -> str:
        return self.row.get("author", "")

    @property
    def filename(self) -> Optional[str]:
        return self.path.name if self.path else None

    def published(self) -> Dict[str, str]:
        """The seven correctable fields as the site currently shows them."""
        return {
            "title": self.row.get("title", ""),
            "author": self.row.get("author", ""),
            "narrator": self.row.get("narrator", ""),
            "year": self.row.get("year", ""),
            "genre": self.row.get("genre", ""),
            "series": self.row.get("series", ""),
            "series_index": self.row.get("series_index_display", ""),
        }

    def match_values(self) -> Dict[str, Optional[str]]:
        """
        What an override entry must be keyed on: the pre-correction title and
        author when the file was found, the published ones otherwise.
        """
        if self.uncorrected:
            return {"title": self.uncorrected.get("title"), "author": self.uncorrected.get("author")}
        return {"title": self.row.get("title"), "author": self.row.get("author")}

    def filename_said(self) -> str:
        """
        The stem of the file, or "" when it was not located - empty so that
        amending an entry never overwrites a real recorded filename with a
        placeholder. evidence.tags_read carries the explicit "not read" note.
        """
        return self.path.stem if self.path else ""


# --------------------------------------------------------------------------- #
# The catalog
# --------------------------------------------------------------------------- #


def load_catalog(csv_path: Path = CATALOG_CSV) -> List[Dict[str, str]]:
    if not csv_path.exists():
        return []
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def search(rows: List[Dict[str, str]], query: str, limit: int = 25) -> List[Dict[str, str]]:
    """
    Every whitespace-separated term must appear somewhere in title/author/series.
    Deliberately dumb: the owner is looking at the site and typing a few words
    they can see, not composing a query.
    """
    terms = [t.lower() for t in (query or "").split() if t]
    if not terms:
        return rows[:limit]
    hits = []
    for row in rows:
        hay = " ".join(
            (row.get(k) or "") for k in ("title", "author", "series", "series_index_display", "narrator")
        ).lower()
        if all(t in hay for t in terms):
            hits.append(row)
    # Exact-title matches first; otherwise catalog order (title, then author).
    q = query.strip().lower()
    hits.sort(key=lambda r: (0 if (r.get("title") or "").lower() == q else 1, (r.get("title") or "").lower()))
    return hits[:limit]


def locate_file(row: Dict[str, str], root: Path = ROOT_DIR) -> Optional[Path]:
    """
    Find the .m4b behind a catalog row.

    cover_href is "covers/<path relative to ROOT_DIR>/<file stem>.jpg"
    (app/metadata.py:_save_cover_for_file), so it is an exact address for the
    file - no guessing from the title, which is the field most likely to be
    wrong on the books that need correcting. All 1076 rows carry one.
    """
    href = (row.get("cover_href") or "").strip()
    if href:
        rel = Path(href)
        if rel.parts and rel.parts[0] == "covers":
            rel = Path(*rel.parts[1:])
        base = root / rel.parent / rel.stem
        for ext in sorted(EXTS):
            candidate = base.with_suffix(ext)
            if candidate.exists():
                return candidate

    # No cover, or the file was renamed since the last build: fall back to a
    # scan of the author's folder for a stem that looks like the title.
    author_dir = root / (row.get("author") or "").split(",")[0].strip()
    title = (row.get("title") or "").strip().lower()
    if title and author_dir.is_dir():
        for candidate in sorted(author_dir.iterdir()):
            if candidate.suffix.lower() in EXTS and candidate.stem.lower().startswith(title[:40]):
                return candidate
    return None


# --------------------------------------------------------------------------- #
# The file
# --------------------------------------------------------------------------- #


def read_tags(path: Path) -> Dict[str, Any]:
    """Raw mutagen tag mapping. Read-only - nothing here ever writes an m4b."""
    from mutagen.mp4 import MP4

    return dict(MP4(str(path)).tags or {})


def tags_summary(tags: Dict[str, Any]) -> Dict[str, str]:
    """
    The atoms that settle a correction, in the shape evidence.tags_read uses:
    the real value, or the string "absent".

    ⚠️ The vendor atoms are bare 'SRNM'/'SRSQ', NOT
    '----:com.apple.iTunes:SRNM' - see docs/info/catalog-corrections.md §8.1.
    Reaching for the prefixed form reads every file as untagged.
    """
    from app.metadata import first_str

    out: Dict[str, str] = {}
    for atom in EVIDENCE_ATOMS:
        if atom in tags and tags[atom]:
            value = tags[atom]
            if atom == "trkn":
                pair = value[0] if isinstance(value, list) and value else value
                out[atom] = str(tuple(pair)) if isinstance(pair, (tuple, list)) else str(pair)
            else:
                out[atom] = first_str(value) or "absent"
        else:
            out[atom] = "absent"
    return out


def load_book(row: Dict[str, str], root: Path = ROOT_DIR) -> Book:
    """A catalog row enriched with the file's own view of itself, when findable."""
    from app.metadata import K_ASIN, derive_correctable_fields, get_tag_any

    book = Book(row=row, path=locate_file(row, root))
    if book.path is None:
        return book
    try:
        tags = read_tags(book.path)
    except Exception:
        return book
    book.uncorrected = derive_correctable_fields(tags)
    book.asin = get_tag_any(tags, [K_ASIN])
    book.tags_read = tags_summary(tags)
    return book


def load_book_from_file(path: Path) -> Book:
    """Same, starting from a file path instead of a catalog row."""
    from app.metadata import K_ASIN, derive_correctable_fields, get_tag_any

    tags = read_tags(path)
    derived = derive_correctable_fields(tags)
    row = {
        "title": derived.get("title") or "",
        "author": derived.get("author") or "",
        "narrator": derived.get("narrator") or "",
        "year": derived.get("year") or "",
        "genre": derived.get("genre") or "",
        "series": derived.get("series") or "",
        "series_index_display": derived.get("series_index") or "",
        "cover_href": "",
    }
    return Book(
        row=row,
        path=path,
        uncorrected=derived,
        asin=get_tag_any(tags, [K_ASIN]),
        tags_read=tags_summary(tags),
    )
