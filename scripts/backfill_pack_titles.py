"""Put a `title` on every row of ingest_state.json, read from the packs themselves.

WHY THIS EXISTS
---------------
`app/tools/ingest_books.py publish_index()` builds the served index straight out
of `ingest_state.json`:

    entries = [{"book_id": bid, **e} for bid, e in state["books"].items()]

so the index carries exactly the fields the state carries. The state never
recorded a title — `mark()` was called with chunks, bytes, key and version, and
nothing else — so every row of `ebooks-gated/text/_index.json.gz` reached the
serving layer with no title at all. A reader that does `row.title || ''` sees an
empty string on all 182 rows, which is how this was found.

⚠️ THE GOING-FORWARD FIX IS IN `pack_one()`, NOT HERE. That is where `mark()`
now records `title=book.title`, so newly packed books need no backfill. This
script is the ONE-TIME migration for rows packed before that change, and it is
idempotent so re-running it is a no-op rather than a hazard.

WHERE THE TITLE COMES FROM, AND WHY THAT SOURCE
-----------------------------------------------
The pack file — `packs/<book_id>.json.gz` — carries `"title"` written by
`build_pack()` from `ExtractedBook.title`. **That is the same value `pack_one()`
now writes into the state**, so a backfilled row and a freshly packed row hold
the identical string rather than two spellings of one book.

⚠️ DE-SLUGGING THE book_id WAS THE OTHER OPTION AND IT IS A FABRICATION. It
cannot restore an apostrophe ("a-killer-s-mind" → "A Killer's Mind"), a colon, a
capital inside a word, or the difference between "MM" and "mm" — and
`processing-board.mjs` already refuses to do it for the same reason. A row whose
pack cannot be read is REPORTED AND LEFT ALONE; a wrong title is worse than a
missing one, because a missing one is visibly missing.

USAGE
-----
    python -m scripts.backfill_pack_titles            # dry run — prints, writes nothing
    python -m scripts.backfill_pack_titles --apply    # writes ingest_state.json

Publishing the index is deliberately NOT part of this script — run
`python -m app.tools.ingest_books --publish-index` afterwards, so the state write
and the R2 publish stay two decisions a human can take separately.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.ingest_queue import PACKS_DIR, STATE_PATH, load_state, save_state


def title_from_pack(book_id: str, packs_dir: Path = PACKS_DIR) -> Optional[str]:
    """The pack's own `title`, or None if it cannot be read as a real one.

    ⚠️ Never raises and never guesses. A missing pack, a truncated gzip, broken
    JSON, a missing key and a blank/non-string title all return None — the
    caller reports the row and leaves it untouched.
    """
    path = packs_dir / f"{book_id}.json.gz"
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            pack = json.load(fh)
    except Exception:
        return None
    if not isinstance(pack, dict):
        return None
    title = pack.get("title")
    if not isinstance(title, str):
        return None
    title = title.strip()
    return title or None


def backfill(state: dict, packs_dir: Path = PACKS_DIR) -> dict:
    """Fill in missing titles. Returns a report; mutates `state` in place.

    Idempotent by construction: a row that already holds a non-empty string
    title is left exactly as it is, so this never overwrites a title recorded at
    pack time with one re-read from disk.
    """
    filled: list[tuple[str, str]] = []
    already: list[str] = []
    unresolved: list[str] = []

    for book_id, entry in (state.get("books") or {}).items():
        if not isinstance(entry, dict):
            continue
        existing = entry.get("title")
        if isinstance(existing, str) and existing.strip():
            already.append(book_id)
            continue
        title = title_from_pack(book_id, packs_dir)
        if title is None:
            unresolved.append(book_id)
            continue
        entry["title"] = title
        filled.append((book_id, title))

    return {"filled": filled, "already": already, "unresolved": unresolved}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="write ingest_state.json (default: dry run)")
    ap.add_argument("--state", type=Path, default=STATE_PATH)
    ap.add_argument("--packs", type=Path, default=PACKS_DIR)
    args = ap.parse_args(argv)

    state = load_state(args.state)
    report = backfill(state, args.packs)

    for book_id, title in report["filled"]:
        print(f"  + {book_id}  ->  {title!r}")
    # ⚠️ Unresolved rows are NAMED, not counted. "3 rows could not be resolved"
    # is a number nobody can act on; the ids are what a human needs to go and
    # look at the pack.
    for book_id in report["unresolved"]:
        print(f"  ? {book_id}  ->  no readable title in {args.packs / (book_id + '.json.gz')}")

    print(f"[titles] {len(report['filled'])} filled, "
          f"{len(report['already'])} already had one, "
          f"{len(report['unresolved'])} unresolved "
          f"({len(state.get('books') or {})} rows total)")

    if not report["filled"]:
        print("[titles] nothing to write.")
        return 0
    if not args.apply:
        print("[titles] DRY RUN — pass --apply to write. "
              "Then: python -m app.tools.ingest_books --publish-index")
        return 0

    save_state(state, args.state)
    print(f"[titles] wrote {args.state}. "
          "Now run: python -m app.tools.ingest_books --publish-index")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
