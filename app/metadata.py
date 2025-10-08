import re
from pathlib import Path
from typing import Optional, Tuple, Dict, List

from mutagen.mp4 import MP4, MP4FreeForm

# MP4/iTunes atoms
K_TITLE   = "\xa9nam"     # Title
K_ARTIST  = "\xa9ART"     # Author(s)
K_WRITER  = "\xa9wrt"     # Narrator
K_DAY     = "\xa9day"     # Year/Date
K_GENRE   = "\xa9gen"     # Genre

# Vendor atoms (Audible-style)
K_SERIES_VENDOR = "SRNM"  # Series Name
K_INDEX_VENDOR  = "SRSQ"  # Series Sequence (e.g., 2.1)

FREEFORM_HINTS = {
    "series": ["series", "book series", "audible:series", "audible:seriesname"],
    "series_index": ["series index", "series_index", "audible:seriessequence", "series number", "series_no"],
}

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
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*[-–—]\s*(\d+(?:\.\d+)?)\s*$", t)
    if m: return f"{m.group(1)}-{m.group(2)}"
    if re.fullmatch(r"\d+(?:\.\d+)?", t): return t
    r = _roman_to_int(t)
    if r: return str(r)
    w = _word_to_num(t)
    if w: return str(w)
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

_PATTERNS = [
    re.compile(r"""
        (?:^.+?\s[-–—]\s)?
        (?P<series7>[^:()]+?)\s*:\s*
        (?:(?:book|bk\.?|volume|vol\.?|novella|part)s?\s*)
        (?P<idx7>[IVXLCM]+|\d+|[A-Za-z]+(?:\s+[A-Za-z]+)?)\b
    """, re.IGNORECASE | re.X),
    re.compile(r"""
        (?:^.+?\s[-–—]\s)?
        (?P<series9>[^:()]+?)\s*:\s*
        (?P<idx9>[IVXLCM]+|\d+|[A-Za-z]+(?:\s+[A-Za-z]+)?)\b
    """, re.IGNORECASE | re.X),
    re.compile(r""".+?\s[-–—]\s
                   (?P<series>[^,()]+?)\s*,\s*
                   (?:(?:book|bk\.?|volume|vol\.?|novella|part)s?\s*)
                   (?P<idx>[IVXLCM]+|\d+|[A-Za-z]+(?:\s+[A-Za-z]+)?(?:\s*[-–—]\s*\d+)?)
                   \b""", re.IGNORECASE | re.X),
    re.compile(r""".+?\(
                   (?P<series>[^()#]+?)\s*
                   (?:,)?\s*
                   (?:(?:book|bk\.?|volume|vol\.?|novella|part)\s*(?P<idx1>[IVXLCM]+|\d+|[A-Za-z]+)
                     |[#]\s*(?P<idx2>\d+|[IVXLCM]+|[A-Za-z]+))
                   \)""", re.IGNORECASE | re.X),
    re.compile(r""".+?\s[-–—]\s
                   (?:(?:book|bk\.?|volume|vol\.?|novella|part)\s*(?P<idx3>[IVXLCM]+|\d+|[A-Za-z]+))\s*
                   (?:of|in)\s+the\s+
                   (?P<series2>[^,()]+?)\s*$""", re.IGNORECASE | re.X),
    re.compile(r""".+?\s[-–—]\s
                   (?P<series3>[^,()]+?)\s*,\s*
                   (?:(?:book|bk\.?|volume|vol\.?|novella|part)s?)\s*(?P<idx4>\d+\s*[-–—]\s*\d+)
                   \b""", re.IGNORECASE | re.X),
    re.compile(r""".+?\((?P<series5>[^()#]+?)\s*[,#]?\s*#\s*(?P<idx6>\d+)\)""", re.IGNORECASE | re.X),
]

def _cleanup_series(name: Optional[str]) -> Optional[str]:
    if not name: return None
    s = re.sub(r"\bseries\b\s*$", "", name, flags=re.IGNORECASE).strip(" -–—:,")
    return re.sub(r"\s{2,}", " ", s).strip()

def parse_series_and_index_from_title(title: str) -> Tuple[Optional[str], Optional[str]]:
    if not title: return (None, None)
    for pat in _PATTERNS:
        m = pat.search(title)
        if not m: continue
        series = None; idx = None
        for g in ("series","series2","series3","series5","series7","series9"):
            if m.groupdict().get(g):
                series = _cleanup_series(m.group(g)); break
        for g in ("idx","idx1","idx2","idx3","idx4","idx6","idx7","idx9"):
            if m.groupdict().get(g):
                idx = _normalize_index(m.group(g)); break
        if series or idx: return (series, idx)
    return (None, None)

def normalize_people_field(s: Optional[str]) -> Optional[str]:
    if not s: return None
    import re as _re
    parts = _re.split(r"[;,/&]| and ", s, flags=_re.IGNORECASE)
    cleaned, seen = [], set()
    for p in parts:
        name = _re.sub(r"\s+", " ", p).strip()
        if not name: continue
        norm = name if (name.isupper() and len(name) <= 5) else " ".join(w.capitalize() for w in name.split())
        if norm.lower() not in seen:
            seen.add(norm.lower()); cleaned.append(norm)
    return ", ".join(cleaned) if cleaned else None

def sec_to_hhmm(s: Optional[int]) -> str:
    if s is None: return ""
    try: s = int(s)
    except Exception: return ""
    h = s // 3600; m = (s % 3600) // 60
    return f"{h}:{m:02d}"

def extract_metadata(path: Path) -> Dict[str, str]:
    audio = MP4(str(path))
    tags = audio.tags or {}
    duration = getattr(getattr(audio, "info", None), "length", None)
    length_sec = int(duration) if duration else None

    title    = get_tag_any(tags, [K_TITLE]) or ""
    author   = normalize_people_field(get_tag_any(tags, [K_ARTIST]))
    narrator = normalize_people_field(get_tag_any(tags, [K_WRITER]))
    year     = get_tag_any(tags, [K_DAY]) or ""
    genre    = get_tag_any(tags, [K_GENRE]) or ""

    series = get_tag_any(tags, [K_SERIES_VENDOR])
    raw_vendor_idx = get_tag_any(tags, [K_INDEX_VENDOR])
    series_index_display = raw_vendor_idx or ""

    if not series:
        series = get_freeform_by_suffix(tags, FREEFORM_HINTS["series"])
    if not series_index_display:
        si_ff = get_freeform_by_suffix(tags, FREEFORM_HINTS["series_index"])
        if si_ff: series_index_display = _normalize_index(si_ff)

    if not series or not series_index_display:
        ts, ti = parse_series_and_index_from_title(title)
        if not series and ts: series = ts
        if not series_index_display and ti: series_index_display = _normalize_index(ti)

    series_index_sort = _sort_key_for_index(series_index_display)

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
    }

def walk_library(root: Path, exts: set[str]):
    return [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in exts]
