# app/tools/book_sort.py
# Move audiobook files into subfolders named after the primary author.
# Uses ROOT_DIR and EXTS from app.config (set via .env).
#
# ⚠️ SUPERSEDED. The live sorter is scripts/sync_to_drive.py sort_books()
# (ARCHITECTURE.md STEP 1) and it now does everything this does, including
# alias resolution — the gap that used to make hand-running this file
# necessary. Kept only because a whole-library pass is occasionally useful.
# Both paths share app/author_names.py so they cannot drift apart again;
# that shared module is also where get_author_name now lives.

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

# The tag reader and the shelf map both live in one place now, so a hand-run of
# this script and a pipeline run reach the same answer for every file.
from app.author_names import (  # noqa: F401  (get_author_name re-exported for callers)
    get_author_name,
    load_shelf_aliases,
    resolve_shelf_author,
)
from app.config import EXTS, ROOT_DIR


def organize_by_author(root_dir: Path, exts: set[str], recursive: bool = True, dry_run: bool = False) -> None:
    """
    Moves files under root_dir into subfolders named after the detected author.
    - Only files with extensions in `exts` are processed.
    - If a file already resides in an 'Author' folder that matches the author, it is skipped.
    - Applies the local shelving aliases (scripts/author_shelf_aliases.json).
    - Set dry_run=True to preview without moving.
    """
    aliases = load_shelf_aliases()

    if recursive:
        files = [p for p in root_dir.rglob("*") if p.is_file() and p.suffix.lower() in exts]
    else:
        files = [p for p in root_dir.iterdir() if p.is_file() and p.suffix.lower() in exts]

    if not files:
        print(f"No audiobook files found in: {root_dir}")
        return

    for f in files:
        author = get_author_name(f)
        if not author:
            print(f"Skipping (no author): {f.relative_to(root_dir)}")
            continue

        # Shelf aliases only. This used to read scripts/author_aliases.json,
        # a Drive-routing table, and on 2026-08-09 that merged two pen names
        # with separate bibliographies. See docs/info/author-folder-audit.md.
        author = resolve_shelf_author(author, aliases)

        # Target folder directly under ROOT_DIR
        author_folder = root_dir / author

        # If already in the correct author folder, skip
        try:
            parent_rel = f.parent.relative_to(root_dir)
            if parent_rel.parts and parent_rel.parts[0].lower() == author.lower():
                # already inside the 'Author/' folder
                continue
        except Exception:
            # If file is not under root_dir (shouldn't happen), we still try to move
            pass

        dest = author_folder / f.name

        # ASCII arrows on purpose: Windows consoles are cp1252 and a "→" here
        # raised UnicodeEncodeError mid-run, so a preview could not be read to
        # the end. Same lesson as scripts/revert_author_moves.py.
        if dest.exists():
            print(f"Exists -> skip: {dest.relative_to(root_dir)}")
            continue

        print(f"Move: {f.relative_to(root_dir)}  ->  {dest.relative_to(root_dir)}")
        if not dry_run:
            author_folder.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(f), str(dest))
            except Exception as e:
                print(f"[ERROR] Move failed: {f} -> {dest} ({e})")


def main():
    # Dry run is the default. This used to move ~25 GB with no flag and no
    # prompt, which is how the 2026-08-09 incident got as far as it did.
    # --execute matches scripts/migrate_folder_names.py's convention.
    parser = argparse.ArgumentParser(
        description="Superseded whole-library author sorter. Prefer "
                    "scripts/sync_to_drive.py --sort-only."
    )
    parser.add_argument("--execute", action="store_true", help="actually move files")
    args = parser.parse_args()

    # Safety: ensure ROOT_DIR exists
    if not ROOT_DIR.exists():
        print(f"[ERROR] ROOT_DIR not found: {ROOT_DIR}")
        return

    organize_by_author(ROOT_DIR, EXTS, recursive=True, dry_run=not args.execute)
    if not args.execute:
        print("\nDRY RUN. Nothing moved. Re-run with --execute.")


if __name__ == "__main__":
    main()
