# app/parsers/title.py
# Parsing logic that uses regex patterns from title_patterns.py

from typing import Optional, Tuple
import re
from app.core.index_utils import normalize_index
from app.parsers.title_patterns import build_title_patterns

# Precompile once at import time
_PATTERNS = build_title_patterns()

def _cleanup_series(name: Optional[str]) -> Optional[str]:
    if not name: return None
    s = re.sub(r"\bseries\b\s*$", "", name, flags=re.IGNORECASE).strip(" -–—:,")
    return re.sub(r"\s{2,}", " ", s).strip()

def parse_series_and_index_from_title(title: str) -> Tuple[Optional[str], Optional[str]]:
    if not title: return (None, None)
    for pat in _PATTERNS:
        m = pat.search(title)
        if not m:
            continue
        series = m.groupdict().get("series")
        idx    = m.groupdict().get("idx")
        if series:
            series = _cleanup_series(series)
        if idx:
            idx = normalize_index(idx)
        if series or idx:
            return (series or None, idx or None)
    return (None, None)
