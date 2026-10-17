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
import json
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
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

# (2026-10-16) The catalogue-name lookup through app/author_names.py's shelf
# aliases is gone: authors are now matched through their own shelf ITEMS (see
# rows_for_items), which needs no name translation at all.

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


def rows_for_items(item_ids: List[str], row_by_item: Dict[str, Dict[str, str]]) -> List[Dict[str, str]]:
    """The catalogue rows behind an author's shelf items, one per title.

    ⚠️ WHY ITEMS AND NOT NAMES (2026-10-16). The first version grouped the
    catalogue by its author STRING and looked that string up in ABS. ABS
    splits a credit like "Amie Kaufman, Jay Kristoff" into two people, so
    neither matched — 103 of 183 blank authors (KI-10). Starting from the
    author's own shelf items makes every co-author, and every shelf spelling
    the catalogue does not use, resolve the same way."""
    out: Dict[str, Dict[str, str]] = {}
    for iid in item_ids:
        row = row_by_item.get(iid)
        if row is not None:
            out.setdefault((row.get("title") or "").strip().lower(), row)
    return list(out.values())


def name_key(name: str) -> str:
    """Comparable form of an author name across the two libraries. The
    Ebooks library takes names from epub metadata, which often spells them
    "Atwood, Margaret" where Audio has "Margaret Atwood" (measured
    2026-10-16), so a single "Last, First" comma is flipped."""
    name = " ".join((name or "").split()).lower()
    if name.count(",") == 1:
        last, first = (p.strip() for p in name.split(","))
        if last and first:
            name = f"{first} {last}"
    return name


def is_empty_author(author: Dict) -> bool:
    """An author record holding nothing at all: no books, no image, no
    description, no ASIN. Deleting one loses nothing — ABS recreates the
    author on the next scan if a book of theirs ever returns. Anything with
    an image or hand-written metadata is kept, whatever its book count.
    Owner approved the prune 2026-10-16."""
    return (not author.get("numBooks") and not author.get("imagePath")
            and not (author.get("description") or "").strip()
            and not (author.get("asin") or "").strip())


def item_row_map(rows: List[Dict[str, str]], audio_items: List[Dict],
                 ebook_items: List[Dict]) -> Dict[str, Dict[str, str]]:
    """ABS item id -> catalogue row, through the SAME join the site's shelf
    buttons use (scripts/build_shelf_map.py, exact + fuzzy). Only catalogue
    rows carry a public cover, so an item with no row has no usable cover."""
    import build_shelf_map as shelf  # noqa: E402 — scripts/ is on the path
    row_by_slug: Dict[str, Dict[str, str]] = {}
    for row in rows:
        slug = shelf.book_id_from_title((row.get("title") or "").strip())
        if slug and slug not in row_by_slug:
            row_by_slug[slug] = row
    claimed: Dict[str, List[str]] = {}
    shelf.build_books_block(rows, audio_items, ebook_items, ids=claimed)
    return {iid: row_by_slug[slug] for slug, iids in claimed.items()
            if slug in row_by_slug for iid in iids}


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


def abs_get_authors(session: requests.Session, library_id: str) -> List[Dict]:
    resp = session.get(
        f"{ABS_BASE_URL.rstrip('/')}/api/libraries/{library_id}/authors", timeout=120
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("authors", data if isinstance(data, list) else [])


def abs_author_item_ids(session: requests.Session, author_id: str) -> List[str]:
    resp = session.get(
        f"{ABS_BASE_URL.rstrip('/')}/api/authors/{author_id}?include=items", timeout=120
    )
    resp.raise_for_status()
    return [i["id"] for i in resp.json().get("libraryItems") or [] if i.get("id")]


def abs_set_author_image(session: requests.Session, author_id: str, image_url: str):
    """
    Set an author's image from a URL.

    ⚠️ Verified against the live server 2026-09-02 rather than assumed:
    `OPTIONS /api/authors/<id>/image` answers `Allow: GET,HEAD,POST,DELETE`, so
    POST is the verb and DELETE is the undo. (`PATCH /api/authors/<id>` exists
    too but is for metadata, not the image.)

    ⚠️ The body takes a URL only, and ABS fetches it itself, server-side — so
    the cover must be on the public covers CDN. That is why an ebook-only
    author stays blank: putting their cover there would put an ebook title on
    a public host (the owner's no-public-ebook-titles rule).

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
    ap.add_argument("--prune-empty", action="store_true",
                    help="delete author records that hold nothing (see is_empty_author)")
    ap.add_argument("--json-summary", action="store_true")
    args = ap.parse_args(argv)

    missing = [
        v for v in ("ABS_BASE_URL", "ABS_USERNAME", "ABS_PASSWORD", "ABS_LIBRARY_ID",
                    "ABS_CF_CLIENT_ID", "ABS_CF_CLIENT_SECRET")
        if not os.getenv(v)
    ]
    if missing:
        say(f"[ERROR] missing env vars: {', '.join(missing)}")
        return 1

    import build_shelf_map as shelf  # noqa: E402

    rows = load_catalog()
    session = abs_session()
    session.headers["Authorization"] = f"Bearer {abs_login(session)}"
    ebook_lib = os.getenv("ABS_EBOOK_LIBRARY_ID", "")
    audio_items = shelf.abs_get_library_items(session, ABS_LIBRARY_ID)
    ebook_items = shelf.abs_get_library_items(session, ebook_lib) if ebook_lib else []
    row_by_item = item_row_map(rows, audio_items, ebook_items)
    say(f"{len(rows)} catalogue books; {len(row_by_item)} shelf items joined to one")

    libraries = [("Audio", ABS_LIBRARY_ID)] + ([("Ebooks", ebook_lib)] if ebook_lib else [])
    authors = {lab: abs_get_authors(session, lib) for lab, lib in libraries}
    # ⚠️ Two Audio records can share a key ("Brandon Sanderson" with 38 books
    # beside an empty "Sanderson, Brandon", measured 2026-10-16); the one
    # holding books must win, or the Ebooks twin inherits the empty one.
    audio_by_name: Dict[str, Dict] = {}
    # Empty records never count as a twin: they hold no books, and with
    # --prune-empty they are deleted before the Ebooks pass reaches them
    # (a 404 that killed the first full run, 2026-10-16).
    for a in sorted(authors["Audio"], key=lambda x: x.get("numBooks") or 0):
        if not is_empty_author(a):
            audio_by_name[name_key(a.get("name") or "")] = a

    totals = {"set": 0, "had_image": 0, "no_cover": 0, "failed": 0, "pruned": 0}
    for lab, _lib in libraries:
        blank: List[str] = []
        for a in sorted(authors[lab], key=lambda x: (x.get("name") or "")):
            name = (a.get("name") or "").strip()
            if is_empty_author(a):
                # Re-check against the author's own items before deleting:
                # a numBooks of 0 from the list is a count, not proof.
                if not args.prune_empty or abs_author_item_ids(session, a["id"]):
                    continue
                if args.dry_run:
                    say(f"  [DRY] prune [{lab}] {name}")
                else:
                    r = session.delete(f"{ABS_BASE_URL.rstrip('/')}/api/authors/{a['id']}", timeout=60)
                    if r.status_code != 200:
                        totals["failed"] += 1
                        say(f"  [FAIL] prune [{lab}] {name} — HTTP {r.status_code}")
                        continue
                totals["pruned"] += 1
                continue
            if a.get("imagePath") and not args.force:
                totals["had_image"] += 1
                continue
            # ⚠️ An Ebooks author who also has audiobooks gets the AUDIO
            # author's pick, so one person wears one cover on both shelves.
            source = audio_by_name.get(name_key(name)) if lab != "Audio" else None
            item_ids = abs_author_item_ids(session, (source or a)["id"])
            url = pick_cover_for_author(name, rows_for_items(item_ids, row_by_item))
            if not url:
                totals["no_cover"] += 1
                blank.append(name)
                continue
            if args.limit and totals["set"] >= args.limit:
                break
            if args.dry_run:
                say(f"  [DRY] [{lab}] {name} -> {url}")
                totals["set"] += 1
                continue
            ok, detail = abs_set_author_image(session, a["id"], url)
            if ok:
                totals["set"] += 1
                say(f"  [SET] [{lab}] {name}")
            else:
                totals["failed"] += 1
                say(f"  [FAIL] [{lab}] {name} — {detail}")
            time.sleep(RATE_LIMIT_SECONDS)
        if blank:
            # ⚠️ Named, never a silent skip. In Audio this means a book the
            # catalogue does not hold; in Ebooks it is expected (ebook-only).
            say(f"  [{lab}] no public cover for {len(blank)}: " + ", ".join(blank[:15])
                + (" ..." if len(blank) > 15 else ""))

    say(f"{'DRY RUN' if args.dry_run else 'DONE'}: "
        + ", ".join(f"{k} {v}" for k, v in totals.items()))
    if args.json_summary:
        print("AUTHOR_IMAGES_JSON " + json.dumps(dict(totals, dry_run=args.dry_run)))
    return 1 if totals["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
