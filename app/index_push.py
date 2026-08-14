# app/index_push.py
"""
Push this catalog's projection to the shared cross-catalog index.

Design: catalog-platform/docs/info/index-worker-design.md §5 / §7 step 4 —
the audiobook pusher is PIPELINE-SIDE Python that PUTs a full snapshot of RAW
display strings to ``PUT /api/push/audiobook``. The index Worker folds,
refuses degenerate keys, and resolves universes ON WRITE, once, on its side.
⚠️ There is NO fold/normalisation code in this module, ever — that is the
design's central rule (§6): sources push raw strings so the estate has exactly
one fold implementation, pinned by the index's fixture file.

Body shape: a JSON array of rows matching the index's STRICT zod schema
(catalog-platform/apps/index-worker/src/rows.ts pushRowSchema — unknown keys
are refused with a 422, not silently stripped). The games pusher
(Board_Game_Catalog apps/worker/src/lib/index-push.ts) is the working proof
of the same protocol: bearer token, full snapshot, replace-by-source.

The projection is default-deny (design §4.1): titles / authors / series /
covers / links only. Ownership does not travel — no purchase data, narrators,
durations, descriptions, progress, or personal fields.

Failure posture, matching the games pusher:
  - INDEX_URL / INDEX_PUSH_TOKEN unset → one log line, nothing else. The
    index must never be able to stall this pipeline.
  - A real push failure raises: app/main.py catches it and warns (the site
    build is already done by then); the manual runner exits non-zero so an
    attended first push is loud. Snapshot-replace means a missed run costs
    freshness only — the previous snapshot stands.

Manual first push (attended, no cron needed):

    INDEX_URL=https://index.heygabi.ai INDEX_PUSH_TOKEN=... python -m app.index_push

Options: ``--dry-run`` (print the projection summary, push nothing),
``--csv PATH`` (default: site/catalog.csv). Locally the env can also come
from .env (app.config loads dotenv).
"""

from __future__ import annotations

import math
import os
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional

from app.additions_log import book_key
from app.config import COVERS_BASE_URL, SITE_CSV_NAME, SITE_DIR

# Same source + default as app/tools/send_discord_notification.py and
# scripts/health_check.py — the repo VARIABLE SITE_URL wins when set.
DEFAULT_SITE_URL = "https://audiobooks.heygabi.ai/"

# The complete set of keys a pushed row may carry (index rows.ts pushRowSchema
# is .strict(); publisher/kind/parent_source_id exist there but are a games
# concern and are deliberately never sent from here).
ALLOWED_KEYS = frozenset(
    {"source_id", "title", "creator", "series", "series_index", "year", "format", "cover_url", "detail_url"}
)

_ENCODED_PAIR = re.compile(r"%[0-9A-Fa-f]{2}")
_LONE_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_ABSOLUTE = re.compile(r"^(https?:)?//")


def canonical_cover_url(href: Optional[str], base: Optional[str] = None) -> str:
    """
    Resolve a catalog `cover_href` to the ONE canonical fetchable URL —
    a Python mirror of site/covers-base.js coverUrl(), rule for rule.

    Covers live on R2 behind COVERS_BASE_URL (https://covers.heygabi.ai/);
    the object key is the href minus the leading "covers/". Some historic
    hrefs arrive already percent-encoded ('J.R.%20Mathews/…') — encoding an
    encoded value double-encodes it (%20 → %2520) and the CDN answers 503
    (measured live 2026-08-13). So: canonicalise by decoding-if-decodable
    first, then encode exactly once with urllib.parse.quote(safe='/'), the
    encoder the JS side matches byte-for-byte (it percent-encodes !'()*
    where encodeURIComponent would not).

    JS decodeURIComponent THROWS on any '%' not followed by two hex digits
    and the value stays raw; the Python mirror of that is decoding only when
    every '%' begins a valid pair.
    """
    cover = (href or "").strip()
    if not cover:
        return ""
    if _ABSOLUTE.match(cover) or cover.startswith("data:"):
        return cover
    rel = cover[len("covers/"):] if cover.startswith("covers/") else cover
    if _ENCODED_PAIR.search(rel) and not _LONE_PERCENT.search(rel):
        rel = urllib.parse.unquote(rel)
    base = (COVERS_BASE_URL if base is None else base).strip()
    if not base:
        return cover
    return base.rstrip("/") + "/" + urllib.parse.quote(rel.lstrip("/"), safe="/")


def detail_url_for(title: str, site_url: Optional[str] = None) -> str:
    """
    Deep link to this book on the site. The site's only book anchor is the
    hash search (site index.html _parseHash: '#' + URLSearchParams, key 'q'),
    so the detail URL is '<site>/#q=<title>'. urlencode() serialises exactly
    like URLSearchParams.toString() (space → '+'), so the link round-trips.
    """
    site = (site_url or os.environ.get("SITE_URL") or DEFAULT_SITE_URL).rstrip("/")
    return site + "/#" + urllib.parse.urlencode({"q": title})


def _year_of(raw: Optional[str]) -> Optional[int]:
    """catalog.csv `year` is sometimes a full date ('2016-06-14'): first 4-digit run wins."""
    m = re.search(r"\d{4}", raw or "")
    return int(m.group(0)) if m else None


def _series_index_of(raw: Optional[str]) -> Optional[float]:
    """`series_index_sort` when it parses as a finite float; ranges/words/blank → None."""
    try:
        value = float((raw or "").strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def build_projection(rows: List[Dict[str, str]]) -> List[Dict[str, object]]:
    """
    The allow-listed projection of the catalog, raw strings only.

    - source_id is the catalog's own identity key, book_key(title, author) —
      the same 'title|author' string additions_log.json and the Discord
      snapshot already key on. Stable across file moves and re-syncs.
    - Rows without a title are skipped (the index schema requires one, and a
      single bad row would 422 the whole snapshot); duplicates of the same
      source_id keep the first row (the index refuses duplicate ids).
    - Empty strings become None — the index schema wants absent/null, not ''.
    """
    projection: List[Dict[str, object]] = []
    seen: set[str] = set()
    skipped_untitled = 0
    skipped_duplicate = 0

    for r in rows:
        title = (r.get("title") or "").strip()
        author = (r.get("author") or "").strip()
        if not title:
            skipped_untitled += 1
            continue
        source_id = book_key(title, author)
        if source_id in seen:
            skipped_duplicate += 1
            continue
        seen.add(source_id)
        projection.append(
            {
                "source_id": source_id,
                "title": title,
                "creator": author or None,
                "series": (r.get("series") or "").strip() or None,
                "series_index": _series_index_of(r.get("series_index_sort")),
                "year": _year_of(r.get("year")),
                "format": "audiobook",
                "cover_url": canonical_cover_url(r.get("cover_href")) or None,
                "detail_url": detail_url_for(title),
            }
        )

    if skipped_untitled:
        print(f"[WARN] index projection: skipped {skipped_untitled} row(s) without a title", file=sys.stderr)
    if skipped_duplicate:
        print(f"[WARN] index projection: skipped {skipped_duplicate} duplicate source_id row(s)", file=sys.stderr)
    return projection


def push_snapshot(projection: List[Dict[str, object]], index_url: str, token: str, timeout: int = 120) -> dict:
    """PUT the full snapshot. Raises on any non-2xx — callers decide how loud to be."""
    import requests  # deferred so the projection stays importable without it

    url = index_url.rstrip("/") + "/api/push/audiobook"
    resp = requests.put(
        url,
        json=projection,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=timeout,
    )
    if not resp.ok:
        raise RuntimeError(f"index push failed: {resp.status_code} {resp.text[:500]}")
    return resp.json()


def push_after_build(rows: List[Dict[str, str]]) -> Optional[dict]:
    """
    The pipeline hook — called by app/main.py once the site is staged.

    Env unset (true on this machine and in CI until the owner sets the
    INDEX_PUSH_TOKEN secret) → one log line, return None. Anything that goes
    wrong past that point raises; main.py catches and warns.
    """
    index_url = os.environ.get("INDEX_URL")
    token = os.environ.get("INDEX_PUSH_TOKEN")
    if not index_url or not token:
        print("[INFO] Index push skipped: INDEX_URL / INDEX_PUSH_TOKEN not set")
        return None

    projection = build_projection(rows)
    if not projection:
        # The index would 422 this anyway: zero rows is a failed export, not an
        # empty catalog. Say so here and keep the previous snapshot standing.
        print("[WARN] Index push skipped: projection produced zero rows (failed export?)", file=sys.stderr)
        return None

    result = push_snapshot(projection, index_url, token)
    print(
        f"[INFO] Index push OK: {result.get('rows')} rows as '{result.get('source')}', "
        f"pushed_at {result.get('pushed_at')}, unfoldable_titles {result.get('unfoldable_titles')}"
    )
    return result


def _load_csv(csv_path: Path) -> List[Dict[str, str]]:
    import csv as _csv

    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        return list(_csv.DictReader(f))


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m app.index_push",
        description="Push site/catalog.csv's projection to the shared index (PUT /api/push/audiobook).",
    )
    parser.add_argument("--csv", type=Path, default=SITE_DIR / SITE_CSV_NAME, help="catalog CSV (default: site/catalog.csv)")
    parser.add_argument("--dry-run", action="store_true", help="build and summarise the projection; push nothing")
    args = parser.parse_args(argv)

    if not args.csv.exists():
        print(f"[ERROR] catalog not found: {args.csv}", file=sys.stderr)
        return 2

    rows = _load_csv(args.csv)
    projection = build_projection(rows)
    print(f"[INFO] {len(projection)} rows projected from {args.csv}")

    if args.dry_run:
        import json

        for sample in projection[:3]:
            print(json.dumps(sample, ensure_ascii=False))
        print("[INFO] dry run: nothing pushed")
        return 0

    index_url = os.environ.get("INDEX_URL")
    token = os.environ.get("INDEX_PUSH_TOKEN")
    if not index_url or not token:
        # Soft by design: in CI this step runs before the owner has created the
        # secret, and "not configured yet" must not redden the pipeline.
        print("[INFO] Index push skipped: INDEX_URL / INDEX_PUSH_TOKEN not set")
        return 0

    if not projection:
        print("[ERROR] projection produced zero rows — refusing to push (failed export?)", file=sys.stderr)
        return 1

    try:
        result = push_snapshot(projection, index_url, token)
    except Exception as e:  # noqa: BLE001 — the manual runner is the loud path
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1
    print(
        f"[INFO] Index push OK: {result.get('rows')} rows as '{result.get('source')}', "
        f"pushed_at {result.get('pushed_at')}, unfoldable_titles {result.get('unfoldable_titles')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
