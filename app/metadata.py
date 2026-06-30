# app/metadata.py
# Read MP4/M4B tags with Mutagen; prefer SRNM/SRSQ for series; fall back to title parsing.
# Extract embedded cover art, and a cleaned "desc" with HTML removed and paragraph breaks preserved.

from __future__ import annotations

import html as htmlmod
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

# Import config (paths used for cover extraction output)
from app.config import OUTPUT_DIR, ROOT_DIR

# ---------- Tag keys ----------
# MP4/iTunes atoms
K_TITLE = "\xa9nam"  # Title
K_ARTIST = "\xa9ART"  # Author(s)
K_WRITER = "\xa9wrt"  # Narrator
K_DAY = "\xa9day"  # Year/Date
K_GENRE = "\xa9gen"  # Genre

# Descriptions (common atoms)
K_COMMENT = "\xa9cmt"  # Comment / short description
K_LONGDES = "ldes"  # Long description
K_DESC = "desc"  # Description (some tools use this)

# Vendor atoms (Audible-style that you provided)
K_SERIES_VENDOR = "SRNM"  # Series Name
K_INDEX_VENDOR = "SRSQ"  # Series Sequence (e.g., 2.1)

# Free-form keys (----:com.apple.iTunes:*)
FREEFORM_HINTS = {
    "series": ["series", "book series", "audible:series", "audible:seriesname"],
    "series_index": ["series index", "series_index", "audible:seriessequence", "series number", "series_no"],
    # description-related suffixes often seen in freeform frames
    "description": ["description", "comment", "synopsis", "summary", "audible:description", "audible:synopsis"],
}

# Import index normalization functions from core module
from app.core.index_utils import normalize_index as _normalize_index
from app.core.index_utils import sort_key_for_index as _sort_key_for_index


# ---------- tag access helpers ----------
def bytes_to_str(b: bytes) -> str:
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            return b.decode(enc).strip()
        except Exception:
            pass
    return b.decode("utf-8", errors="ignore").strip()


def first_str(val):
    v = val[0] if isinstance(val, list) and val else val
    if isinstance(v, bytes):
        return bytes_to_str(v)
    if isinstance(v, tuple):
        return str(v[0])
    return (str(v).strip()) if v is not None else None


def get_tag_any(tags: Dict, keys: List[str]) -> Optional[str]:
    for k in keys:
        if k in tags and tags[k]:
            s = first_str(tags[k])
            if s:
                return s
    return None


def get_freeform_by_suffix(tags: Dict, suffixes: List[str]) -> Optional[str]:
    for key, val in (tags or {}).items():
        if not isinstance(key, str) or not key.startswith("----"):
            continue
        tail = key.split(":")[-1].lower()
        if any(tail.endswith(sfx.lower()) for sfx in suffixes):
            if isinstance(val, list) and val:
                parts = []
                for piece in val:
                    if isinstance(piece, MP4FreeForm):
                        parts.append(bytes_to_str(bytes(piece)))
                    elif isinstance(piece, (bytes, bytearray)):
                        parts.append(bytes_to_str(piece))
                    else:
                        parts.append(str(piece))
                joined = ", ".join(p for p in parts if p)
                if joined.strip():
                    return joined.strip()
    return None


def _cleanup_series(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    s = re.sub(r"\bseries\b\s*$", "", name, flags=re.IGNORECASE).strip(" -–—:,")
    return re.sub(r"\s{2,}", " ", s).strip()


def _load_priority_authors() -> list[str]:
    """Load the priority authors list from scripts/priority_authors.json."""
    priority_path = Path(__file__).resolve().parent.parent / "scripts" / "priority_authors.json"
    if priority_path.exists():
        import json
        try:
            with open(priority_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return [a.lower() for a in data.get("priority_authors", [])]
        except Exception:
            pass
    return []


# Cache priority authors at module level
_PRIORITY_AUTHORS: list[str] = _load_priority_authors()


def normalize_people_field(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    parts = re.split(r"[;,/&]| and ", s, flags=re.IGNORECASE)
    cleaned, seen = [], set()
    for p in parts:
        name = re.sub(r"\s+", " ", p).strip()
        if not name:
            continue
        norm = name if (name.isupper() and len(name) <= 5) else " ".join(w.capitalize() for w in name.split())
        key = norm.lower()
        if key not in seen:
            seen.add(key)
            cleaned.append(norm)
    return ", ".join(cleaned) if cleaned else None


def resolve_primary_author(author_field: Optional[str]) -> Optional[str]:
    """
    Given a multi-author field like "Dennis Vanderkerken, Dakota Krout",
    reorder so the highest-ranked priority author comes first. This ensures
    catalog display, Drive folder assignment, and author links all use the
    canonical primary.

    If no priority author is found, the original order is preserved.
    """
    if not author_field:
        return author_field
    parts = [a.strip() for a in author_field.split(",")]
    if len(parts) <= 1:
        return author_field

    # Find the highest-ranked priority author (lowest index in _PRIORITY_AUTHORS)
    best_idx = -1
    best_rank = len(_PRIORITY_AUTHORS) + 1
    for i, author in enumerate(parts):
        if author.lower() in _PRIORITY_AUTHORS:
            rank = _PRIORITY_AUTHORS.index(author.lower())
            if rank < best_rank:
                best_rank = rank
                best_idx = i

    if best_idx < 0:
        return author_field  # no priority author found
    if best_idx == 0:
        return author_field  # already first

    # Move highest-ranked priority author to front
    reordered = [parts[best_idx]] + parts[:best_idx] + parts[best_idx + 1:]
    return ", ".join(reordered)


def sec_to_hhmm(s: Optional[int]) -> str:
    if s is None:
        return ""
    try:
        s = int(s)
    except Exception:
        return ""
    h = s // 3600
    m = (s % 3600) // 60
    return f"{h}:{m:02d}"


# ---------- desc cleaner ----------
_TAG_BLOCK_RE = re.compile(
    r"(?is)"
    r"<!--.*?-->|"  # HTML comments
    r"<script.*?>.*?</script>|"
    r"<style.*?>.*?</style>"
)

_BR_RE = re.compile(r"(?i)<\s*br\s*/?\s*>")
_P_RE = re.compile(r"(?i)</\s*p\s*>")
_TAG_RE = re.compile(r"<[^>]+>")  # any other tags


def _html_to_plain_text(s: str) -> str:
    """
    Convert HTML-ish description to readable plain text:
    - convert <br> to '\n'
    - convert </p> to '\n\n'
    - strip remaining tags (<i>, <b>, <ul>, etc.)
    - unescape HTML entities (&amp;, &quot;, etc.)
    - collapse 3+ newlines to max 2, trim
    """
    if not s:
        return ""
    # remove comments/scripts/styles
    s = _TAG_BLOCK_RE.sub("", s)
    # normalize paragraph/line breaks
    s = _BR_RE.sub("\n", s)
    s = _P_RE.sub("\n\n", s)
    # strip any remaining tags
    s = _TAG_RE.sub("", s)
    # unescape entities
    s = htmlmod.unescape(s)
    # normalize whitespace: CRLF → LF, collapse extra blank lines, strip spaces at line ends
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # trim trailing spaces per line
    s = "\n".join(line.rstrip() for line in s.split("\n"))
    # collapse 3+ newlines to at most 2
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# ---------- cover extraction ----------
def _save_cover_for_file(path: Path) -> Optional[str]:
    """
    Extract first cover from 'covr' atom and write it under:
      OUTPUT_DIR / "covers" / <relative-to-ROOT_DIR parent> / <stem>.<ext>
    Returns a site-relative href like:
      "covers/<relative-path>/<filename>.jpg"
    or None if no cover found.
    """
    try:
        audio = MP4(str(path))
        tags = audio.tags or {}
        covrs = tags.get("covr")
        if not covrs:
            return None

        cover = covrs[0]
        ext = ".jpg"
        try:
            if isinstance(cover, MP4Cover):
                if cover.imageformat == MP4Cover.FORMAT_PNG:
                    ext = ".png"
                elif cover.imageformat == MP4Cover.FORMAT_JPEG:
                    ext = ".jpg"
        except Exception:
            pass

        rel = path.relative_to(ROOT_DIR)
        out_dir = OUTPUT_DIR / "covers" / rel.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / (path.stem + ext)

        data = bytes(cover) if isinstance(cover, (MP4Cover, bytes, bytearray)) else bytes(cover)
        out_path.write_bytes(data)

        return str(Path("covers") / rel.parent / (path.stem + ext)).replace("\\", "/")
    except Exception:
        return None


# Import the improved parsing logic
from app.parsers.title import parse_series_and_index_from_title


# ---------- high-level extractors ----------
def _extract_description(tags: Dict) -> Optional[str]:
    """
    Pull a human-friendly description from common atoms first, then freeform.
    Priority: ldes > desc > ©cmt > free-form 'description-like' suffixes.
    Clean HTML into readable text with paragraph breaks.
    """
    # 1) Direct atoms
    for key in (K_LONGDES, K_DESC, K_COMMENT):
        val = get_tag_any(tags, [key])
        if val and val.strip():
            return _html_to_plain_text(val.strip())

    # 2) Free-form fallbacks
    ff = get_freeform_by_suffix(tags, FREEFORM_HINTS["description"])
    if ff and ff.strip():
        return _html_to_plain_text(ff.strip())

    return None


COMPANION_EXTS = {".pdf", ".epub", ".mobi", ".azw3"}


def _find_companion_files(audio_path: Path) -> str:
    """
    Look for companion files (PDF, EPUB, MOBI) in the same directory as an audiobook.
    Returns a pipe-separated string of companion filenames, or empty string if none.
    """
    parent = audio_path.parent
    companions = []
    try:
        for f in parent.iterdir():
            if f.is_file() and f.suffix.lower() in COMPANION_EXTS:
                companions.append(f.name)
    except OSError:
        pass
    companions.sort()
    return " | ".join(companions)


def extract_metadata(path: Path) -> Dict[str, str]:
    """Extract metadata from an MP4/M4B file, preferring SRNM/SRSQ,
    then free-form tags, and finally conservative title parsing. Also saves cover and cleaned description."""
    audio = MP4(str(path))
    tags = audio.tags or {}
    duration = getattr(getattr(audio, "info", None), "length", None)
    length_sec = int(duration) if duration else None

    title = get_tag_any(tags, [K_TITLE]) or ""
    author = resolve_primary_author(normalize_people_field(get_tag_any(tags, [K_ARTIST])))
    narrator = normalize_people_field(get_tag_any(tags, [K_WRITER]))
    year = get_tag_any(tags, [K_DAY]) or ""
    genre = get_tag_any(tags, [K_GENRE]) or ""

    # Description (cleaned)
    desc = _extract_description(tags) or ""

    # 1) Prefer vendor tags if present (SRNM/SRSQ)
    series = get_tag_any(tags, [K_SERIES_VENDOR])
    series_index_display = get_tag_any(tags, [K_INDEX_VENDOR]) or ""

    # 2) Fall back to free-form hints
    if not series:
        series = get_freeform_by_suffix(tags, FREEFORM_HINTS["series"])
    if not series_index_display:
        si_ff = get_freeform_by_suffix(tags, FREEFORM_HINTS["series_index"])
        if si_ff:
            series_index_display = _normalize_index(si_ff)

    # 3) Finally, conservative title parsing
    if not series or not series_index_display:
        ts, ti = parse_series_and_index_from_title(title)
        if not series and ts:
            series = ts
        if not series_index_display and ti:
            series_index_display = _normalize_index(ti)

    series_index_sort = _sort_key_for_index(series_index_display)

    # 4) Cover extraction (site-relative href)
    cover_href = _save_cover_for_file(path)

    # 5) Companion files (PDF, EPUB in same directory)
    companion_files = _find_companion_files(path)

    return {
        "title": title,
        "series": series or "",
        "series_index_display": series_index_display or "",
        "series_index_sort": "" if series_index_sort is None else str(series_index_sort),
        "author": author or "",
        "narrator": narrator or "",
        "year": year,
        "genre": genre,
        "duration_hhmm": sec_to_hhmm(length_sec),
        "cover_href": cover_href or "",
        "desc": desc,  # cleaned text
        "companion_files": companion_files,
        "file_mtime": path.stat().st_mtime,  # for "Recently Added" sorting
    }


def walk_library(root: Path, exts: set[str]):
    return [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in exts]
