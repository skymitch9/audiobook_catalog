"""Look at the curated twin table before it changes anything — and only then
rebuild the catalogue.

WHAT A TWIN IS: two AUDIO EDITIONS of one work that the household owns twice
(an Audible download and a backer copy, a re-recording, a second cut). The
catalogue shows two cards for one book, and `resolve_book_file` refuses the
title as ambiguous. `scripts/catalog_twins.json` records which edition keeps
the catalogue identity; `app/core/catalog_twins.py` applies it at build time.

🔴 NO FILE IS EVER TOUCHED, BY THIS TOOL OR BY THE TABLE IT READS. The retired
edition keeps its bytes on disk, its copy on Drive, its key in the R2
`estate-audio` archive and its `upload_manifest.json` entry. Owner, 2026-09-02:
*"Keep the audible one but make sure all source files stay."* This is a
metadata decision expressed as one fewer row in `site/catalog.csv`.

USAGE
-----
    .venv\\Scripts\\python -m app.tools.catalog_twins            # DRY RUN (default)
    .venv\\Scripts\\python -m app.tools.catalog_twins --commit   # assert, then rebuild

**Dry run is the default and writes nothing.** It walks the real library, runs
the same two filename dedupe passes the build runs, evaluates every entry
against the live files field by field, and prints what WOULD be dropped.

`--commit` runs exactly the same assertions and then calls `app.main.main()` —
the ordinary catalogue rebuild — so the drop reaches `site/catalog.csv`,
`site/index.html` and the staged site. ⚠️ It does NOT push: a metadata-only fix
uploads nothing and nothing rebuilds on its own downstream, so follow it with
`python scripts/sync_to_drive.py --rebuild-only` (STEP 5 -> 6 -> 7) to commit,
publish and refresh the shared index. That split is deliberate and is the same
one `docs/info/catalog-corrections.md` §10 records for the overrides editor.

Exit 0 = every entry asserted (and applied, with `--commit`).
Exit 1 = at least one entry was REFUSED. ⚠️ A refusal is not a crash — it means
both editions stay in the catalogue, which is the safe direction — but it is a
non-zero exit because a refused entry is a duplicate still on the site, and a
refusal nobody sees is the failure this whole shape exists to prevent.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:  # pragma: no cover - import ergonomics
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import EXTS, ROOT_DIR  # noqa: E402
from app.core.catalog_twins import (  # noqa: E402
    ASSERTED_FIELDS,
    DEFAULT_TABLE_PATH,
    apply_catalog_twins,
    load_table,
    probe_file,
)
from app.core.file_dedupe import dedupe_library  # noqa: E402
from app.metadata import walk_library  # noqa: E402


def _walk(root: Path) -> list:
    """The build's own view of the library, up to the twin pass.

    ⚠️ It must be the SAME view or the dry run is lying: the `Copy of ` filter
    and both dedupe passes run here for the same reason they run in
    `app/main.py`, and in the same order.
    """
    exts = set(EXTS) if isinstance(EXTS, (set, list, tuple)) else {".m4b", ".m4a", ".mp4"}
    files = [f for f in walk_library(root, exts) if not f.name.startswith("Copy of ")]
    files, _ = dedupe_library(files)
    return files


def _describe(label: str, spec: dict, root: Path) -> None:
    """Print one side of an entry with its live reading beside the table's."""
    rel = str(spec.get("file") or "?")
    print(f"    {label:<16} {rel}")
    path = root / rel
    if not path.exists():
        print(f"      {'':<16} !! not on disk at this path")
        return
    try:
        live = probe_file(path)
    except Exception as exc:  # noqa: BLE001 - reporting tool
        print(f"      {'':<16} !! unreadable: {exc}")
        return
    for field in ASSERTED_FIELDS:
        want = str(spec.get(field, "")).strip()
        got = str(live.get(field, "")).strip()
        mark = "ok " if want == got else "!! "
        detail = f"{got!r}" if want == got else f"{got!r}  (table says {want!r})"
        print(f"      {mark}{field:<14} {detail}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.tools.catalog_twins",
        description="Assert scripts/catalog_twins.json against the live library. "
                    "Dry run by default; nothing is ever deleted or moved.",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="after asserting, rebuild the catalogue (app.main) so the drop reaches "
             "site/catalog.csv. Still touches no audio file.",
    )
    parser.add_argument(
        "--table",
        type=Path,
        default=DEFAULT_TABLE_PATH,
        help="the twin table to read (default: scripts/catalog_twins.json)",
    )
    args = parser.parse_args(argv)

    root = Path(ROOT_DIR)
    entries, problem = load_table(args.table)
    if problem:
        print(f"!! {problem}")
        return 1

    print(f"twin table : {args.table}")
    print(f"library    : {root}")
    print(f"entries    : {len(entries)}")
    print("mode       : " + ("COMMIT (rebuilds the catalogue)" if args.commit else "DRY RUN (writes nothing)"))
    print()

    for entry in entries:
        print(f"  {entry.get('book') or '<unlabelled entry>'}")
        _describe("SURVIVES", entry.get("survivor") or {}, root)
        _describe("RETIRES (row)", entry.get("retire") or {}, root)
        print()

    files = _walk(root)
    kept, report = apply_catalog_twins(files, root, table_path=args.table)

    print(f"walked {len(files)} file(s) -> {len(kept)} catalogued")
    for rel in report.dropped:
        print(f"  RETIRED FROM THE CATALOGUE (file untouched): {rel}")
    for label, why in report.refused:
        print(f"  REFUSED — {label}: {why}")

    if report.refused:
        print("\n!! at least one entry was refused; BOTH editions stay in the catalogue.")
        print("   Nothing was changed. Fix the table or the library, then re-run.")
        return 1

    if not args.commit:
        print("\nDRY RUN — nothing written. Re-run with --commit to rebuild the catalogue.")
        return 0

    print("\nassertions passed; rebuilding the catalogue…")
    from app.main import main as build_catalog

    build_catalog()
    print("\nsite/catalog.csv and site/index.html are rebuilt.")
    print("⚠️ NOT published: run `python scripts/sync_to_drive.py --rebuild-only` to "
          "commit, push and refresh the shared index.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
