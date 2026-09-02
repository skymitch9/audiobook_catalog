"""
Reunite Audiobookshelf author records that one book split in two.

THE SYMPTOM (owner, 2026-09-02, looking at the shelf home page)
---------------------------------------------------------------
The "Newest Authors" row was ten author cards and every one of them was wrong:
`Sanderson, Brandon` sitting beside the real `Brandon Sanderson`, three
spellings of the Arcane Pathfinder duo, `the Mad, Sir Bedivere`, `English,
Miles`. Measured against the live library the same day, it was not ten — it was
**24 records**, and the largest was `Wight, Will` holding 15 books away from the
3 under `Will Wight`.

THE CAUSE, MEASURED RATHER THAN GUESSED
---------------------------------------
Every one of them is an **ebook-only item** (`numAudioFiles=0`,
`ebookFormat=epub`), and the name comes from the publisher's `<dc:creator>`
inside the EPUB, which vendors write in SORT order:

    Will Wight/The Captain … .m4b   ©ART   = 'Will Wight'      <- clean
    site/catalog.csv                author = 'Will Wight'      <- clean
    the shelf folder                         'Will Wight'      <- clean
    Blackflame (Cradle Book 3).epub dc:creator = 'Wight, Will' <- the source
    Audiobookshelf                  authorName = 'Wight, Will' <- read verbatim

So nothing in this repo mints these names. `scripts/rename_epubs.py` already
un-flips the name for the FILENAME, and since 2026-09-02 does so without
inventing people (see `normalize_creator`), but it does not rewrite the bytes
inside the epub — which is what ABS reads. That is why this script exists: the
records are already there, and only an ABS-side rename can join them.

⚠️ ABS MERGES ON COLLISION. `PATCH /api/authors/<id> {"name": …}` renames when
the name is free and MERGES into the existing author when it is taken. Both
outcomes are wanted, and each row of the table says which one it expects.

⚠️ NO FUZZY MATCHING, ANYWHERE. Every merge is a row in
`scripts/abs_author_merges.json`, written by hand, carrying the source id, the
expected book count and the target's id. Before any write this script fetches
the live author list and ASSERTS every row against it; a row whose live state
has drifted is REFUSED BY NAME and the run stops. There is no "closest match",
no lowercase-and-hope, no repair of a stale row.

⚠️ AUTHORSHIP, NOT SHELVING. The target of every row is the plain un-flip of
its source. `scripts/author_shelf_aliases.json` says 'Rik Hoskin' ->
'Brandon Sanderson' and 'Travis Deverell' -> 'Shirtaloon'; those entries decide
WHERE FILES LIVE and are deliberately not applied here. `app/author_names.py`
opens by recording the 2026-08-09 incident in which a Drive-routing line was
executed as a shelving instruction and merged two people's bibliographies.
Reading that map as an authorship map would repeat it in the other direction.

USAGE
-----
    .venv\\Scripts\\python scripts/merge_abs_authors.py                # DRY RUN
    .venv\\Scripts\\python scripts/merge_abs_authors.py --commit       # do it
    .venv\\Scripts\\python scripts/merge_abs_authors.py --verify       # after

Dry run is the default and writes nothing. Exit 0 = every row applied or
verified; exit 1 = an assertion failed, a write failed, or the table drifted.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
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

# Imported for its side effect as much as its name: app/__init__.py forces
# UTF-8 stdio, so a CJK author name in this list cannot kill the run mid-write
# (KI-3, which bit set_author_images.py on an author called 猫子).
from app.core.console import force_utf8_stdio  # noqa: E402

force_utf8_stdio()

MERGES_PATH: Path = PROJECT_ROOT / "scripts" / "abs_author_merges.json"

ABS_BASE_URL: str = os.getenv("ABS_BASE_URL", "")
ABS_USERNAME: str = os.getenv("ABS_USERNAME", "")
ABS_PASSWORD: str = os.getenv("ABS_PASSWORD", "")
ABS_LIBRARY_ID: str = os.getenv("ABS_LIBRARY_ID", "")
ABS_CF_CLIENT_ID: str = os.getenv("ABS_CF_CLIENT_ID", "")
ABS_CF_CLIENT_SECRET: str = os.getenv("ABS_CF_CLIENT_SECRET", "")

RATE_LIMIT_SECONDS: float = 0.25


def say(msg: str = "") -> None:
    print(msg)


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------
def load_merges(path: Path = MERGES_PATH) -> List[Dict]:
    """Read the hand-written merge table. Keys starting with '_' are prose."""
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    rows = data.get("merges", [])
    seen_from: set = set()
    for row in rows:
        for key in ("from", "from_id", "to", "expect_books"):
            if key not in row:
                raise ValueError(f"merge row missing {key!r}: {row}")
        if row["from"] in seen_from:
            raise ValueError(f"duplicate source author in table: {row['from']!r}")
        seen_from.add(row["from"])
        if row["from"] == row["to"]:
            raise ValueError(f"row renames {row['from']!r} to itself")
    return rows


# ---------------------------------------------------------------------------
# ABS API — same credential path as scripts/set_author_images.py
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
        say("[ERROR] login succeeded but no token in response")
        sys.exit(1)
    return token


def abs_get_authors(session: requests.Session) -> List[Dict]:
    resp = session.get(
        f"{ABS_BASE_URL.rstrip('/')}/api/libraries/{ABS_LIBRARY_ID}/authors", timeout=120
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("authors", data if isinstance(data, list) else [])


def abs_rename_author(session: requests.Session, author_id: str, new_name: str):
    """Rename, which ABS turns into a MERGE when the name is taken.

    Returns (ok, merged, detail).
    """
    resp = session.patch(
        f"{ABS_BASE_URL.rstrip('/')}/api/authors/{author_id}",
        json={"name": new_name},
        timeout=120,
    )
    if resp.status_code != 200:
        return False, False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return True, bool(body.get("merged")), ""


# ---------------------------------------------------------------------------
# Assertions — the whole point of the script
# ---------------------------------------------------------------------------
def check_rows(rows: List[Dict], authors: List[Dict]) -> tuple[List[Dict], List[str]]:
    """Assert every row against live ABS. Returns (applicable rows, problems).

    ⚠️ A row is checked, never repaired. If the live state disagrees with what
    somebody wrote down, the human who wrote it is the one who should look —
    silently adapting is how a merge table starts merging the wrong people.
    """
    by_id = {a.get("id"): a for a in authors}
    by_name = {(a.get("name") or ""): a for a in authors}
    problems: List[str] = []
    applicable: List[Dict] = []

    # Names this run will CREATE, so a later row can legitimately expect a
    # target that does not exist yet (the Hoskin pair does exactly this).
    will_exist = set(by_name)

    for row in rows:
        src = by_id.get(row["from_id"])
        if src is None:
            if row["from"] not in by_name and row["to"] in will_exist:
                # Already applied — idempotent, not a problem.
                applicable.append({**row, "_state": "already-applied"})
                continue
            problems.append(f"{row['from']!r}: id {row['from_id']} not in the library")
            continue
        if (src.get("name") or "") != row["from"]:
            problems.append(
                f"{row['from']!r}: id {row['from_id']} is now named "
                f"{src.get('name')!r} — the table is stale, not the server"
            )
            continue
        if src.get("numBooks") != row["expect_books"]:
            problems.append(
                f"{row['from']!r}: holds {src.get('numBooks')} books, table says "
                f"{row['expect_books']} — re-measure before merging"
            )
            continue

        target = by_name.get(row["to"])
        expected_target = row.get("expect_merge_into")
        if expected_target and (target is None or target.get("id") != expected_target):
            problems.append(
                f"{row['from']!r}: expected to merge into {row['to']!r} "
                f"({expected_target}), live target is "
                f"{(target or {}).get('id')!r}"
            )
            continue
        if expected_target is None and target is not None and row["to"] not in will_exist - set(by_name):
            # A target appeared that the table said would not be there. That
            # changes a rename into a merge, which is a different act.
            if target.get("id") != row["from_id"]:
                problems.append(
                    f"{row['from']!r}: table says RENAME to {row['to']!r}, but "
                    f"that author already exists ({target.get('id')}) — it would "
                    f"MERGE instead. Confirm and set expect_merge_into"
                )
                continue

        # ⚠️ `will_exist`, not `by_name`: an earlier row in this same run can
        # create the target ('Hoskin, Rik' -> 'Rik Hoskin' precedes
        # 'Rik Hoskin, Julius Gopez' -> 'Rik Hoskin'). Reading only the
        # pre-run list would label that second row a RENAME in the dry run and
        # a MERGE in the real one, which is exactly the kind of preview a
        # person stops trusting.
        merges_now = row["to"] in will_exist
        will_exist.add(row["to"])
        applicable.append({
            **row,
            "_state": "merge" if merges_now else "rename",
            "_target_books": (target or {}).get("numBooks"),
        })

    return applicable, problems


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--commit", action="store_true",
                    help="actually write. Default is a dry run that writes nothing")
    ap.add_argument("--verify", action="store_true",
                    help="check the outcome of a previous run and write nothing")
    args = ap.parse_args(argv)

    missing = [
        v for v in ("ABS_BASE_URL", "ABS_USERNAME", "ABS_PASSWORD", "ABS_LIBRARY_ID",
                    "ABS_CF_CLIENT_ID", "ABS_CF_CLIENT_SECRET")
        if not os.getenv(v)
    ]
    if missing:
        say(f"[ERROR] missing env vars: {', '.join(missing)}")
        return 1

    rows = load_merges()
    say(f"Merge table: {MERGES_PATH.name} — {len(rows)} rows")

    session = abs_session()
    say("Logging in to Audiobookshelf...")
    session.headers["Authorization"] = f"Bearer {abs_login(session)}"
    authors = abs_get_authors(session)
    say(f"  {len(authors)} authors in the library\n")

    if args.verify:
        return verify(rows, authors)

    applicable, problems = check_rows(rows, authors)
    if problems:
        say(f"--- {len(problems)} ROW(S) REFUSED — the table and the server disagree ---")
        for p in problems:
            say(f"  [REFUSED] {p}")
        say("\nNothing was written. Fix the table (or re-measure) and run again.")
        return 1

    say(f"--- {'APPLYING' if args.commit else 'DRY RUN'}: {len(applicable)} rows ---")
    done = failed = merged_count = 0
    for row in applicable:
        if row.get("_state") == "already-applied":
            say(f"  [SKIP]  {row['from']!r} — already applied")
            continue
        verb = "MERGE into" if row["_state"] == "merge" else "RENAME to"
        detail = (f" (+{row['_target_books']} there)"
                  if row.get("_target_books") is not None else "")
        flag = "  ⚠️ JUDGEMENT ROW" if row.get("judgement") else ""
        say(f"  [{'SET' if args.commit else 'DRY'}] {row['from']!r} "
            f"({row['expect_books']} books) — {verb} {row['to']!r}{detail}{flag}")
        if not args.commit:
            done += 1
            continue
        ok, merged, why = abs_rename_author(session, row["from_id"], row["to"])
        if ok:
            done += 1
            merged_count += 1 if merged else 0
        else:
            failed += 1
            say(f"      [FAIL] {why}")
        time.sleep(RATE_LIMIT_SECONDS)

    say(f"\n--- {'SUMMARY' if args.commit else 'DRY RUN SUMMARY'} ---")
    say(f"  rows applied : {done}")
    if args.commit:
        say(f"  of which ABS reported a merge : {merged_count}")
    say(f"  failed       : {failed}")
    if not args.commit:
        say("\n  Nothing was written. Re-run with --commit to apply.")
    return 1 if failed else 0


def verify(rows: List[Dict], authors: List[Dict]) -> int:
    """After a --commit run: are the malformed names gone and the books joined?"""
    by_name = {(a.get("name") or ""): a for a in authors}
    bad = 0
    say("--- VERIFY ---")
    for row in rows:
        if row["from"] in by_name:
            say(f"  [STILL THERE] {row['from']!r} "
                f"({by_name[row['from']].get('numBooks')} books)")
            bad += 1
            continue
        target = by_name.get(row["to"])
        if target is None:
            say(f"  [MISSING]     {row['to']!r} — source is gone and target never appeared")
            bad += 1
            continue
        say(f"  [OK]          {row['from']!r} -> {row['to']!r} "
            f"({target.get('numBooks')} books, portrait="
            f"{'yes' if target.get('imagePath') else 'no'})")
    say(f"\n  {len(rows) - bad} of {len(rows)} rows verified; {bad} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
