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

⚠️ WHO PUSHES — the LOCAL PIPELINE, and only it (owner decision 2026-08-17,
"option A"). ``scripts/sync_to_drive.py`` STEP 7 runs ``push_from_disk()`` at
the end of every cycle; the CI step that used to do it
(.github/workflows/deploy.yml) was DELETED the same day and its
``INDEX_PUSH_TOKEN`` repo secret retired. The reason is structural and
permanent: this push carries the EBOOK manifest, ``site/ebooks.json`` is
gitignored because the repo is public, and a CI checkout therefore can NEVER
hold it — a CI push would silently replace the snapshot with audiobooks only
and delete every ebook row. One writer, and it is the machine that owns the
files. Do not add a second pusher anywhere.

Failure posture, matching the games pusher:
  - INDEX_PUSH_TOKEN unset → one log line, nothing else. The index must never
    be able to stall this pipeline. (INDEX_URL is optional: it defaults to
    DEFAULT_INDEX_URL, so the SECRET is the only thing a machine must
    configure. A half-configured machine that silently never pushes is the
    exact failure this file exists to make loud.)
  - A real push failure raises: the pipeline step catches it and warns (the
    site is already committed by then); the manual runner exits non-zero so
    an attended push is loud. Snapshot-replace means a missed run costs
    freshness only — the previous snapshot stands.

Manual push (attended; the pipeline does this for you 3×/day):

    python -m app.index_push          # INDEX_PUSH_TOKEN comes from .env

Options: ``--dry-run`` (print the projection summary, push nothing),
``--csv PATH`` (default: site/catalog.csv), ``--ebooks PATH`` (default:
site/ebooks.json). Locally the env can also come from .env (app.config
loads dotenv).

Ebooks (ebook-split design phase 3, catalog-platform
docs/info/ebook-split-design.md §6): the snapshot ALSO carries one row per
ebook in ``site/ebooks.json`` (the manifest sync step 1b builds, and STEP 7
therefore runs after it), with
``format: 'ebook'``. They ride the SAME ``PUT /api/push/audiobook`` source —
the index's source vocabulary is closed (game/library/audiobook; an unknown
source 404s) and the design says the shared pool holds them, so 'audiobook'
the source means "the household's shared pool", and ``format`` carries the
medium. A missing or malformed manifest never blocks the audiobook rows:
ebooks degrade to absent, loudly.

Ebook ``detail_url`` is a DEEP LINK — ``https://ebooks.heygabi.ai/#<anchor>``
since 2026-08-17 (it used to be the bare shelf, which left the reader hunting
their own book among 168). The anchor is READ from the manifest, never
computed here: ``scripts/build_ebook_manifest.ebook_anchor()`` is its one
implementation, and ``app/web/templates/ebooks.html`` stamps the same value as
the tile's element id. A second derivation would break every deep link
silently — the page would simply not scroll, with no error anywhere.
"""

from __future__ import annotations

import json
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

# The shared estate index. A DEFAULT, not a required env var, since 2026-08-17:
# the CI step that carried `vars.INDEX_URL` is gone and the pipeline machine
# only holds the SECRET in .env. Requiring both would mean a machine with a
# valid token and no URL skips every push forever, printing "not set" — the
# silent-loss shape this module's warnings exist to prevent.
DEFAULT_INDEX_URL = "https://index.heygabi.ai"

# The ebook shelf's OWN hostname. It stopped being a page on the audiobook site
# on 2026-08-17 ("make it seem like it's own custom page") and estate search
# should send people to the shelf's own door, not through the audiobook site's.
DEFAULT_EBOOKS_SITE_URL = "https://ebooks.heygabi.ai"

# The ebook manifest sync step 1b writes (scripts/build_ebook_manifest.py
# OUT_PATH) — read here, never re-derived: one pipeline, one source of data.
DEFAULT_EBOOKS_PATH = SITE_DIR / "ebooks.json"

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


def load_ebook_manifest(path: Path) -> Optional[dict]:
    """
    Read site/ebooks.json defensively. Returns the manifest dict, or None.

    ⚠️ Never raises: a missing manifest means "no ebooks this run" (one INFO
    line) and a malformed one means "step 1b broke" (one WARN) — either way
    the AUDIOBOOK push proceeds untouched. The ebook rows are additive; their
    absence costs ebook freshness only, exactly the soft posture the rest of
    this module takes toward the index itself.
    """
    if not path.exists():
        # ⚠️ THE CONSEQUENCE IS NOT "no new ebooks" BUT "every ebook row LEAVES
        # the index" — the push is a snapshot REPLACE for the whole `audiobook`
        # source. Said loudly here because it is the one place it is observable.
        #
        # Since 2026-08-17 (owner decision "option A") this is NO LONGER a
        # normal state anywhere that pushes: the only pusher is
        # scripts/sync_to_drive.py STEP 7, running on the machine that owns the
        # library, and sync step 1b rewrites this file earlier in the SAME
        # cycle. The CI pusher — which never had the manifest, because
        # site/ebooks.json is gitignored on a PUBLIC repo — was deleted.
        #
        # So reaching this line now means one of three real faults, all worth
        # the shouting: step 1b failed or never ran this cycle, the push was
        # invoked from a checkout that is not the pipeline machine, or someone
        # re-added a second pusher somewhere that cannot hold the manifest.
        print(
            f"[WARN] ebook manifest not found ({path}): pushing audiobook rows only. "
            "⚠️ This snapshot REPLACES the source, so every ebook row is now ABSENT "
            "from estate search until an index push runs somewhere that has the manifest.",
            file=sys.stderr,
        )
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"[WARN] ebook manifest unreadable ({path}): {e} — pushing audiobook rows only", file=sys.stderr)
        return None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("ebooks"), list):
        print(
            f"[WARN] ebook manifest malformed ({path}): expected an object with an 'ebooks' array "
            "— pushing audiobook rows only",
            file=sys.stderr,
        )
        return None
    return manifest


def _str_or_empty(value: object) -> str:
    """A manifest field as a stripped string — non-strings (None, numbers) fold to ''."""
    return value.strip() if isinstance(value, str) else ""


def ebooks_detail_url(anchor: Optional[str] = None, site_url: Optional[str] = None) -> str:
    """Deep link to one book on the ebook shelf — `https://ebooks.heygabi.ai/#<anchor>`.

    ⚠️ The anchor is NOT computed here. It is read from the manifest, where
    `scripts/build_ebook_manifest.ebook_anchor()` is its one implementation —
    the page stamps the same value as the tile's element id, so a second
    derivation in a second place would break every estate-search deep link
    silently (the page would simply not scroll, with no error anywhere).

    Before 2026-08-17 this pointed at `<audiobook site>/ebooks.html` and every
    ebook in estate search landed on the top of the shelf, leaving the reader
    to find their own book among 168. The shelf has had its own hostname since
    it became its own page; use it.

    An entry with no anchor (an older manifest) degrades to the bare shelf URL
    — the previous behaviour, which is a worse link but never a broken one.
    """
    site = (site_url or os.environ.get("EBOOKS_SITE_URL") or DEFAULT_EBOOKS_SITE_URL).rstrip("/")
    return f"{site}/#{anchor}" if anchor else site + "/"


def build_ebook_rows(manifest: Optional[dict]) -> List[Dict[str, object]]:
    """
    Project ebook manifest entries into index rows (design phase 3), raw
    strings only — same allow-list, same no-fold rule as build_projection.

    - format is the literal 'ebook' — the medium, not the file extension
      (epub/pdf stays a site concern). This is what makes ebooks findable AS
      ebooks in estate search; the design names it.
    - source_id is 'ebook:<path>' — path-derived per the design, unique by
      construction (one file, one path). It can never collide with an
      audiobook source_id: book_key() always contains '|', and a Windows
      file path never can.
    - `filename`-sourced entries are pushed as-is: title from the manifest,
      author only when the manifest has one (design: "pushed title-only" —
      a wrong author is worse than a missing one, and the index's fold guard
      handles what won't join honestly).
    - cover_url passes through from the manifest (the bookshelf redesign,
      2026-08-17: step 1b resolves it — sibling audiobook cover or extracted
      epub cover on R2). The manifest stores the absolute canonical URL;
      canonical_cover_url() passes absolutes through untouched and is kept
      here purely as defence for a relative href. `cover_source` is the
      page's concern and never travels (it is not in the allow-list).
    - Entries without a title or path are skipped with a warning; a single
      bad row must not 422 the whole snapshot.
    """
    if not manifest:
        return []

    rows: List[Dict[str, object]] = []
    seen: set[str] = set()
    skipped_unusable = 0
    skipped_duplicate = 0
    anchorless = 0

    for e in manifest.get("ebooks", []):
        if not isinstance(e, dict):
            skipped_unusable += 1
            continue
        path_rel = _str_or_empty(e.get("path"))
        title = _str_or_empty(e.get("title"))
        if not path_rel or not title:
            skipped_unusable += 1
            continue
        source_id = "ebook:" + path_rel
        if source_id in seen:
            skipped_duplicate += 1
            continue
        seen.add(source_id)
        anchor = _str_or_empty(e.get("anchor"))
        if not anchor:
            anchorless += 1
        rows.append(
            {
                "source_id": source_id,
                "title": title,
                "creator": _str_or_empty(e.get("author")) or None,
                "series": None,
                "series_index": None,
                "year": None,
                "format": "ebook",
                "cover_url": canonical_cover_url(_str_or_empty(e.get("cover_url"))) or None,
                "detail_url": ebooks_detail_url(anchor or None),
            }
        )

    if anchorless:
        print(
            f"[WARN] ebook projection: {anchorless} entry(ies) carry no `anchor` — their detail_url "
            "lands on the top of the shelf instead of the book. Rebuild the manifest "
            "(`python scripts/build_ebook_manifest.py`).",
            file=sys.stderr,
        )
    if skipped_unusable:
        print(f"[WARN] ebook projection: skipped {skipped_unusable} entry(ies) without a usable title/path", file=sys.stderr)
    if skipped_duplicate:
        print(f"[WARN] ebook projection: skipped {skipped_duplicate} duplicate path(s)", file=sys.stderr)
    return rows


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


def _load_csv(csv_path: Path) -> List[Dict[str, str]]:
    import csv as _csv

    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        return list(_csv.DictReader(f))


def build_snapshot_from_disk(csv_path: Path, ebooks_path: Path) -> tuple[List[Dict[str, object]], List[Dict[str, object]]]:
    """(audiobook rows, ebook rows) read from the two files on disk."""
    projection = build_projection(_load_csv(csv_path))
    ebook_rows = build_ebook_rows(load_ebook_manifest(ebooks_path))
    return projection, ebook_rows


def push_rows(projection: List[Dict[str, object]], ebook_rows: List[Dict[str, object]]) -> Dict[str, object]:
    """
    PUT one snapshot and report what happened, in a shape a caller can log.

    Returns ``{"pushed", "skipped", "audiobooks", "ebooks", "rows", "result"}``
    — ``skipped`` carries the human reason when nothing was sent.

    Raises RuntimeError when the projection has ZERO audiobook rows (that is a
    failed export, not an empty catalog: the push is a snapshot REPLACE, so
    sending it would erase ~1,078 rows — and ebook rows alone must never
    become the catalog's whole snapshot) or when the index answers non-2xx.
    Every success prints exactly ONE named line; so does every skip.
    """
    index_url = os.environ.get("INDEX_URL") or DEFAULT_INDEX_URL
    token = os.environ.get("INDEX_PUSH_TOKEN")
    summary: Dict[str, object] = {
        "pushed": False,
        "skipped": None,
        "audiobooks": len(projection),
        "ebooks": len(ebook_rows),
        "rows": len(projection) + len(ebook_rows),
        "result": None,
    }
    if not token:
        summary["skipped"] = "INDEX_PUSH_TOKEN not set"
        print("[INFO] Index push skipped: INDEX_PUSH_TOKEN not set")
        return summary
    if not projection:
        raise RuntimeError("projection produced zero audiobook rows — refusing to push (failed export?)")

    result = push_snapshot(projection + ebook_rows, index_url, token)
    summary["pushed"] = True
    summary["result"] = result
    print(
        f"[INFO] Index push OK: {result.get('rows')} rows as '{result.get('source')}' "
        f"({len(projection)} audiobook + {len(ebook_rows)} ebook), "
        f"pushed_at {result.get('pushed_at')}, unfoldable_titles {result.get('unfoldable_titles')}"
    )
    return summary


def push_from_disk(csv_path: Optional[Path] = None, ebooks_path: Optional[Path] = None) -> Dict[str, object]:
    """
    The PIPELINE's entry point — sync STEP 7 (scripts/sync_to_drive.py).

    Reads site/catalog.csv and site/ebooks.json as they stand on disk AFTER
    step 1b rebuilt the manifest and step 5 rebuilt the catalog, then pushes
    the whole snapshot. Deliberately file-based rather than taking the build's
    in-memory rows: the pipeline is the one writer, it runs the push as its
    own step at the END of a cycle, and reading the same files every other
    local check reads is what keeps "what shipped" and "what was pushed"
    the same thing.

    Raises (never swallows) — the caller decides how loud. The pipeline step
    warns and carries on; ``python -m app.index_push`` exits non-zero.
    """
    csv_path = Path(csv_path) if csv_path else SITE_DIR / SITE_CSV_NAME
    ebooks_path = Path(ebooks_path) if ebooks_path else DEFAULT_EBOOKS_PATH
    if not csv_path.exists():
        raise FileNotFoundError(f"catalog not found: {csv_path}")
    projection, ebook_rows = build_snapshot_from_disk(csv_path, ebooks_path)
    return push_rows(projection, ebook_rows)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m app.index_push",
        description="Push site/catalog.csv's projection to the shared index (PUT /api/push/audiobook).",
    )
    parser.add_argument("--csv", type=Path, default=SITE_DIR / SITE_CSV_NAME, help="catalog CSV (default: site/catalog.csv)")
    parser.add_argument(
        "--ebooks", type=Path, default=DEFAULT_EBOOKS_PATH, help="ebook manifest (default: site/ebooks.json)"
    )
    parser.add_argument("--dry-run", action="store_true", help="build and summarise the projection; push nothing")
    args = parser.parse_args(argv)

    if not args.csv.exists():
        print(f"[ERROR] catalog not found: {args.csv}", file=sys.stderr)
        return 2

    projection, ebook_rows = build_snapshot_from_disk(args.csv, args.ebooks)
    print(f"[INFO] {len(projection)} audiobook row(s) projected from {args.csv}")
    print(f"[INFO] {len(ebook_rows)} ebook row(s) projected from {args.ebooks}")
    print(f"[INFO] {len(projection) + len(ebook_rows)} row(s) total for PUT /api/push/audiobook")

    if args.dry_run:
        for sample in projection[:3]:
            print(json.dumps(sample, ensure_ascii=False))
        for sample in ebook_rows[:3]:
            print(json.dumps(sample, ensure_ascii=False))
        print("[INFO] dry run: nothing pushed")
        return 0

    try:
        # Same body the pipeline's STEP 7 runs — the CLI only differs in how
        # loud it is about a failure (non-zero exit; the pipeline warns and
        # carries on, since the index must never cost a cycle).
        push_rows(projection, ebook_rows)
    except Exception as e:  # noqa: BLE001 — the manual runner is the loud path
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
