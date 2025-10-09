# app/metadata.py
# Minimal facade for metadata extraction + directory walking.

from pathlib import Path
from typing import Dict, List, Optional
from mutagen.mp4 import MP4

from app.config import ROOT_DIR, OUTPUT_DIR  # ROOT_DIR used by walk_library elsewhere

from app.core.keys import (
    K_TITLE, K_ARTIST, K_WRITER, K_DAY, K_GENRE,
    K_SERIES_VENDOR, K_INDEX_VENDOR, FREEFORM_HINTS,
)
from app.core.people import normalize_people_field
from app.core.index_utils import normalize_index, sort_key_for_index
from app.extractors.tags import get_tag_any, get_freeform_by_suffix
from app.extractors.covers import save_cover_for_file
from app.parsers.title import parse_series_and_index_from_title

def sec_to_hhmm(s: Optional[int]) -> str:
    if s is None: return ""
    try: s = int(s)
    except Exception: return ""
    h = s // 3600; m = (s % 3600) // 60
    return f"{h}:{m:02d}"

def extract_metadata(path: Path) -> Dict[str, str]:
    """Prefer SRNM/SRSQ tags; then free-form hints; finally conservative title parsing. Also saves cover."""
    audio = MP4(str(path))
    tags = audio.tags or {}
    duration = getattr(getattr(audio, "info", None), "length", None)
    length_sec = int(duration) if duration else None

    title    = get_tag_any(tags, [K_TITLE]) or ""
    author   = normalize_people_field(get_tag_any(tags, [K_ARTIST]))
    narrator = normalize_people_field(get_tag_any(tags, [K_WRITER]))
    year     = get_tag_any(tags, [K_DAY]) or ""
    genre    = get_tag_any(tags, [K_GENRE]) or ""

    # 1) Vendor tags
    series = get_tag_any(tags, [K_SERIES_VENDOR])
    series_index_display = get_tag_any(tags, [K_INDEX_VENDOR]) or ""

    # 2) Free-form
    if not series:
        series = get_freeform_by_suffix(tags, FREEFORM_HINTS["series"])
    if not series_index_display:
        si_ff = get_freeform_by_suffix(tags, FREEFORM_HINTS["series_index"])
        if si_ff: series_index_display = normalize_index(si_ff)

    # 3) Title parsing
    if not series or not series_index_display:
        ts, ti = parse_series_and_index_from_title(title)
        if not series and ts: series = ts
        if not series_index_display and ti: series_index_display = normalize_index(ti)

    series_index_sort = sort_key_for_index(series_index_display)

    # 4) Cover extraction (site-relative href)
    cover_href = save_cover_for_file(path)

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
    }

def walk_library(root: Path, exts: set[str]) -> List[Path]:
    return [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in exts]
