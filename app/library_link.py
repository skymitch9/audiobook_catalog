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
match).

⚠️ **Folded, not exact — fixed 2026-08-14.** A byte-exact comparison against
this catalog's own `title` field only reached 37 of ~90 mapped pairs: the
cached `audiobookTitle` and this catalog's live `title` drift in DECORATION
(case, a curly quote, an "&" vs "and") without becoming a different book, and
an exact string test cannot tell that apart from an actual rename. The join
now folds BOTH sides through `app.core.review_join.normalise_title` — the
exact same fold `titles.ts::normaliseTitle` on the library side computes,
already the estate's one trusted identity fold (`work_key`, the review join).
This is not fuzzy matching: it is the identity fold every other cross-catalog
join in this estate already uses, applied here for the first time.

`foldedTitle` in each mapping row is that fold, computed ONCE on the library
side (`apps/worker/src/routes/audiobook-mapping.ts`) and sent as a plain
string so this side only ever compares strings, never re-derives the fold
from a title it does not own. ⚠️ **A `null`/missing `foldedTitle` is not a
degraded row — it is a collision tombstone.** When two mapping rows fold
identically, the library side withholds `foldedTitle` from both rather than
pick one arbitrarily; this side then falls back to the untouched exact-title
comparison for that row, and if that ALSO misses, the row is counted and
logged unmatched — never guessed at. Same posture, still "shown, never
hidden", the rule `OtherVersions.tsx` documents for the reverse direction.

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
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.config import SITE_CSV_NAME, SITE_DIR
from app.core.review_join import normalise_title

MAPPING_PATH = "/api/machine/audiobook-mapping"

# --------------------------------------------------------------------------- #
# Ported from library_catalog/packages/core/src/titles.ts, by hand — same
# posture as app/core/review_join.py's normalise_title/split_authors above it:
# there is no package shared across the two runtimes, so a change to the TS
# source must be mirrored here too. See titles.ts's own header on why a
# second fold implementation is how this estate's bugs start; this is that
# risk accepted deliberately, for the reason review_join.py's header names —
# "if a second language ever needs these rules again, bring the parity check
# back with it." (`test_library_link.py` is that check, run against real
# `catalog.csv` titles rather than a synthetic fixture.)
#
# ## Why THIS join needs it and normalise_title alone does not
#
# Measured 2026-08-14: `catalog.csv`'s own `title` column carries Audible's
# raw packaging — "Dungeon Born - Divine Dungeon Series, Book 1" — while the
# library's cached `audiobook_holding.title` is already CLEANED (the library
# side's `scripts/lib/audiobooks.mjs` applies this exact strip before it ever
# writes the holding). `normalise_title` folds case/punctuation/diacritics; it
# does not remove Audible packaging, so 53 of 90 mapped pairs still missed
# even after folding raw strings. Cleaning first — same as the library side
# already does to the SAME csv file — closes the gap to 88 of 90.
# --------------------------------------------------------------------------- #

_PART_OF_RE = re.compile(r"\s*[-–—:,]?\s*\bPart\s+\d+\s+of\s+\d+\b", re.IGNORECASE)
_DRAMATIZED_RE = re.compile(r"\s*[-–—:,]?\s*\bDramatized Adaptation\b", re.IGNORECASE)
_SERIES_BOOK_SUFFIX_RE = re.compile(
    r"\s*[-–—:]\s*[^,\-–—]*,\s*(Book|Volume|Vol\.?|Part)\s+[\w-]+\s*$", re.IGNORECASE
)
_BARE_BOOK_SUFFIX_RE = re.compile(r",\s*(Book|Volume|Vol\.?|Part)\s+[\w-]+\s*$", re.IGNORECASE)
_SERIES_BARE_NUM_RE = re.compile(r"\s*[-–—:]\s*[^,\-–—]*,\s*\d+(?:\.\d+)?\s*$")
_EMPTY_PARENS_RE = re.compile(r"\s*\(\s*\)")
_PAREN_VOLUME_RE = re.compile(
    r"\s*\(([^()]*?(Book|Volume|Vol\.?|Part)\s+[\w-]+|[^()]*Series[^()]*)\)\s*$", re.IGNORECASE
)
_MARKETING_TAIL_RE = re.compile(r"\s*[-–—:]\s*(A Novel|A Novella|Light Novel|Unabridged)\s*$", re.IGNORECASE)
_TRAILING_DASH_RE = re.compile(r"\s*[-–—:]\s*$")


def clean_audiobook_title(raw: Optional[str]) -> str:
    """
    Port of titles.ts::cleanAudiobookTitle. Strip Audible's title decoration
    down to what is printed on a book — see that function's header for the
    measurement (5/30 -> 14/30 Open Library hit rate) and why order matters:
    the series suffix must go before the parenthetical, or "Arc, Book 3)"
    survives inside the bracket.

    ⚠️ Does NOT strip a bare trailing number — "Summoner 6" is the title.
    """
    t = raw or ""
    t = _PART_OF_RE.sub("", t)
    t = _DRAMATIZED_RE.sub("", t)
    t = _SERIES_BOOK_SUFFIX_RE.sub("", t)
    t = _BARE_BOOK_SUFFIX_RE.sub("", t)
    t = _SERIES_BARE_NUM_RE.sub("", t)
    t = _EMPTY_PARENS_RE.sub("", t)
    t = _PAREN_VOLUME_RE.sub("", t)
    t = _MARKETING_TAIL_RE.sub("", t)
    return _TRAILING_DASH_RE.sub("", t).strip()


def clean_title_with_series(raw: Optional[str], series: Optional[str]) -> str:
    """
    Port of titles.ts::cleanTitleWithSeries. Prefer this over
    `clean_audiobook_title` alone whenever a series name is available —
    `catalog.csv` always carries one beside the title — because an exact
    strip of a KNOWN series name catches spellings the heuristic alone
    cannot (Audible writes the same suffix three different ways within one
    series; see that function's header for the measured example).

    ⚠️ Never returns empty: a standalone book whose title IS its series name
    would otherwise be reduced to nothing.
    """
    base = clean_audiobook_title(raw)
    if not series:
        return base
    escaped = re.escape(series.strip())
    if not escaped:
        return base
    suffix = re.compile(
        rf"\s*[-–—:,]\s*(?:The\s+)?{escaped}(?:\s*,?\s*(?:Book|Volume|Vol\.?|Part)?\s*[\w.-]+)?\s*$",
        re.IGNORECASE,
    )
    stripped = suffix.sub("", base).strip()
    return stripped if stripped else base


class LibraryLinkStats:
    """Coverage numbers for one run — reported honestly, not just logged."""

    def __init__(self) -> None:
        self.mapping_rows = 0
        self.stamped = 0
        # A link an EARLIER run wrote that this run's stricter collision
        # handling no longer supports — see `stamp_rows`'s docstring. Kept
        # apart from `stamped` because "we removed a wrong link" and "we
        # found nothing new" are different facts worth seeing separately.
        self.cleared = 0
        self.unmatched_titles: List[str] = []

    def summary(self) -> str:
        cleared = f"; {self.cleared} stale link(s) cleared" if self.cleared else ""
        return (
            f"{self.stamped} of {self.mapping_rows} library mapping row(s) matched a catalog "
            f"title; {len(self.unmatched_titles)} unmatched{cleared}"
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


def _row_fold(r: Dict[str, object]) -> str:
    """
    This catalog's own join key for one `catalog.csv` row: Audible's
    packaging stripped (`clean_title_with_series`, using the row's OWN
    `series` column — the same pairing `scripts/lib/audiobooks.mjs` reads
    from this exact file), then folded through the estate's one identity
    fold. Comparable directly against a mapping row's `foldedTitle`, which is
    `normaliseTitle(audiobookTitle)` computed on the library side — no
    cleaning step there because `audiobook_holding.title` is ALREADY clean
    (cached from this same pipeline, at match time).
    """
    title = str(r.get("title") or "").strip()
    if not title:
        return ""
    series = str(r.get("series") or "").strip() or None
    return normalise_title(clean_title_with_series(title, series))


def _build_by_title(mapping: List[Dict[str, object]]) -> Dict[str, Optional[Dict[str, object]]]:
    """
    Index the mapping by its raw `audiobookTitle`. `None` tombstones a title
    claimed by more than one DIFFERENT work — see `stamp_rows`'s docstring on
    why "the last one wins" stopped being safe once two works could share the
    identical cached title (Space Knight, 2026-08-14). `is not entry`
    (identity, not equality) is deliberate: two dicts can compare equal
    without being the SAME mapping row, and only genuinely distinct rows
    count as a collision.
    """
    by_title: Dict[str, Optional[Dict[str, object]]] = {}
    for m in mapping:
        title = str(m.get("audiobookTitle") or "").strip()
        if not title:
            continue
        entry = by_title.get(title)
        if title not in by_title:
            by_title[title] = m
            continue
        if entry is None:
            continue  # already tombstoned by an earlier pair; stays tombstoned
        if entry is m or entry.get("workId") == m.get("workId"):
            continue  # the same row (or the same work) seen twice, not a collision
        print(
            f"[WARN] Exact title collision in library mapping ({title!r}, "
            f"works {entry.get('workId')} and {m.get('workId')}); neither is guessed at",
            file=sys.stderr,
        )
        by_title[title] = None
    return by_title


def _build_by_folded(mapping: List[Dict[str, object]]) -> Dict[str, Optional[Dict[str, object]]]:
    """
    Index the mapping by the `foldedTitle` each row was sent with. A `None`
    tombstone means two DIFFERENT mapping rows folded identically — the
    library side is supposed to withhold `foldedTitle` from both rather than
    guess (see audiobook-mapping.ts), but this side never trusts that
    unconditionally: if a collision somehow reaches here anyway, it is caught
    and logged rather than silently letting the second row win.
    """
    by_folded: Dict[str, Optional[Dict[str, object]]] = {}
    for m in mapping:
        folded = str(m.get("foldedTitle") or "").strip()
        if not folded:
            continue
        if folded in by_folded:
            if by_folded[folded] is not None:
                print(
                    f"[WARN] Folded title collision in library mapping ({folded!r}); "
                    "both rows fall back to an exact title match",
                    file=sys.stderr,
                )
            by_folded[folded] = None  # tombstone: never match via the fold
            continue
        by_folded[folded] = m
    return by_folded


def _catalog_collision_folds(rows: List[Dict[str, object]]) -> set:
    """
    Which folds (see `_row_fold`) more than one `catalog.csv` row shares —
    the docstring on `stamp_rows` explains why that is refused rather than
    guessed at, the same as a mapping-side collision. Logged once, here,
    rather than per row.
    """
    counts: Counter = Counter(f for f in (_row_fold(r) for r in rows) if f)
    collisions = {f for f, n in counts.items() if n > 1}
    if collisions:
        print(
            f"[WARN] {len(collisions)} folded title collision(s) within catalog.csv itself "
            f"(falling back to exact title for each row involved): {sorted(collisions)}",
            file=sys.stderr,
        )
    return collisions


def stamp_rows(rows: List[Dict[str, object]], mapping: List[Dict[str, object]]) -> LibraryLinkStats:
    """
    Mutate `rows` in place: every row whose title, cleaned and folded (see
    `_row_fold`), matches a mapping row's `foldedTitle` gets
    `library_work_id` and `library_formats` (pipe-separated, same convention
    as `companion_files`) set. A row that misses the fold — or whose mapping
    counterpart has no `foldedTitle` at all, i.e. a collision the library side
    already declined to resolve, see the module docstring — falls back to the
    original exact-string comparison. A row with no match either way is left
    BLANK, never guessed at — and that includes clearing a link an earlier,
    less strict run may have written (see the ⚠️ below): "never a guess"
    describes what stays on disk, not only what gets written fresh.

    ⚠️ The fold can ALSO be ambiguous on THIS side, and that is not
    hypothetical: measured 2026-08-14, two live `catalog.csv` rows — the
    "Space Knight" volumes and a duplicate "Isles of the Emberdark" listing —
    fold identically to each other once cleaned, because the volume/series
    decoration that told them apart is exactly what `clean_title_with_series`
    removes. Matching either one via the fold would be a coin flip over which
    physical row "really" owns the one mapping entry — the wrong-book risk
    `titles.ts` warns about, just discovered on this side of the join instead
    of the library's. So a fold shared by more than one `catalog.csv` row is
    refused for ALL of them too, with the same tombstone-and-log-fallback
    treatment as a mapping-side collision.

    ⚠️ **The exact-title FALLBACK can be ambiguous too, and once
    `matching.ts`'s volume disambiguation (Space Knight, 2026-08-14) started
    resolving BOTH #249 and #250, it started happening for real**: two
    different works can cache the identical `audiobookTitle` — both hold the
    bare series name "Space Knight" — so "the last one wins" (this function's
    old, honest-enough-when-it-could-not-really-happen posture) would silently
    hand a plain "Space Knight" catalog row (this catalog's own volume-1
    listing, which needs no cleaning at all) to whichever work happened to
    sort last. That is a wrong link, not a missing one, so a raw-title
    collision is tombstoned exactly like a folded one instead.
    """
    stats = LibraryLinkStats()
    stats.mapping_rows = len(mapping)

    by_title = _build_by_title(mapping)
    by_folded = _build_by_folded(mapping)
    collision_folds = _catalog_collision_folds(rows)

    matched_titles: set = set()
    for r in rows:
        title = str(r.get("title") or "").strip()
        if not title:
            continue

        entry: Optional[Dict[str, object]] = None
        folded = _row_fold(r)
        if folded and folded not in collision_folds:
            candidate = by_folded.get(folded)
            if candidate is not None:
                entry = candidate
        if entry is None:
            entry = by_title.get(title)

        work_id = entry.get("workId") if entry else None
        if entry is None or work_id is None:
            # No confident match THIS run — which is not the same as "never
            # checked". An earlier, less strict run (see the docstring's ⚠️
            # above — this is exactly how the Space Knight wrong link was
            # found, still sitting in `catalog.csv` from before this fix
            # existed) may have written a link that this run's stricter
            # collision handling no longer supports. Cleared rather than
            # left in place: a wrong link silently outliving the logic that
            # produced it is worse than a blank one, and "never a guess" has
            # to apply to what stays on disk, not only to what gets written.
            if str(r.get("library_work_id") or "").strip():
                r["library_work_id"] = ""
                r["library_formats"] = ""
                stats.cleared += 1
            continue

        formats = entry.get("formats") or []
        r["library_work_id"] = str(work_id)
        r["library_formats"] = "|".join(str(f) for f in formats)
        stats.stamped += 1
        matched_titles.add(str(entry.get("audiobookTitle") or "").strip())

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
