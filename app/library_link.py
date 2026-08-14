"""
Stamp library_catalog work links onto matching catalog rows — the audiobook
side of "Other versions available" (owner, 2026-08-14: every entry across
both sites is a hyperlink to the counterpart record, always labeled with the
format the media is in).

Design: mirrors app/index_push.py's failure posture exactly, in the opposite
direction. That module PUSHES this catalog's projection out to a shared
index; this one PULLS library_catalog's work-id + format mapping in and
stamps it onto rows this site already has.

Transport: GET <LIBRARY_MAPPING_URL>/api/machine/audiobook-mapping, bearer
LIBRARY_MAPPING_TOKEN. A narrow, read-only machine-token route on the library
Worker (that repo's apps/worker/src/routes/audiobook-mapping.ts) — same shape
as this repo's own app/index_push.py push route, in reverse. Answers 401
tokenless.

Join key: the mapping's `audiobookTitle` is library_catalog's own cached copy
of THIS catalog's title (its audiobook_holding.title, migration 0010 over
there — the last title the library's matcher saw when it confirmed the
match). An EXACT match against this catalog's own `title` field is expected:
that is the string the holding was matched FROM. A miss means the title has
drifted on one side since the holding was cached (an override, a rename) and
is counted and logged, never guessed at with a fuzzy match — the same "shown,
never hidden" rule that repo's OtherVersions.tsx documents for the reverse
direction.

Failure posture, matching app/index_push.py:
  - LIBRARY_MAPPING_URL / LIBRARY_MAPPING_TOKEN unset -> one log line, rows
    left untouched. The library link must never be able to stall this
    pipeline.
  - A fetch failure raises; app/main.py catches it and warns (the site build
    is already done by then).

Manual run — also the one-off backfill for the CURRENT catalog, without
re-walking the library (see app/tools/rebuild_site_html.py, which this reuses
for the HTML step):

    python -m app.library_link             # fetch, stamp site/catalog.csv,
                                            # re-render site/index.html
    python -m app.library_link --dry-run   # fetch + report coverage; write nothing
"""

from __future__ import annotations

import csv
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.config import SITE_CSV_NAME, SITE_DIR

MAPPING_PATH = "/api/machine/audiobook-mapping"


class LibraryLinkStats:
    """Coverage numbers for one run — reported honestly, not just logged."""

    def __init__(self) -> None:
        self.mapping_rows = 0
        self.stamped = 0
        self.unmatched_titles: List[str] = []

    def summary(self) -> str:
        return (
            f"{self.stamped} of {self.mapping_rows} library mapping row(s) matched a catalog "
            f"title; {len(self.unmatched_titles)} unmatched"
        )


def fetch_mapping(library_url: str, token: str, timeout: int = 30) -> List[Dict[str, object]]:
    """GET the machine-token mapping. Raises on any non-2xx — callers decide how loud to be."""
    import requests  # deferred so this module stays importable without it

    url = library_url.rstrip("/") + MAPPING_PATH
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
    if not resp.ok:
        raise RuntimeError(f"library mapping fetch failed: {resp.status_code} {resp.text[:500]}")
    data = resp.json()
    rows = data.get("rows") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise RuntimeError("library mapping response was not a list of rows")
    return rows


def stamp_rows(rows: List[Dict[str, object]], mapping: List[Dict[str, object]]) -> LibraryLinkStats:
    """
    Mutate `rows` in place: every row whose `title` exact-matches a mapping
    row's `audiobookTitle` gets `library_work_id` and `library_formats`
    (pipe-separated, same convention as `companion_files`) set. Rows with no
    match are left exactly as they were — never a guess.
    """
    stats = LibraryLinkStats()
    stats.mapping_rows = len(mapping)

    by_title: Dict[str, Dict[str, object]] = {}
    for m in mapping:
        title = str(m.get("audiobookTitle") or "").strip()
        if title:
            # A duplicate title in the mapping can't happen honestly — the
            # library side has one row per work_id — but if it ever does, the
            # last one wins rather than raising, matching this pipeline's
            # general "degrade, don't break the build" posture.
            by_title[title] = m

    matched_titles: set = set()
    for r in rows:
        title = str(r.get("title") or "").strip()
        entry = by_title.get(title)
        if not entry:
            continue
        work_id = entry.get("workId")
        if work_id is None:
            continue
        formats = entry.get("formats") or []
        r["library_work_id"] = str(work_id)
        r["library_formats"] = "|".join(str(f) for f in formats)
        stats.stamped += 1
        matched_titles.add(title)

    stats.unmatched_titles = sorted(t for t in by_title if t not in matched_titles)
    return stats


def stamp_after_build(rows: List[Dict[str, object]]) -> Optional[LibraryLinkStats]:
    """
    The pipeline hook's inner half — called by `stamp_after_build_safe`
    below. Split out so that function's try/except is the ONLY branching
    app/main.py's call site adds, keeping main() under the repo's complexity
    ceiling (see app/core/file_dedupe.py's header for the same reason it was
    extracted).
    """
    library_url = os.environ.get("LIBRARY_MAPPING_URL")
    token = os.environ.get("LIBRARY_MAPPING_TOKEN")
    if not library_url or not token:
        print("[INFO] Library link stamping skipped: LIBRARY_MAPPING_URL / LIBRARY_MAPPING_TOKEN not set")
        return None

    mapping = fetch_mapping(library_url, token)
    stats = stamp_rows(rows, mapping)
    print(f"[INFO] Library link stamping OK: {stats.summary()}")
    if stats.unmatched_titles:
        print(f"[INFO] Unmatched library titles: {stats.unmatched_titles}")
    return stats


def stamp_after_build_safe(rows: List[Dict[str, object]]) -> Optional[LibraryLinkStats]:
    """
    The pipeline hook — called by app/main.py once rows are extracted and
    BEFORE write_csv/stage_site_files, so the stamp reaches the CSV and the
    rendered HTML in the same build. Fail-soft, matching app/index_push.py's
    push_after_build: the library link must never be able to stall this
    pipeline, so any exception (network, bad response, …) warns and the build
    proceeds without the stamp.
    """
    try:
        return stamp_after_build(rows)
    except Exception as e:  # noqa: BLE001 — the library link must never stall this pipeline
        print(f"[WARN] Library link stamping failed (site build unaffected): {e}", file=sys.stderr)
        return None


def _load_csv(csv_path: Path) -> Tuple[List[Dict[str, str]], List[str]]:
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader), list(reader.fieldnames or [])


def _write_csv(rows: List[Dict[str, str]], fieldnames: List[str], csv_path: Path) -> None:
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m app.library_link",
        description=(
            "Fetch library_catalog's work-id/format mapping, stamp site/catalog.csv, "
            "and re-render site/index.html from it (no library walk, no uploads)."
        ),
    )
    parser.add_argument("--csv", type=Path, default=SITE_DIR / SITE_CSV_NAME)
    parser.add_argument("--dry-run", action="store_true", help="fetch and report coverage; write nothing")
    args = parser.parse_args(argv)

    library_url = os.environ.get("LIBRARY_MAPPING_URL")
    token = os.environ.get("LIBRARY_MAPPING_TOKEN")
    if not library_url or not token:
        print("[INFO] Library link stamping skipped: LIBRARY_MAPPING_URL / LIBRARY_MAPPING_TOKEN not set")
        return 0

    if not args.csv.exists():
        print(f"[ERROR] catalog not found: {args.csv}", file=sys.stderr)
        return 2

    rows, fieldnames = _load_csv(args.csv)

    try:
        mapping = fetch_mapping(library_url, token)
    except Exception as e:  # noqa: BLE001 — the manual runner is the loud path
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1

    stats = stamp_rows(rows, mapping)
    print(f"[INFO] {stats.summary()}")
    if stats.unmatched_titles:
        print(f"[INFO] Unmatched library titles ({len(stats.unmatched_titles)}):")
        for t in stats.unmatched_titles:
            print(f"  - {t}")

    if args.dry_run:
        print("[INFO] dry run: nothing written")
        return 0

    out_fieldnames = list(fieldnames)
    for extra in ("library_work_id", "library_formats"):
        if extra not in out_fieldnames:
            out_fieldnames.append(extra)
    _write_csv(rows, out_fieldnames, args.csv)
    print(f"[INFO] Wrote {args.csv}")

    from app.tools.rebuild_site_html import main as rebuild_main

    return rebuild_main([])


if __name__ == "__main__":
    raise SystemExit(main())
