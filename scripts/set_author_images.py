"""
Give Audiobookshelf's author cards a real book cover instead of a silhouette.

THE ASK (owner, 2026-08-21, verbatim)
-------------------------------------
    "for the newest author area lets make the author be a random book from
     their collection, make sure its the first book in a series, if we dont
     have the first book have it be the loest series book."

THE SURFACE. Audiobookshelf's own author cards — the "Newest Authors" shelf on
its home page and every author tile. It is not a page we build, so this is not
a UI change: it is `POST /api/authors/<id>/image` per author, and ABS renders
the result. Measured live 2026-09-02: the library has **495 authors and 494 of
them have no `imagePath`**, which is the placeholder art the ask is about.

THE RULE, in the order it is applied
------------------------------------
  1. Prefer a book that is **#1 in its series**.
  2. If we do not hold #1, take the **lowest series number we do hold** — a #3
     with #1 and #2 absent still beats a standalone, because the point is a
     recognisable entry point, not an arbitrary volume.
  3. ⚠️ An author with **no series at all** falls through both. That case is
     named rather than left to chance: pick among their standalones, stably
     (see below).

⚠️ WHY THE PICK IS STABLE AND NOT RE-ROLLED
-------------------------------------------
The word in the ask is "random", and the first draft of this script honoured it
literally — `random.choice` on every run, with a docstring promising the
portrait "rotates nightly". `docs/TODO.md` had already recorded why that is
wrong: *"The picker is a function that produces a PERSISTED choice, so it is
one canonical implementation, and changing it later is a migration, not an
edit… ⚠️ Stable is almost certainly what is wanted — a shelf whose art changes
every night reads as a bug."*

So the choice is deterministic:
  * rules 1 and 2 are already deterministic — the lowest series index wins, with
    the title as tie-break;
  * rule 3 seeds a `random.Random` with the AUTHOR'S NAME, so the pick is
    arbitrary (which is what "random" was asking for) but identical on every
    run, forever, on any machine.

Changing the seed or the ordering re-rolls 495 persisted choices. That is a
migration; do it deliberately or not at all.

⚠️ IDEMPOTENCE. By default this only fills authors who have NO image, because
that is the ask — 494 silhouettes. An author who already has one (including a
real photo somebody set by hand) is left alone. `--force` re-sets everyone,
which is the flag to reach for after changing the picker, and the only way to
overwrite a hand-set portrait.

USAGE
-----
    .venv\\Scripts\\python scripts/set_author_images.py --dry-run
    .venv\\Scripts\\python scripts/set_author_images.py --limit 1     # try one
    .venv\\Scripts\\python scripts/set_author_images.py
    .venv\\Scripts\\python scripts/set_author_images.py --force

Exit 0 = success (even if some authors were not found in ABS; they are named).
Exit 1 = fatal (cannot read the catalog, cannot log in, missing env vars).
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

import requests

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ⚠️ The canonical author-name resolver, NOT a local reimplementation. A shelf
# has one folder per body of work and Drive has one folder per human being;
# app/author_names.py's header records the 2026-08-09 incident caused by
# collapsing those two maps. ABS's author names come from the shelf tree, so
# the catalogue's spelling has to travel through the SHELF aliases to match.
from app.author_names import load_shelf_aliases, resolve_shelf_author  # noqa: E402

CATALOG_CSV: Path = PROJECT_ROOT / "site" / "catalog.csv"

COVERS_BASE_URL: str = (os.getenv("COVERS_BASE_URL") or "https://covers.heygabi.ai/").strip()

ABS_BASE_URL: str = os.getenv("ABS_BASE_URL", "")
ABS_USERNAME: str = os.getenv("ABS_USERNAME", "")
ABS_PASSWORD: str = os.getenv("ABS_PASSWORD", "")
ABS_LIBRARY_ID: str = os.getenv("ABS_LIBRARY_ID", "")
ABS_CF_CLIENT_ID: str = os.getenv("ABS_CF_CLIENT_ID", "")
ABS_CF_CLIENT_SECRET: str = os.getenv("ABS_CF_CLIENT_SECRET", "")

RATE_LIMIT_SECONDS: float = 0.15


def say(msg: str = "") -> None:
    """
    print() that cannot kill the run on a non-ASCII author name.

    ⚠️ Found the hard way 2026-09-02: the library has an author named 猫子, and
    a Windows console on cp1252 raises UnicodeEncodeError trying to print it.
    The first dry run died three quarters of the way through the list — after
    the writes it had already reported, which in a real run would have left no
    record of where it stopped.
    """
    try:
        print(msg)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "ascii"
        print(msg.encode(enc, errors="replace").decode(enc, errors="replace"))


# ---------------------------------------------------------------------------
# Cover URL construction (mirrors app/index_push.py canonical_cover_url)
# ---------------------------------------------------------------------------
def cover_url_from_href(href: str) -> str:
    """
    'covers/Author/Title.jpg' -> a fetchable URL on the covers CDN.

    ⚠️ ABS fetches this URL ITSELF, server-side, so it must be publicly
    readable — not behind the estate's gate. Verified 2026-09-02: the CDN
    answers 206/image/jpeg to an unauthenticated ranged GET.
    """
    href = (href or "").strip()
    if not href:
        return ""
    rel = href[len("covers/"):] if href.startswith("covers/") else href
    return COVERS_BASE_URL.rstrip("/") + "/" + urllib.parse.quote(rel, safe="/")


# ---------------------------------------------------------------------------
# Catalog reading
# ---------------------------------------------------------------------------
def load_catalog() -> List[Dict[str, str]]:
    if not CATALOG_CSV.exists():
        print(f"[ERROR] catalog not found: {CATALOG_CSV}")
        sys.exit(1)
    with CATALOG_CSV.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _series_index(book: Dict[str, str]) -> Optional[float]:
    """The book's position in its series, or None when it is a standalone."""
    if not (book.get("series") or "").strip():
        return None
    raw = (book.get("series_index_sort") or "").strip()
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def pick_cover_for_author(author: str, books: List[Dict[str, str]]) -> str:
    """
    The owner's rule, applied in order. Deterministic for a given author.

    ⚠️ THIS FUNCTION PRODUCES A PERSISTED CHOICE. Changing it re-rolls every
    author's portrait, which is a migration and not an edit. See the module
    docstring.
    """
    with_covers = [b for b in books if (b.get("cover_href") or "").strip()]
    if not with_covers:
        return ""

    # Rules 1 and 2 — lowest series index we hold, title as the tie-break so
    # two books at the same index cannot swap places between runs.
    in_series = [(idx, b) for b in with_covers if (idx := _series_index(b)) is not None]
    if in_series:
        chosen = min(in_series, key=lambda pair: (pair[0], (pair[1].get("title") or "")))[1]
        return cover_url_from_href(chosen.get("cover_href", ""))

    # Rule 3 — no series anywhere in this author's shelf. Arbitrary, but the
    # SAME arbitrary choice on every run and every machine: sort for a stable
    # order, then seed the pick with the author's name.
    standalones = sorted(with_covers, key=lambda b: (b.get("title") or ""))
    rng = random.Random(author)
    return cover_url_from_href(rng.choice(standalones).get("cover_href", ""))


def pick_covers(rows: List[Dict[str, str]]) -> Dict[str, str]:
    """
    Group the catalogue by SHELF author name and pick one cover each.

    Returns {shelf_author_name: cover_url}.
    """
    aliases = load_shelf_aliases()
    by_author: Dict[str, List[Dict[str, str]]] = {}
    for row in rows:
        author = (row.get("author") or "").strip()
        if not author or not (row.get("cover_href") or "").strip():
            continue
        by_author.setdefault(resolve_shelf_author(author, aliases), []).append(row)

    out: Dict[str, str] = {}
    for author, books in by_author.items():
        url = pick_cover_for_author(author, books)
        if url:
            out[author] = url
    return out


# ---------------------------------------------------------------------------
# ABS API
# ---------------------------------------------------------------------------
def abs_session() -> requests.Session:
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
    token = resp.json().get("user", {}).get("token", "")
    if not token:
        print("[ERROR] login succeeded but no token in response")
        sys.exit(1)
    return token


def abs_get_authors(session: requests.Session) -> List[Dict]:
    resp = session.get(
        f"{ABS_BASE_URL.rstrip('/')}/api/libraries/{ABS_LIBRARY_ID}/authors", timeout=120
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("authors", data if isinstance(data, list) else [])


def abs_set_author_image(session: requests.Session, author_id: str, image_url: str):
    """
    Set an author's image from a URL.

    ⚠️ Verified against the live server 2026-09-02 rather than assumed:
    `OPTIONS /api/authors/<id>/image` answers `Allow: GET,HEAD,POST,DELETE`, so
    POST is the verb and DELETE is the undo. (`PATCH /api/authors/<id>` exists
    too but is for metadata, not the image.)

    Returns (ok, detail).
    """
    resp = session.post(
        f"{ABS_BASE_URL.rstrip('/')}/api/authors/{author_id}/image",
        json={"url": image_url},
        timeout=120,
    )
    if resp.status_code == 200:
        return True, ""
    return False, f"HTTP {resp.status_code}: {resp.text[:200]}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would change without calling the write API")
    ap.add_argument("--force", action="store_true",
                    help="re-set authors who already have an image (⚠️ overwrites hand-set portraits)")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N writes — use --limit 1 to try one before the rest")
    args = ap.parse_args(argv)

    missing = [
        v for v in ("ABS_BASE_URL", "ABS_USERNAME", "ABS_PASSWORD", "ABS_LIBRARY_ID",
                    "ABS_CF_CLIENT_ID", "ABS_CF_CLIENT_SECRET")
        if not os.getenv(v)
    ]
    if missing:
        say(f"[ERROR] missing env vars: {', '.join(missing)}")
        return 1

    say(f"Reading catalog: {CATALOG_CSV}")
    rows = load_catalog()
    covers = pick_covers(rows)
    say(f"  {len(rows)} books in catalog")
    say(f"  {len(covers)} shelf authors with a chosen cover")
    if not covers:
        say("[WARN] no covers to set — nothing to do")
        return 0

    session = abs_session()
    say("\nLogging in to Audiobookshelf...")
    session.headers["Authorization"] = f"Bearer {abs_login(session)}"
    say("  logged in OK")

    say("Fetching ABS author list...")
    abs_authors = abs_get_authors(session)
    already = sum(1 for a in abs_authors if a.get("imagePath"))
    say(f"  {len(abs_authors)} authors in ABS ({already} already have an image)")

    abs_map: Dict[str, Dict] = {}
    for a in abs_authors:
        name = (a.get("name") or "").strip().lower()
        if name:
            abs_map[name] = a

    set_count = skipped_has_image = 0
    failures: List[str] = []
    not_found: List[str] = []

    for author_name, cover_url in sorted(covers.items()):
        abs_author = abs_map.get(author_name.lower())
        if not abs_author:
            not_found.append(author_name)
            continue

        if abs_author.get("imagePath") and not args.force:
            # ⚠️ Default is fill-the-blanks. Overwriting an image somebody set
            # by hand needs --force and should be a decision, not a side effect.
            skipped_has_image += 1
            continue

        if args.limit and set_count >= args.limit:
            break

        if args.dry_run:
            say(f"  [DRY] {author_name} -> {cover_url}")
            set_count += 1
            continue

        ok, detail = abs_set_author_image(session, abs_author["id"], cover_url)
        if ok:
            set_count += 1
            say(f"  [SET] {author_name}")
        else:
            failures.append(f"{author_name}: {detail}")
            say(f"  [FAIL] {author_name} — {detail}")
        time.sleep(RATE_LIMIT_SECONDS)

    say(f"\n{'--- DRY RUN SUMMARY ---' if args.dry_run else '--- SUMMARY ---'}")
    say(f"  images set              : {set_count}")
    say(f"  skipped (already had one): {skipped_has_image}   (use --force to overwrite)")
    say(f"  failed                  : {len(failures)}")
    say(f"  not found in ABS        : {len(not_found)}")
    if failures:
        say("  Failures:")
        for f in failures[:20]:
            say(f"    - {f}")
    if not_found:
        # ⚠️ Named, never a silent skip: an author here means the catalogue and
        # the shelf disagree about a name, which is an alias question.
        say("  Authors in the catalogue but not on the shelf (alias mismatch?):")
        for name in sorted(not_found)[:20]:
            say(f"    - {name}")
        if len(not_found) > 20:
            say(f"    ... and {len(not_found) - 20} more")

    return 0


if __name__ == "__main__":
    sys.exit(main())
