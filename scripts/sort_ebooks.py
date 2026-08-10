#!/usr/bin/env python3
"""Move loose ebooks at the library root into their author's folder.

## Why this exists separately from `app/tools/book_sort.py`

That script does the same job for **audiobooks**: read the author, resolve it
through the shelving aliases, move the file into `<ROOT>/<Author>/`. It cannot
be pointed at ebooks, because it gets the author from `get_author_name()`, which
reads audio tags. An EPUB has no audio tags; its author is in the OPF.

So this is the same shape with a different author source, and **deliberately the
same `resolve_shelf_author()`** from `app/author_names.py`. That sharing is not
tidiness — `docs/info/author-folder-audit.md` §7 records what happened on
2026-08-09 when a sorter reached for `author_aliases.json` (a *Drive-routing*
table) instead of `author_shelf_aliases.json`: it merged two pen names with
separate bibliographies and 27 files had to be reverted. One shared function is
how that stays fixed.

## What it does NOT do

- **Never overwrites.** A name collision is reported and skipped.
- **Never renames.** The filename is carried across unchanged.
- **Never invents an author.** No OPF author and no parseable filename means the
  file is left where it is and listed, which is the honest outcome.
- **Never moves a file that is already in a folder.** Only the library root is
  considered loose; anything already shelved is somebody's decision.

## Reversibility

Every committed run writes `scripts/ebook_sort_manifest.json` — the exact
from/to pairs — before touching anything, so a bad run can be undone the way
`revert_author_moves.py` undid the 2026-08-09 one. It is written first and
flushed, so it exists even if the run dies halfway.

## ⚠️ After a committed run, two things are stale

1. `site/ebooks.json` — regenerate with `python scripts/build_ebook_manifest.py`.
2. `library_catalog`'s `edition.source_url`, which stores these paths. Fix with
   `node scripts/relink-ebook-paths.mjs` in that repo, using the manifest this
   writes. Skipping it makes `--prune` see every moved book as an orphan.

    python scripts/sort_ebooks.py             # show the plan
    python scripts/sort_ebooks.py --commit
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.author_names import load_shelf_aliases, resolve_shelf_author  # noqa: E402
from app.config import ROOT_DIR  # noqa: E402
from app.metadata import COMPANION_EXTS  # noqa: E402
from scripts.rename_epubs import get_epub_metadata  # noqa: E402

MANIFEST_PATH = PROJECT_ROOT / "scripts" / "ebook_sort_manifest.json"

# "Title - Author Name.epub" is how the loose files are named. Used only when the
# file carries no OPF author at all — a PDF, or an EPUB with empty metadata.
FILENAME_AUTHOR = re.compile(r"\s-\s([^-]+?)\s*$")


def author_for(path: Path) -> tuple[str | None, str]:
    """Return (author, where-it-came-from). The OPF wins; the filename is a fallback."""
    if path.suffix.lower() == ".epub":
        meta = get_epub_metadata(path) or {}
        author = (meta.get("author") or "").strip()
        if author:
            return author, "opf"
    m = FILENAME_AUTHOR.search(path.stem)
    if m:
        candidate = m.group(1).strip()
        # Guard against a subtitle being mistaken for a person: "A LitRPG
        # Adventure" is not an author. A real name here is short and has no
        # leading article.
        if candidate and len(candidate) <= 60 and not candidate.lower().startswith(("a ", "an ", "the ")):
            return candidate, "filename"
    return None, "none"


def existing_folder(root: Path, name: str) -> Path:
    """Match an existing author folder case-insensitively.

    Windows is case-insensitive but Git and Drive are not, so creating
    `will wight` beside `Will Wight` produces two shelves that look like one
    locally and diverge everywhere else.
    """
    target = name.casefold()
    for child in root.iterdir():
        if child.is_dir() and child.name.casefold() == target:
            return child
    return root / name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--commit", action="store_true", help="actually move files")
    args = parser.parse_args()

    root = Path(ROOT_DIR)
    loose = sorted(
        p for p in root.iterdir()
        if p.is_file() and p.suffix.lower() in COMPANION_EXTS
    )

    if not loose:
        print(f"Nothing loose at {root}. All ebooks are already shelved.")
        return 0

    aliases = load_shelf_aliases()
    moves: list[dict[str, str]] = []
    skipped: list[str] = []

    for f in loose:
        raw, source = author_for(f)
        if not raw:
            skipped.append(f"{f.name}  — no author in the file and none parseable from the name")
            continue

        shelf = resolve_shelf_author(raw, aliases)
        dest_dir = existing_folder(root, shelf)
        dest = dest_dir / f.name

        if dest.exists():
            skipped.append(f"{f.name}  — {dest_dir.name}/ already holds a file of this name")
            continue

        note = "" if dest_dir.exists() else "  (new folder)"
        alias_note = f"  [alias: {raw} -> {shelf}]" if shelf != raw else ""
        moves.append({
            "from": str(f.relative_to(root)).replace("\\", "/"),
            "to": str(dest.relative_to(root)).replace("\\", "/"),
            "author": shelf,
            "author_source": source,
        })
        print(f"  {f.name[:62]:64} -> {dest_dir.name}/{note}{alias_note}")

    print(f"\n{len(moves)} to move, {len(skipped)} skipped")
    for s in skipped:
        print(f"  [skip] {s}")

    if not args.commit:
        print("\nDRY RUN. Nothing moved. Re-run with --commit.")
        return 0

    if not moves:
        print("\nNothing to do.")
        return 0

    # ⚠️ Written and flushed BEFORE the first move, so a run that dies halfway
    # still leaves a complete record of what it intended to do.
    MANIFEST_PATH.write_text(json.dumps(moves, indent=2), encoding="utf-8")
    print(f"\nwrote {MANIFEST_PATH.relative_to(PROJECT_ROOT)} ({len(moves)} entries)")

    done = 0
    for m in moves:
        src, dst = root / m["from"], root / m["to"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            src.rename(dst)
            done += 1
        except OSError as e:
            print(f"  [FAIL] {m['from']} -> {m['to']}: {e}")

    print(f"\n[OK] moved {done} of {len(moves)}")
    print("\nNow, in order:")
    print("  1. python scripts/build_ebook_manifest.py       # site/ebooks.json still has the old paths")
    print("  2. (library_catalog) node scripts/relink-ebook-paths.mjs --remote --commit")
    print("     ^ without this, edition.source_url is stale and --prune sees every moved book as an orphan")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
