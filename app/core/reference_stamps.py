# app/core/reference_stamps.py
"""
Stamps app/main.py applies to every row right after they're built: the
shared universe name (app/core/universes.py::universe_for) and the
per-series gap summary (app/core/series_gaps.py::compute_series_gaps).

Split out of app/main.py on purpose, mirroring app/library_link.py's
stamp_after_build / stamp_after_build_safe split and app/core/file_dedupe.py's
extraction — both done for the same reason: keeping main()'s own function
under the repo's flake8 C901 complexity ceiling by making this module's
try/except the only branching the call site adds.

Same fail-safe posture as everything else touching app/core/universes.py:
this pipeline runs unattended three times a day, so a reference-data problem
warns to stderr and the build continues with that field left "" on every
row — never a crash, never a guess.
"""
from __future__ import annotations

import sys
from typing import Any, Dict, List


def stamp_universe(rows: List[Dict[str, Any]]) -> None:
    """Mutates every row in place, adding row["universe"]."""
    from app.core.universes import universe_for

    for row in rows:
        row["universe"] = universe_for(title=row.get("title"), series=row.get("series")) or ""


def stamp_series_gaps(rows: List[Dict[str, Any]]) -> None:
    """Mutates every row in place, adding row["series_gap"]."""
    from app.core.series_gaps import compute_series_gaps

    gaps_by_series = compute_series_gaps(rows)
    for row in rows:
        row["series_gap"] = gaps_by_series.get((row.get("series") or "").strip(), "")


def stamp_reference_data_safe(rows: List[Dict[str, Any]]) -> None:
    """
    The pipeline hook — called by app/main.py once rows are extracted, BEFORE
    write_csv/stage_site_files so both stamps reach the CSV and the rendered
    HTML in the same build.

    Each stamp gets its OWN try/except, not one shared one: a broken
    catalog-platform checkout must not also take down series-gap computation,
    which is pure/local and has no external dependency at all, and vice versa.
    """
    try:
        stamp_universe(rows)
    except Exception as e:  # noqa: BLE001 — reference data must never stop a build
        print(f"[WARN] Universe stamping failed: {e}", file=sys.stderr)

    try:
        stamp_series_gaps(rows)
    except Exception as e:  # noqa: BLE001 — reference data must never stop a build
        print(f"[WARN] Series gap computation failed: {e}", file=sys.stderr)
