# app/parsers/title.py
# Parsing logic that uses regex patterns from title_patterns.py

import re
from typing import Optional, Tuple

from app.core.index_utils import normalize_index
from app.parsers.title_patterns import build_exclusion_patterns, build_title_patterns

# Precompile once at import time
_PATTERNS = build_title_patterns()
_EXCLUSIONS = build_exclusion_patterns()


def _cleanup_series(name: Optional[str]) -> Optional[str]:
    """Clean up series name by removing common suffixes and extra whitespace."""
    if not name:
        return None

    # Remove common series suffixes
    s = re.sub(r"\bseries\b\s*$", "", name, flags=re.IGNORECASE).strip(" -–—:,")

    # Normalize whitespace
    s = re.sub(r"\s{2,}", " ", s).strip()

    # Don't return very short or generic series names
    if len(s) <= 2 or s.lower() in ["a", "an", "the", "of", "in", "on", "at", "to", "for", "with"]:
        return None

    return s


def _is_excluded_title(title: str) -> bool:
    """Check if title matches exclusion patterns (not a series book)."""
    for pattern in _EXCLUSIONS:
        if pattern.search(title):
            return True
    return False


def _validate_series_match(series: str, index: str, title: str) -> bool:
    """Validate that a series/index match makes sense."""
    if not series or not index:
        return False

    # Series name should be substantial
    if len(series.strip()) < 3:
        return False

    # Index should be reasonable
    try:
        idx_num = float(normalize_index(index))
        if idx_num < 0 or idx_num > 100:  # Reasonable bounds for series numbers
            return False
    except (ValueError, TypeError):
        # Non-numeric indices are okay (like "Prequel", "Epilogue", etc.)
        pass

    # Series name shouldn't be too similar to common non-series words
    series_lower = series.lower().strip()
    if series_lower in [
        "movie tie",
        "tv tie",
        "special edition",
        "deluxe edition",
        "collector edition",
        "anniversary edition",
        "unabridged",
        "abridged",
    ]:
        return False

    return True


def parse_series_and_index_from_title(title: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Parse series name and index from title using regex patterns.
    Returns (series_name, index) or (None, None) if no match.
    """
    if not title:
        return (None, None)

    # Check exclusion patterns first
    if _is_excluded_title(title):
        return (None, None)

    for pat in _PATTERNS:
        m = pat.search(title)
        if not m:
            continue

        series = m.groupdict().get("series")
        idx = m.groupdict().get("idx")

        if series:
            series = _cleanup_series(series)
        if idx:
            idx = normalize_index(idx)

        # Validate the match
        if _validate_series_match(series or "", idx or "", title):
            return (series, idx)

    return (None, None)
