# app/metadata.py
# Read MP4/M4B tags with Mutagen and (only if needed) parse series/index from title.
# Also extracts embedded cover art and writes it under OUTPUT_DIR/covers/..., exposing
# a site-relative href for the HTML renderer.

import re
from pathlib import Path
from typing import Optional, Tuple, Dict, List

from mutagen.mp4 import MP4, MP4FreeForm, MP4Cover

# Import config (paths used for cover extraction output)
from app.config import ROOT_DIR, OUTPUT_DIR

# ---------- Tag keys ----------
# MP4/iTunes atoms
K_TITLE   = "\xa9nam"     # Title
K_ARTIST  = "\xa9ART"     # Author(s)
K_WRITER  = "\xa9wrt"     # Narrator
K_DAY     = "\xa9day"     # Year/Date
K_GENRE   = "\xa9gen"     # Genre

# Vendor atoms (Audible-style that you provided)
K_SERIES_VENDOR = "SRNM"  # Series Name
K_INDEX_VENDOR  = "SRSQ"  # Series Sequence (e.g., 2.1)

# Free-form keys (----:com.apple.iTunes:*)
FREEFORM_HINTS = {
    "series": ["series", "book series", "audible:series", "audible:seriesname"],
    "series_index": ["series index", "series_index", "audible:seriessequence", "series number", "series_no"],
}

# ---------- helpers for index normalization ----------
_WORD_NUM = {
    "one":1,"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,"eight":8,"nine":9,"ten":10,
    "eleven":11,"twelve":12,"thirteen":13,"fourteen":14,"fifteen":15,"sixteen":16,"seventeen":17,
    "eighteen":18,"nineteen":19,"twenty":20,"thirty":30
}
_ROMAN_MAP = {'I':1,'V':5,'X':10,'L':50,'C':100,'D':500,'M':1000}

def _word_to_num(tok: str) -> Optional[int]:
    t = tok.lower().strip().replace("-", " ")
    if t in _WORD_NUM: return _WORD_NUM[t]
    parts = t.split()
    if len(parts) == 2 and parts[0] in _WORD_NUM and parts[1] in _WORD_NUM and _WORD_NUM[parts[0]] % 10 == 0:
        return _WORD_NUM[parts[0]] + _WORD_NUM[parts[1]]
    return None

def _roman_to_int(s: str) -> Optional[int]:
    s = s.upper()
    if not s or not all(c in _ROMAN_MAP for c in s): return None
    total, prev = 0, 0
    for ch in reversed(s):
        val = _ROMAN_MAP[ch]
        if val < prev: total -= val
        else: total += val; prev = val
    return total

def _normalize_index(tok: str) -> str:
    if not tok: return ""
    t = tok.strip()
    # allow numeric ranges e.g. "1-3"
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*[-–—]\s*(\d+(?:\.\d+)?)\s*$", t)
    if m: return f"{m.group(1)}-{m.group(2)}"
    # integer/decimal
    if re.fullmatch(r"\d+(?:\.\d+)?", t): return t
    # roman
    r = _roman_to_int(t)
    if r is not None: return str(r)
    # word(s)
    w = _word_to_num(t)
    if w is not None: return str(w)
    return t

def _sort_key_for_index(display_val: str) -> Optional[float]:
    if not display_val: return None
    s = display_val.strip()
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*[-–—]\s*(\d+(?:\.\d+)?)\s*$", s)
    if m:
        try: return float(m.group(1))
        except Exception: return None
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        try: return float(s)
        except Exception: return None
    r = _roman_to_int(s)
    if r is not None: return float(r)
    w = _word_to_num(s)
    if w is not None: return float(w)
    return None

# ---------- tag access helpers ----------
def bytes_to_str(b: bytes) -> str:
    for enc in ("utf-8","utf-16","latin-1"):
        try: return b.decode(enc).strip()
        except Exception: pass
    return b.decode("utf-8", errors="ignore").strip()

def first_str(val):
    v = val[0] if isinstance(val, list) and val else val
    if isinstance(v, bytes): return bytes_to_str(v)
    if isinstance(v, tuple): return str(v[0])
    return (str(v).strip()) if v is not None else None

def get_tag_any(tags: Dict, keys: List[str]) -> Optional[str]:
    for k in keys:
        if k in tags and tags[k]:
            s = first_str(tags[k])
            if s: return s
    return None

def get_freeform_by_suffix(tags: Dict, suffixes: List[str]) -> Optional[str]:
    for key, val in (tags or {}).items():
        if not isinstance(key, str) or not key.startswith("----"): continue
        tail = key.split(":")[-1].lower()
        if any(tail.endswith(sfx.lower()) for sfx in suffixes):
            if isinstance(val, list) and val:
                parts = []
                for piece in val:
                    if isinstance(piece, MP4FreeForm): parts.append(bytes_to_str(bytes(piece)))
                    elif isinstance(piece, (bytes, bytearray)): parts.append(bytes_to_str(piece))
                    else: parts.append(str(piece))
                joined = ", ".join(p for p in parts if p)
                if joined.strip(): return joined.strip()
    return None

def _cleanup_series(name: Optional[str]) -> Optional[str]:
    if not name: return None
    s = re.sub(r"\bseries\b\s*$", "", name, flags=re.IGNORECASE).strip(" -–—:,")
    return re.sub(r"\s{2,}", " ", s).strip()

def normalize_people_field(s: Optional[str]) -> Optional[str]:
    if not s: return None
    parts = re.split(r"[;,/&]| and ", s, flags=re.IGNORECASE)
    cleaned, seen = [], set()
    for p in parts:
        name = re.sub(r"\s+", " ", p).strip()
        if not name: continue
        norm = name if (name.isupper() and len(name) <= 5) else " ".join(w.capitalize() for w in name.split())
        key = norm.lower()
        if key not in seen:
            seen.add(key); cleaned.append(norm)
    return ", ".join(cleaned) if cleaned else None

def sec_to_hhmm(s: Optional[int]) -> str:
    if s is None: return ""
    try: s = int(s)
    except Exception: return ""
    h = s // 3600; m = (s % 3600) // 60
    return f"{h}:{m:02d}"

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

# ---------- minimal, conservative fallback patterns ----------
# Only used if SRNM/SRSQ and free-form series tags don't yield a result.
_PATTERNS = [
    # 1) "Series (Volume 1)" / "Series (Book 2)" / "Series (Part IV)"
    re.compile(r"""
        ^\s*
        (?P<series>.+?)
        \s*\(\s*
        (?:(?:book|bk\.?|volume|vol\.?|novella|part)\s*)
        (?P<idx>[IVXLCM]+|\d+|[A-Za-z]+(?:\s+[A-Za-z]+)?)
        \s*\)
        \s*$
    """, re.IGNORECASE | re.X),

    # 2) "Title (Series #3)"
    re.compile(r"""
        .+?\(
        (?P<series>[^()#]+?)\s*[,#]?\s*#\s*
        (?P<idx>\d+|[IVXLCM]+|[A-Za-z]+(?:\s+[A-Za-z]+)?)
        \)
    """, re.IGNORECASE | re.X),

    # 3) "Title – Series, Book 3"
    re.compile(r"""
        .+?\s[-–—]\s
        (?P<series>[^,()]+?)\s*,\s*
        (?:(?:book|bk\.?|volume|vol\.?|novella|part)s?\s*)
        (?P<idx>[IVXLCM]+|\d+|[A-Za-z]+(?:\s+[A-Za-z]+)?(?:\s*[-–—]\s*\d+)?)
        \b
    """, re.IGNORECASE | re.X),

    # 4) Colon w/ keyword — "Title - Series: Novella 1" / "Series: Book 3"
    re.compile(r"""
        (?:^.+?\s[-–—]\s)?     # optional "Title – "
        (?P<series>[^:()]+?)\s*:\s*
        (?:(?:book|bk\.?|volume|vol\.?|novella|part)s?\s*)
        (?P<idx>[IVXLCM]+|\d+|[A-Za-z]+(?:\s+[A-Za-z]+)?)
        (?:\b|$)
    """, re.IGNORECASE | re.X),

    # 5) Minimal colon — "Title - Series: 3"
    re.compile(r"""
        (?:^.+?\s[-–—]\s)?
        (?P<series>[^:()]+?)\s*:\s*
        (?P<idx>[IVXLCM]+|\d+|[A-Za-z]+(?:\s+[A-Za-z]+)?)
        (?:\b|$)
    """, re.IGNORECASE | re.X),

    # 6) "Title - Book Five of the Stormlight Archive"
    re.compile(r"""
        .+?\s[-–—]\s
        (?:(?:book|bk\.?|volume|vol\.?|novella|part)\s*(?P<idx>[IVXLCM]+|\d+|[A-Za-z]+))\s*
        (?:of|in)\s+the\s+
        (?P<series>[^,()]+?)\s*$
    """, re.IGNORECASE | re.X),
]

def parse_series_and_index_from_title(title: str) -> Tuple[Optional[str], Optional[str]]:
    if not title:
        return (None, None)
    for pat in _PATTERNS:
        m = pat.search(title)
        if not m:
            continue
        series = m.groupdict().get("series")
        idx    = m.groupdict().get("idx")
        if series:
            series = _cleanup_series(series)
        if idx:
            idx = _normalize_index(idx)
        if series or idx:
            return (series or None, idx or None)
    return (None, None)

# ---------- high-level extractors ----------
def extract_metadata(path: Path) -> Dict[str, str]:
    """Extracts metadata from an MP4/M4B file, preferring SRNM/SRSQ,
    then free-form tags, and finally conservative title parsing. Also saves cover."""
    audio = MP4(str(path))
    tags = audio.tags or {}
    duration = getattr(getattr(audio, "info", None), "length", None)
    length_sec = int(duration) if duration else None

    title    = get_tag_any(tags, [K_TITLE]) or ""
    author   = normalize_people_field(get_tag_any(tags, [K_ARTIST]))
    narrator = normalize_people_field(get_tag_any(tags, [K_WRITER]))
    year     = get_tag_any(tags, [K_DAY]) or ""
    genre    = get_tag_any(tags, [K_GENRE]) or ""

    # 1) Prefer vendor tags if present (SRNM/SRSQ)
    series = get_tag_any(tags, [K_SERIES_VENDOR])
    series_index_display = get_tag_any(tags, [K_INDEX_VENDOR]) or ""

    # 2) Fall back to free-form hints
    if not series:
        series = get_freeform_by_suffix(tags, FREEFORM_HINTS["series"])
    if not series_index_display:
        si_ff = get_freeform_by_suffix(tags, FREEFORM_HINTS["series_index"])
        if si_ff: series_index_display = _normalize_index(si_ff)

    # 3) Finally, conservative title parsing
    if not series or not series_index_display:
        ts, ti = parse_series_and_index_from_title(title)
        if not series and ts: series = ts
        if not series_index_display and ti: series_index_display = _normalize_index(ti)

    series_index_sort = _sort_key_for_index(series_index_display)

    # 4) Cover extraction (site-relative href)
    cover_href = _save_cover_for_file(path)

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

def walk_library(root: Path, exts: set[str]):
    return [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in exts]
