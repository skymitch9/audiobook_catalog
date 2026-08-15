# app/core/series_gaps.py
"""
Per-series gap summaries: which numbered volumes within a series this
catalog owns, and where the holes are BETWEEN them.

Deliberately does NOT claim a series total. This catalog has no way to know
how many volumes a series actually has — only sync_series_canon.py's manual
canon and the shared universes list get that kind of outside knowledge, and
neither is consulted here. "Volumes 1-6, 8 owned - gap: 7" is honest;
"8 of 12" is a guess this module refuses to make, ever.

Cross-format availability - whether a gap volume happens to exist in
library_catalog - is explicitly OUT OF SCOPE. That's app/library_link.py's
"Other versions available" stamp (library_work_id / library_formats), and it
owns its own matching/collision logic. This module never touches it.

Pure and testable: no I/O, no Firestore, no dependency on library_link.py or
universes.py. Grouping is on the exact `series` string this catalog stores -
no canonicalization is attempted here (that's sync_series_canon.py's job; a
second, silent canonicalization here would risk splitting or merging series
the manual tool treats as distinct).
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List

# Matches "3-4", "3 - 4", "3–4" (en dash), "3—4" (em dash) - the same shapes
# app/core/index_utils.py::normalize_index()/sort_key_for_index() recognise.
_RANGE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[-–—]\s*(\d+(?:\.\d+)?)\s*$")

# A ranged index wider than this is almost certainly a mis-tagged value (a
# date, an ISBN fragment) rather than a real omnibus span - refuse to expand
# it rather than allocate an enormous "owned" list.
_MAX_RANGE_WIDTH = 50


def _owned_numbers_for_row(display: str) -> List[float]:
    """
    The volume number(s) ONE row's series_index_display contributes.

    ⚠️ Documented interpretation of a ranged index like "3-4":
    app/core/index_utils.py::sort_key_for_index() returns only the low end
    (3.0) for these, because it exists to give the table a single numeric
    sort key. Reusing that alone here would silently under-count - a row
    tagged "3-4" (an omnibus/bind-up) really does mean this catalog owns
    both 3 and 4. So this function reparses series_index_display itself and
    treats "3-4" as owning EVERY whole number from 3 through 4 inclusive.
    An egregiously wide range (see _MAX_RANGE_WIDTH) is treated as unparsable
    instead, to avoid a mistagged value inflating the owned set.

    Anything that isn't a clean number or number-range - blank, "N/A", free
    text - contributes nothing. Never raises.
    """
    if not display:
        return []
    s = display.strip()
    if not s:
        return []

    m = _RANGE_RE.match(s)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if lo == int(lo) and hi == int(hi) and lo <= hi and (hi - lo) <= _MAX_RANGE_WIDTH:
            return [float(n) for n in range(int(lo), int(hi) + 1)]
        # Not a sane integer span (reversed, fractional, or absurdly wide) -
        # fall back to just the two named endpoints rather than nothing.
        return [lo, hi]

    try:
        return [float(s)]
    except ValueError:
        return []


def _format_number(n: float) -> str:
    return str(int(n)) if n == int(n) else str(n)


def _format_run(numbers: List[float]) -> str:
    """
    Collapse a sorted list of numbers into "1-6, 8" style ranges. Only
    consecutive INTEGERS (n, n+1, n+2, ...) collapse into a dash-range; any
    other gap between neighbours (fractional indices, non-consecutive
    integers) starts a new segment.
    """
    if not numbers:
        return ""
    parts: List[str] = []
    start = prev = numbers[0]
    for n in numbers[1:]:
        if n - prev == 1 and n == int(n) and prev == int(prev):
            prev = n
            continue
        parts.append(_format_number(start) if start == prev else f"{_format_number(start)}-{_format_number(prev)}")
        start = prev = n
    parts.append(_format_number(start) if start == prev else f"{_format_number(start)}-{_format_number(prev)}")
    return ", ".join(parts)


def compute_series_gaps(rows: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    """
    One human-readable gap summary per series, keyed on the exact `series`
    string as stored on the rows.

    Only the WHOLE-NUMBER volumes between the lowest and highest owned
    number are considered when looking for gaps - e.g. owning 1, 2, 3, 4, 5,
    6, 8 reports gap "7"; a stray fractional index (a novella at "1.5") can
    never itself BE a gap, since gaps are only ever whole numbers, but it is
    still shown as owned.

    A series contributes NO entry (not even a "complete" one) when it has
    fewer than two distinct numeric indices - one book, or every book's
    index is blank/unparsable, is nothing to report a gap against. When the
    owned run has no gap at all, the phrasing is "Volumes X-Y owned" (no
    em-dash gap suffix) rather than omitting the summary, so a fully-owned
    series still gets a positive confirmation.
    """
    by_series: Dict[str, List[float]] = {}
    for row in rows:
        series = (row.get("series") or "").strip()
        if not series:
            continue
        numbers = _owned_numbers_for_row(row.get("series_index_display") or "")
        if numbers:
            by_series.setdefault(series, []).extend(numbers)

    result: Dict[str, str] = {}
    for series, raw_numbers in by_series.items():
        owned = sorted(set(raw_numbers))
        if len(owned) < 2:
            continue

        lo, hi = owned[0], owned[-1]
        owned_str = _format_run(owned)

        if lo == int(lo) and hi == int(hi):
            full_span = set(range(int(lo), int(hi) + 1))
            owned_whole = {int(n) for n in owned if n == int(n)}
            missing = sorted(float(n) for n in (full_span - owned_whole))
        else:
            # Fractional bounds (e.g. the series' first tagged index is a
            # "0.5" prequel novella) can't anchor a whole-number gap scan -
            # there is no missing set to compute, only what's owned.
            missing = []

        if missing:
            result[series] = f"Volumes {owned_str} owned — gap: {_format_run(missing)}"
        else:
            result[series] = f"Volumes {owned_str} owned"

    return result
