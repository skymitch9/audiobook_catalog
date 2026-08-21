#!/usr/bin/env python3
"""Undo the incorrect author-folder moves of 2026-08-09.

## What went wrong

`app/tools/book_sort.py::organize_by_author()` — a **superseded** script; the
current sorter is `sync_to_drive.sort_books()` — was hand-run over the library
and applied `scripts/author_aliases.json` as a *local shelving* table. That file
documents itself as a **Drive-folder routing** table. One map, two incompatible
jobs.

Full investigation: `docs/info/author-folder-audit.md`.

Of nine move classes it performed, the audit found six correct, two a judgement
call, and one objectively wrong:

  ❌ `William D. Arand → Randi Darren`, 26 files.

Randi Darren and William D. Arand are pen names of one person with **separate,
non-overlapping bibliographies**. The 17 files already in `Randi Darren\\` were
exactly the Darren canon (Wild Wastes, Fostering Faust, Remnant, Incubus Inc.,
System Overclocked). All 26 that moved in are Arand titles. **The pre-move split
was correct and the move destroyed it.**

## What this reverts

| Class | Files | Why |
|---|---|---|
| `Randi Darren` → `William D. Arand` | 26 | objectively wrong |
| `T.L. Payne` → `T. L. Payne` | 1 | owner's decision: `T. L. Payne` is canonical |

Everything else is **left alone** — the initials fixes, the romanisation, and
`Alex Toxic → Nadya Lee`, all of which the audit verified as correct.

It also removes the two now-wrong lines from `author_aliases.json`, so the next
run of anything reading that map cannot redo the damage.

## Safety

- **Never overwrites.** A destination that already exists aborts that file and is
  reported; nothing is silently clobbered.
- **Moves, never copies-then-deletes** within the same volume, so a file cannot
  exist in a half-written state.
- **Counts and total bytes are compared before and after.** The run fails loudly
  if they differ by a single byte.
- Dry run is the default.

⚠️ No non-ASCII in printed output. Windows consoles default to cp1252, and a
tick character raised UnicodeEncodeError *after* a successful run — the moves and
the verification had both completed, and the script still exited non-zero. A
destructive script that crashes on its own success message is a script you cannot
trust the exit code of.

    python scripts/revert_author_moves.py            # show the plan
    python scripts/revert_author_moves.py --commit
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import ROOT_DIR  # noqa: E402

ALIASES = PROJECT_ROOT / "scripts" / "author_aliases.json"

# (moved_to, correct_home) — reverting means moving back from the first to the
# second. Only classes the audit called wrong, plus the owner's Payne decision.
REVERTS = [
    ("Randi Darren", "William D. Arand"),
    ("T.L. Payne", "T. L. Payne"),
]

# ⚠️ The 17 files that were ALREADY in `Randi Darren` before the move are the
# genuine Darren canon and must NOT be moved out. Identified by series, which is
# how the audit distinguished them — every Darren title belongs to one of these.
DARREN_CANON_MARKERS = (
    "wild wastes",
    "fostering faust",
    "remnant",
    "incubus inc",
    "system overclocked",
    "privateer's commission",
    "privateers commission",
)

# Aliases that caused the wrong moves. Removing them stops a re-run redoing it.
ALIAS_KEYS_TO_DROP = ["William D. Arand", "T. L. Payne"]


def is_darren_canon(name: str) -> bool:
    low = name.lower()
    return any(marker in low for marker in DARREN_CANON_MARKERS)


def inventory(root: Path) -> dict[str, int]:
    """Every m4b by relative path -> size. The proof that nothing is lost."""
    out = {}
    for p in root.rglob("*.m4b"):
        if p.is_file():
            out[str(p.relative_to(root)).replace("\\", "/")] = p.stat().st_size
    return out


def main() -> int:
    # description is NOT __doc__: it contains non-ASCII, and argparse prints it
    # to the same cp1252 console that crashed the success message.
    parser = argparse.ArgumentParser(
        description="Undo the incorrect author-folder moves of 2026-08-09. "
                    "See the module docstring and docs/info/author-folder-audit.md."
    )
    parser.add_argument("--commit", action="store_true", help="actually move files")
    args = parser.parse_args()

    root = Path(ROOT_DIR)
    if not root.is_dir():
        print(f"ROOT_DIR not found: {root}")
        return 1

    before = inventory(root)
    print(f"before: {len(before)} m4b files, {sum(before.values()):,} bytes\n")

    planned: list[tuple[Path, Path]] = []
    kept_canon: list[str] = []
    blocked: list[str] = []

    for moved_to, correct_home in REVERTS:
        src_dir = root / moved_to
        dst_dir = root / correct_home
        if not src_dir.is_dir():
            print(f"[skip] {moved_to} does not exist")
            continue

        # Top level only. Subfolders under an author dir are a different thing
        # (the audit found a 43-file duplicate folder under Randi Darren) and are
        # deliberately out of scope for this revert.
        for f in sorted(src_dir.glob("*.m4b")):
            if moved_to == "Randi Darren" and is_darren_canon(f.name):
                kept_canon.append(f.name)
                continue
            target = dst_dir / f.name
            if target.exists():
                blocked.append(f"{moved_to}/{f.name} — already exists at destination")
                continue
            planned.append((f, target))

    print(f"to move back: {len(planned)}")
    for src, dst in planned:
        print(f"  {src.parent.name}/{src.name}\n      -> {dst.parent.name}/")
    if kept_canon:
        print(f"\nleft in Randi Darren (genuine Darren canon): {len(kept_canon)}")
        for n in kept_canon:
            print(f"  {n}")
    if blocked:
        print(f"\n[WARN] BLOCKED (nothing overwritten): {len(blocked)}")
        for b in blocked:
            print(f"  {b}")

    if not args.commit:
        print("\nDRY RUN. Nothing moved. Re-run with --commit.")
        return 0

    moved = 0
    for src, dst in planned:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            print(f"  [abort] {dst} appeared mid-run - skipped")
            continue
        src.rename(dst)
        moved += 1
    print(f"\nmoved {moved} file(s)")

    # Drop the aliases that caused it, so nothing can redo the damage.
    try:
        data = json.loads(ALIASES.read_text(encoding="utf-8"))
        dropped = [k for k in ALIAS_KEYS_TO_DROP if k in data]
        for k in dropped:
            del data[k]
        if dropped:
            ALIASES.write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            print(f"removed alias(es): {', '.join(dropped)}")
    except Exception as e:
        print(f"[WARN] could not update {ALIASES.name}: {e}")

    # Remove folders that are now empty of audiobooks.
    for moved_to, _ in REVERTS:
        d = root / moved_to
        if d.is_dir() and not any(d.rglob("*.m4b")):
            leftovers = [p for p in d.rglob("*") if p.is_file()]
            if leftovers:
                print(f"kept {moved_to}/ - still holds {len(leftovers)} non-audio file(s)")
            else:
                try:
                    for sub in sorted((p for p in d.rglob("*") if p.is_dir()), reverse=True):
                        sub.rmdir()
                    d.rmdir()
                    print(f"removed empty folder: {moved_to}/")
                except OSError as e:
                    print(f"kept {moved_to}/ - {e}")

    after = inventory(root)
    print(f"\nafter: {len(after)} m4b files, {sum(after.values()):,} bytes")

    # ⚠️ The whole point. Same count, same bytes, or something went wrong.
    if len(after) != len(before) or sum(after.values()) != sum(before.values()):
        print("\n❌ FILE COUNT OR SIZE CHANGED - investigate immediately")
        lost = set(before) - set(after)
        gained = set(after) - set(before)
        for p in sorted(lost)[:20]:
            print(f"  missing: {p}")
        for p in sorted(gained)[:20]:
            print(f"  new:     {p}")
        return 1

    print("[OK] every file accounted for - same count, same total bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
