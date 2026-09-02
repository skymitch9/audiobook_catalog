"""
Build site/shelf_book_map.json — the data half of the catalog->Audiobookshelf join.

⚠️ THE CODE HALF IS `site/shelf-link.js`, AND IT OWNS THE RULES.
This script only produces data; every decision about what a link LOOKS like
lives there. The one rule that binds both is the slug, and
`tests/test_shelf_map.py` pins `book_id_from_title` here against
`bookIdFromTitle` in `site/reviews.js`.

WHAT CHANGED, 2026-09-02, AND WHY
---------------------------------
This script used to write `{"<slug>": "<abs-item-uuid>"}` and the site turned
those uuids into `/audiobookshelf/item/<uuid>` deep links.

🔴 ABS item ids are NOT stable. Measured 2026-08-21: every id from the
2026-08-20 flat layout returned 404 after the hardlink reshape. Re-measured
2026-09-02: 0 of the shipped 1,077 were stale — which measures only that the
map had been regenerated since the reshape, not that ids became durable.
Nothing in the pipeline regenerates it, so the rot is one
`02-abs-hardlinks.sh` re-run away, and a dead link is worse than an absent one.

So the map no longer carries ids at all. It carries the ABS-side TITLE, and the
site builds an ABS **search**, which cannot 404. Measured 2026-09-02 over a
random 60-item sample: searching the ABS title returned the intended item FIRST
for 57 books and inside the top 10 for the other 3 — nothing was not found.

OUTPUT SHAPE
------------
    {
      "generatedAt":    "2026-09-02T18:00:00Z",   <- staleness is visible
      "libraryId":      "<Audio library id>",
      "ebookLibraryId": "<Ebooks library id>" | null,
      "books": { "<catalog slug>": { "t": "<ABS title>", "m": "audio"|"ebook"|"both" } }
    }

⚠️ `site/shelf-link.js` also reads the OLD flat shape, so the two deploy
independently and a cached browser does not lose its buttons.

MATCHING
--------
Two passes, unchanged and deliberately kept — they are the expensive part:
  1. exact slug match (`book_id_from_title` on both sides);
  2. fuzzy multi-signal scoring for the rest — cleaned title, author, narrator,
     duration and series, needing a combined score of 5 before it will claim a
     match, and never letting two catalog books claim the same ABS item.

USAGE
-----
    .venv\\Scripts\\python scripts/build_shelf_map.py --dry-run
    .venv\\Scripts\\python scripts/build_shelf_map.py --dry-run --verbose
    .venv\\Scripts\\python scripts/build_shelf_map.py

Exit 0 = success. Exit 1 = fatal (cannot login, missing env vars, no catalog).
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import requests

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
CATALOG_CSV: Path = PROJECT_ROOT / "site" / "catalog.csv"
SHELF_MAP_PATH: Path = PROJECT_ROOT / "site" / "shelf_book_map.json"

ABS_BASE_URL: str = os.getenv("ABS_BASE_URL", "")
ABS_USERNAME: str = os.getenv("ABS_USERNAME", "")
ABS_PASSWORD: str = os.getenv("ABS_PASSWORD", "")
ABS_LIBRARY_ID: str = os.getenv("ABS_LIBRARY_ID", "")
ABS_CF_CLIENT_ID: str = os.getenv("ABS_CF_CLIENT_ID", "")
ABS_CF_CLIENT_SECRET: str = os.getenv("ABS_CF_CLIENT_SECRET", "")

#: ⚠️ The Ebooks library does not exist yet — option A (owner's choice,
#: 2026-09-02) creates it over the tree that `04-ebook-hardlinks.sh` builds.
#: Runbook: docs/access/SHELF_EBOOKS_LIBRARY.md. Until the owner creates it and
#: sets this, every ebook stays filed under the Audio library, which is correct
#: rather than degraded: the 132 ebook-only items live there today.
ABS_EBOOK_LIBRARY_ID: str = os.getenv("ABS_EBOOK_LIBRARY_ID", "")

FUZZY_MATCH_THRESHOLD: int = 5


# ---------------------------------------------------------------------------
# book_id_from_title — the cross-language twin of bookIdFromTitle in
# site/reviews.js. ⚠️ Two implementations exist ON PURPOSE (one per language)
# and they must agree exactly, or the map keys stop matching the slugs the site
# computes and every button silently disappears. tests/test_shelf_map.py pins
# them together against a shared table of cases.
# ---------------------------------------------------------------------------
def book_id_from_title(title: str) -> str:
    """Lowercase, non-alphanumerics to hyphens, collapse runs, trim ends."""
    slug = (title or "").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug)
    return slug.strip("-")


# ---------------------------------------------------------------------------
# Title cleaning for fuzzy matching
# ---------------------------------------------------------------------------
def clean_title(title: str) -> str:
    """Strip common audiobook title decorations for comparison."""
    t = title
    t = re.sub(r"\s*-\s*[A-Z][a-z]+ [A-Z][a-z]+$", "", t)
    t = re.sub(
        r"\s*[\(\[](Dramatized Adaptation|Unabridged|Abridged|GraphicAudio)[)\]]",
        "", t, flags=re.IGNORECASE,
    )
    t = re.sub(r"\s*-\s*(Dramatized Adaptation|GraphicAudio)$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*[\(\[]?Part \d+( of \d+)?[\)\]]?$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*-\s*.+,\s*Book\s*\d+$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*\(.+#\d+\)$", "", t)
    return t.strip()


# ---------------------------------------------------------------------------
# Fuzzy matching helpers
# ---------------------------------------------------------------------------
def duration_hhmm_to_seconds(hhmm: str) -> Optional[float]:
    """Convert "HH:MM" to seconds; None when it is not that shape."""
    if not hhmm:
        return None
    parts = hhmm.split(":")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]) * 3600 + int(parts[1]) * 60
    except ValueError:
        return None


def normalize_author(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def narrators_set(narrator_str: str) -> set:
    if not narrator_str:
        return set()
    parts = re.split(r"[,&]", narrator_str)
    return {re.sub(r"\s+", " ", n.strip().lower()) for n in parts if n.strip()}


def score_fuzzy_match(catalog_row: Dict[str, str], abs_item: Dict) -> Tuple[int, float, List[str]]:
    """Score an ABS item against a catalog row. Returns (score, dur_diff, signals)."""
    score = 0
    signals: List[str] = []
    duration_diff_pct = 1.0

    metadata = abs_item.get("media", {}).get("metadata", {})

    cat_clean = clean_title((catalog_row.get("title") or "").strip())
    abs_clean = clean_title((metadata.get("title") or "").strip())
    cat_slug = book_id_from_title(cat_clean)
    abs_slug = book_id_from_title(abs_clean)

    if cat_slug and abs_slug and cat_slug == abs_slug:
        score += 5
        signals.append(f"clean_title_slug({cat_slug})")
    elif cat_slug and abs_slug and (cat_slug in abs_slug or abs_slug in cat_slug):
        score += 3
        signals.append(f"title_contains({cat_slug[:30]})")

    cat_author = normalize_author(catalog_row.get("author") or "")
    abs_author = normalize_author(metadata.get("authorName") or "")
    if cat_author and abs_author and cat_author == abs_author:
        score += 3
        signals.append("author")

    cat_narrators = narrators_set(catalog_row.get("narrator") or "")
    abs_narrators = narrators_set(metadata.get("narratorName") or "")
    if cat_narrators and abs_narrators and cat_narrators & abs_narrators:
        score += 2
        signals.append("narrator")

    cat_dur = duration_hhmm_to_seconds(catalog_row.get("duration_hhmm") or "")
    abs_dur = abs_item.get("media", {}).get("duration")
    if cat_dur and abs_dur:
        try:
            abs_dur_f = float(abs_dur)
            if abs_dur_f > 0 and cat_dur > 0:
                duration_diff_pct = abs(cat_dur - abs_dur_f) / max(cat_dur, abs_dur_f)
                if duration_diff_pct <= 0.05:
                    score += 2
                    signals.append(f"duration({duration_diff_pct:.1%})")
        except (ValueError, TypeError):
            pass

    cat_series = (catalog_row.get("series") or "").strip().lower()
    abs_series_list = metadata.get("series") or []
    abs_series_name = ""
    abs_series_seq = ""
    if isinstance(abs_series_list, list) and abs_series_list:
        abs_series_name = (abs_series_list[0].get("name") or "").strip().lower()
        abs_series_seq = str(abs_series_list[0].get("sequence") or "").strip()

    if cat_series and abs_series_name and (
        cat_series in abs_series_name or abs_series_name in cat_series
    ):
        score += 2
        signals.append("series")
        cat_seq = (catalog_row.get("series_index_sort") or "").strip()
        try:
            cat_seq_n = str(int(float(cat_seq))) if cat_seq else ""
        except ValueError:
            cat_seq_n = cat_seq
        try:
            abs_seq_n = str(int(float(abs_series_seq))) if abs_series_seq else ""
        except ValueError:
            abs_seq_n = abs_series_seq
        if cat_seq_n and abs_seq_n and cat_seq_n == abs_seq_n:
            score += 1
            signals.append(f"series_pos({cat_seq_n})")

    return score, duration_diff_pct, signals


def run_fuzzy_pass(
    unmatched_catalog: List[Dict[str, str]],
    unmatched_abs_items: List[Dict],
    verbose: bool = False,
) -> Dict[str, Tuple[Dict, List[str]]]:
    """
    Best-scoring ABS item per unmatched catalog book, above the threshold.

    ⚠️ An ABS item is CLAIMED once matched, so two catalog rows can never both
    point at it — without that, a series' books all collapse onto volume 1.

    Returns {catalog_slug: (abs_item, signals)}.
    """
    fuzzy: Dict[str, Tuple[Dict, List[str]]] = {}
    claimed: set = set()

    pairs = []
    for row in unmatched_catalog:
        slug = book_id_from_title((row.get("title") or "").strip())
        if slug:
            pairs.append((slug, row))

    for cat_slug, cat_row in pairs:
        best_score = 0
        best_dur = 1.0
        best_item: Optional[Dict] = None
        best_signals: List[str] = []

        for abs_item in unmatched_abs_items:
            abs_id = abs_item.get("id", "")
            if not abs_id or abs_id in claimed:
                continue
            score, dur, signals = score_fuzzy_match(cat_row, abs_item)
            if score < FUZZY_MATCH_THRESHOLD:
                continue
            if score > best_score or (score == best_score and dur < best_dur):
                best_score, best_dur, best_item, best_signals = score, dur, abs_item, signals

        if best_item is not None:
            fuzzy[cat_slug] = (best_item, best_signals)
            claimed.add(best_item.get("id", ""))
            if verbose:
                print(f"    ~ fuzzy: {(cat_row.get('title') or '')[:60]}")
                print(f"             -> {best_item.get('id')} score={best_score} [{', '.join(best_signals)}]")

    return fuzzy


# ---------------------------------------------------------------------------
# Media kind
# ---------------------------------------------------------------------------
def media_kind(abs_item: Dict) -> str:
    """
    'audio' when the item has audio tracks, else 'ebook'.

    ⚠️ Read off the LIST endpoint deliberately. `/api/libraries/<id>/items` does
    NOT return `libraryFiles`, so per-file inspection would cost one request per
    item (1,220 of them). `numTracks` is enough for the split that matters:
    measured 2026-09-02, 1,086 audio and 132 ebook-only, with 2 items holding
    neither. 'both' is not decided here — it comes from a slug appearing in the
    Audio library AND the Ebooks library, which is the only place that fact is
    real.
    """
    media = abs_item.get("media") or {}
    tracks = media.get("numTracks")
    if not tracks:
        tracks = len(media.get("audioFiles") or [])
    return "audio" if tracks else "ebook"


# ---------------------------------------------------------------------------
# ABS API helpers
# ---------------------------------------------------------------------------
def abs_session() -> requests.Session:
    """A session carrying the Cloudflare Access service-token headers."""
    s = requests.Session()
    s.headers.update({
        "CF-Access-Client-Id": ABS_CF_CLIENT_ID,
        "CF-Access-Client-Secret": ABS_CF_CLIENT_SECRET,
    })
    return s


def abs_login(session: requests.Session) -> str:
    resp = session.post(
        f"{ABS_BASE_URL.rstrip('/')}/login",
        json={"username": ABS_USERNAME, "password": ABS_PASSWORD},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("user", {}).get("token") or data.get("token")
    if not token:
        raise RuntimeError("Login succeeded but no token in response")
    return token


def abs_get_library_items(session: requests.Session, library_id: str) -> List[Dict]:
    resp = session.get(
        f"{ABS_BASE_URL.rstrip('/')}/api/libraries/{library_id}/items?limit=0",
        timeout=300,
    )
    resp.raise_for_status()
    return resp.json().get("results", [])


# ---------------------------------------------------------------------------
# Catalog reading
# ---------------------------------------------------------------------------
def load_catalog_rows() -> List[Dict[str, str]]:
    if not CATALOG_CSV.exists():
        print(f"[ERROR] catalog not found: {CATALOG_CSV}")
        sys.exit(1)
    rows: List[Dict[str, str]] = []
    with CATALOG_CSV.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if (row.get("title") or "").strip():
                rows.append(dict(row))
    return rows


# ---------------------------------------------------------------------------
# Map assembly
# ---------------------------------------------------------------------------
def abs_title_of(abs_item: Dict) -> str:
    return ((abs_item.get("media") or {}).get("metadata") or {}).get("title") or ""


def _record(books: Dict[str, Dict[str, str]], slug: str, item: Dict, stats: Dict[str, int]) -> None:
    """Write one entry, promoting to 'both' when a slug is in both libraries."""
    kind = media_kind(item)
    existing = books.get(slug)
    if existing is not None:
        if existing["m"] != kind and existing["m"] != "both":
            existing["m"] = "both"
            stats["both"] = stats.get("both", 0) + 1
        return
    title = abs_title_of(item).strip()
    if not title:
        return
    books[slug] = {"t": title, "m": kind}


def build_books_block(
    catalog_rows: List[Dict[str, str]],
    audio_items: List[Dict],
    ebook_items: List[Dict],
    verbose: bool = False,
) -> Tuple[Dict[str, Dict[str, str]], Dict[str, int]]:
    """
    Join the catalog to whatever ABS holds, and return (books, stats).

    A slug found in BOTH libraries is `both` — the one place that fact is real.
    """
    catalog_row_by_slug: Dict[str, Dict[str, str]] = {}
    for row in catalog_rows:
        slug = book_id_from_title((row.get("title") or "").strip())
        if slug and slug not in catalog_row_by_slug:
            catalog_row_by_slug[slug] = row

    books: Dict[str, Dict[str, str]] = {}
    stats: Dict[str, int] = {"exact": 0, "fuzzy": 0, "unmatched": 0, "both": 0}

    for items in (audio_items, ebook_items):
        if not items:
            continue

        # --- exact pass ------------------------------------------------------
        abs_by_slug: Dict[str, Dict] = {}
        for item in items:
            slug = book_id_from_title(abs_title_of(item))
            if slug and slug not in abs_by_slug:
                abs_by_slug[slug] = item

        matched_abs_ids: set = set()
        matched_slugs: set = set()
        for slug in catalog_row_by_slug:
            item = abs_by_slug.get(slug)
            if not item:
                continue
            matched_slugs.add(slug)
            matched_abs_ids.add(item.get("id", ""))
            _record(books, slug, item, stats)
            stats["exact"] += 1

        # --- fuzzy pass ------------------------------------------------------
        unmatched_rows = [
            catalog_row_by_slug[s] for s in catalog_row_by_slug if s not in matched_slugs
        ]
        unmatched_items = [i for i in items if i.get("id") not in matched_abs_ids]
        if unmatched_rows and unmatched_items:
            for slug, (item, _sig) in run_fuzzy_pass(unmatched_rows, unmatched_items, verbose).items():
                _record(books, slug, item, stats)
                stats["fuzzy"] += 1

    stats["unmatched"] = len(catalog_row_by_slug) - len(books)
    stats["catalog_slugs"] = len(catalog_row_by_slug)
    return books, stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Build site/shelf_book_map.json from ABS.")
    ap.add_argument("--dry-run", action="store_true", help="report without writing the file")
    ap.add_argument("--verbose", action="store_true", help="print fuzzy match signals")
    args = ap.parse_args(argv)

    missing = [
        v for v in (
            "ABS_BASE_URL", "ABS_USERNAME", "ABS_PASSWORD",
            "ABS_LIBRARY_ID", "ABS_CF_CLIENT_ID", "ABS_CF_CLIENT_SECRET",
        ) if not os.getenv(v)
    ]
    if missing:
        print(f"[ERROR] missing env vars: {', '.join(missing)}")
        return 1

    print(f"Reading catalog: {CATALOG_CSV}")
    catalog_rows = load_catalog_rows()
    print(f"  {len(catalog_rows)} books in catalog")

    session = abs_session()
    print("\nLogging in to Audiobookshelf...")
    try:
        session.headers["Authorization"] = f"Bearer {abs_login(session)}"
    except Exception as e:
        print(f"[ERROR] ABS login failed: {e}")
        return 1
    print("  logged in OK")

    print("Fetching Audio library items...")
    try:
        audio_items = abs_get_library_items(session, ABS_LIBRARY_ID)
    except Exception as e:
        print(f"[ERROR] failed to fetch library items: {e}")
        return 1
    print(f"  {len(audio_items)} items")

    ebook_items: List[Dict] = []
    if ABS_EBOOK_LIBRARY_ID:
        print("Fetching Ebooks library items...")
        try:
            ebook_items = abs_get_library_items(session, ABS_EBOOK_LIBRARY_ID)
            print(f"  {len(ebook_items)} items")
        except Exception as e:
            # ⚠️ Soft: a missing Ebooks library must not cost the audio half its
            # map. It is a NAMED skip, never a silent one.
            print(f"[WARN] Ebooks library {ABS_EBOOK_LIBRARY_ID} unreachable ({e}) — skipping it")
            ebook_items = []
    else:
        print("Ebooks library: not configured (ABS_EBOOK_LIBRARY_ID unset) — "
              "ebooks stay filed under the Audio library.")

    books, stats = build_books_block(catalog_rows, audio_items, ebook_items, args.verbose)

    payload = {
        "generatedAt": _dt.datetime.now(_dt.timezone.utc)
            .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "libraryId": ABS_LIBRARY_ID,
        "ebookLibraryId": ABS_EBOOK_LIBRARY_ID or None,
        "books": dict(sorted(books.items())),
    }

    kinds: Dict[str, int] = {"audio": 0, "ebook": 0, "both": 0}
    for v in books.values():
        kinds[v["m"]] = kinds.get(v["m"], 0) + 1

    print("\n--- Results ---")
    print(f"  catalog slugs   : {stats['catalog_slugs']}")
    print(f"  matched         : {len(books)}  (exact {stats['exact']}, fuzzy {stats['fuzzy']})")
    print(f"  unmatched       : {stats['unmatched']}   <- these render NO shelf button")
    print(f"  by kind         : audio {kinds['audio']}, ebook {kinds['ebook']}, both {kinds['both']}")

    if args.dry_run:
        print(f"\n[DRY RUN] would write {len(books)} entries to {SHELF_MAP_PATH}")
    else:
        SHELF_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SHELF_MAP_PATH.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=1, sort_keys=False, ensure_ascii=False)
            f.write("\n")
        print(f"\nWrote {len(books)} entries to {SHELF_MAP_PATH}")
        print(f"  stamped generatedAt={payload['generatedAt']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
