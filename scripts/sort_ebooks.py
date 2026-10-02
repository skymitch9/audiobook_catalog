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

## After a committed run

Nothing needs doing by hand. The moves are a library change, so
`AudiobookFsWatcher` fires a pipeline run, which rebuilds `site/ebooks.json`
(STEP 1b), uploads the moved files to Drive (STEP 4) and R2 (5.75), and
publishes the shelf (5.8) — on the idle path too since 2026-10-02.

The pipeline also runs this same planner itself as STEP 1c
(`sync_to_drive.sort_standalone_ebooks`), so the CLI is for looking at a plan
or filing by hand, not a required step. ⚠️ The old advice to run
`library_catalog/scripts/relink-ebook-paths.mjs` afterwards is gone: that
script no longer exists (checked 2026-10-02) — the library's ebook lane was
retired by the ebook split.

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


def plan_moves(
    root: Path,
    aliases: dict,
    *,
    unattended: bool = False,
    collisions_out: list[dict[str, str]] | None = None,
) -> tuple[list[dict[str, str]], list[str]]:
    """Decide where each loose ebook at ``root`` goes. Moves nothing.

    Returns ``(moves, skipped)``: ``moves`` are from/to dicts relative to
    ``root``; ``skipped`` are human-readable lines naming the file and why.

    ``unattended`` is the pipeline's mode (STEP 1c of sync_to_drive.py), where
    nobody reads the plan before it runs. There a FILENAME-derived author may
    only file a book into a folder that already exists — it never mints a new
    shelf. The OPF is the book's own statement of its author, the same standing
    an audio tag has; "Title - Something.pdf" is a guess, and a guess that
    creates a folder becomes a Drive folder the same run.

    ``collisions_out``, when given, receives the loose files whose author
    folder already holds a file of the same name (``from`` + ``author_folder``)
    INSTEAD of a skip line — the pipeline sets those aside as duplicates. The
    CLI passes nothing and keeps reporting them as skipped.
    """
    loose = sorted(
        p for p in root.iterdir()
        if p.is_file() and p.suffix.lower() in COMPANION_EXTS
    )
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

        if unattended and source != "opf" and not dest_dir.exists():
            skipped.append(
                f"{f.name}  — author '{shelf}' comes from the filename only and has no "
                "folder yet; file it by hand"
            )
            continue

        if dest.exists():
            if collisions_out is not None:
                collisions_out.append({"from": f.name, "author_folder": dest_dir.name})
            else:
                skipped.append(f"{f.name}  — {dest_dir.name}/ already holds a file of this name")
            continue

        moves.append({
            "from": str(f.relative_to(root)).replace("\\", "/"),
            "to": str(dest.relative_to(root)).replace("\\", "/"),
            "author": shelf,
            "author_source": source,
            "new_folder": "" if dest_dir.exists() else "yes",
            "alias_from": raw if shelf != raw else "",
        })

    return moves, skipped


def apply_moves(root: Path, moves: list[dict[str, str]]) -> list[Path]:
    """Carry out a plan from plan_moves(). Returns the destination Paths that
    were actually moved; a failed rename is printed and left for the next run."""
    done: list[Path] = []
    for m in moves:
        src, dst = root / m["from"], root / m["to"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            src.rename(dst)
            done.append(dst)
        except OSError as e:
            print(f"  [FAIL] {m['from']} -> {m['to']}: {e}")
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--commit", action="store_true", help="actually move files")
    args = parser.parse_args()

    root = Path(ROOT_DIR)
    moves, skipped = plan_moves(root, load_shelf_aliases())

    if not moves and not skipped:
        print(f"Nothing loose at {root}. All ebooks are already shelved.")
        return 0

    for m in moves:
        name = Path(m["from"]).name
        note = "  (new folder)" if m["new_folder"] else ""
        alias_note = f"  [alias: {m['alias_from']} -> {m['author']}]" if m["alias_from"] else ""
        print(f"  {name[:62]:64} -> {Path(m['to']).parent.name}/{note}{alias_note}")

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

    done = len(apply_moves(root, moves))

    print(f"\n[OK] moved {done} of {len(moves)}")
    print("\nNothing else to run: the file watcher starts a pipeline run that rebuilds")
    print("site/ebooks.json, uploads the moved files to Drive and R2, and publishes the shelf.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
