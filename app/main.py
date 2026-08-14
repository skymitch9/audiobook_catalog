# app/main.py
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from app.config import (
    DRIVE_FOLDER_URL,
    EXTS,
    OUTPUT_DIR,
    ROOT_DIR,
    SITE_CSV_NAME,
    SITE_DIR,
    SITE_INDEX_NAME,
)
from app.core.file_dedupe import dedupe_library
from app.metadata import extract_metadata, walk_library
from app.writers import render_output_html, stage_site_files, write_csv


def main() -> None:
    # Timestamp strings
    ts = datetime.now()
    stamp = ts.strftime("%Y%m%d_%H%M%S")
    generated_at = ts.strftime("%Y-%m-%d %H:%M:%S")

    # Ensure output directory exists
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Output filenames in output_files/
    out_csv = OUTPUT_DIR / f"audiobook_catalog_{stamp}.csv"
    out_html = OUTPUT_DIR / f"audiobook_catalog_{stamp}.html"

    # Walk library and extract rows
    exts = set(EXTS) if isinstance(EXTS, (set, list, tuple)) else {".m4b", ".m4a", ".mp4"}
    files = walk_library(Path(ROOT_DIR), exts)

    # Filter out "Copy of" files (leftovers from Drive reclaim operations)
    files = [f for f in files if not f.name.startswith("Copy of ")]

    # Two dedupe passes, in app/core/file_dedupe.py — see that module for why the
    # tie-break prefers the longest parent path, and why the numbered pass runs
    # first. Extracted 2026-08-12 to bring this function under flake8's C901
    # ceiling, which had been failing Lint and Deploy on every push.
    deduped_files, dedupe = dedupe_library(files)
    if dedupe.numbered:
        print(f"[INFO] Removed {dedupe.numbered} numbered duplicates (e.g., 'Title (1).m4b')")
    if dedupe.duplicates:
        print(f"[INFO] Deduplicated {dedupe.duplicates} duplicate files (same book in multiple folders)")

    rows = []
    for p in deduped_files:
        try:
            rows.append(extract_metadata(p))
        except Exception as e:
            print(f"[WARN] Failed reading {p}: {e}", file=sys.stderr)

    if not rows:
        print("No audiobook files found.")
        return

    # 0a) The shared universe list — catalog-platform/data/universes.json, read
    #     by this pipeline and by library_catalog. This REPORTS ONLY: it writes
    #     nothing to the CSV or the site, because surfacing universes on screen
    #     is a separate job and a new column would change every generated page.
    #     It runs on every build so the dependency is exercised — a list nobody
    #     reads is a list that breaks quietly. A missing or malformed list warns
    #     and the build continues. See app/core/universes.py.
    from app.core.universes import report_coverage

    try:
        report_coverage(rows)
    except Exception as e:  # noqa: BLE001 — reference data must never stop a build
        print(f"[WARN] Universe coverage report failed: {e}", file=sys.stderr)

    # 0) Record first-seen dates for any new books (drives "Recently Added"
    #    and the upload-history view; immune to file moves/re-syncs)
    from app.additions_log import update_additions_log
    additions = update_additions_log(rows, SITE_DIR)

    # 1) Write CSV (timestamped) into output_files/
    write_csv(rows, out_csv)

    # 2) Write HTML (timestamped) into output_files/
    #    The download link here points to the timestamped CSV file name,
    #    so opening this HTML locally still downloads the matching CSV.
    render_output_html(
        rows=rows,
        out_path=out_html,
        generated_at=generated_at,
        csv_link=out_csv.name,  # relative to this HTML in output_files/
        drive_link=DRIVE_FOLDER_URL or None,
        additions=additions,
    )

    # 3) Stage the public site:
    #    - Copy covers/ and static/ into site/
    #    - Copy CSV into site/catalog.csv
    #    - Render site/index.html with csv_link="catalog.csv" (relative link)
    stage_site_files(
        out_html=out_html,
        out_csv=out_csv,
        site_dir=SITE_DIR,
        site_index_name=SITE_INDEX_NAME,
        site_csv_name=SITE_CSV_NAME,
        rows=rows,
        generated_at=generated_at,
        drive_link=DRIVE_FOLDER_URL or None,
        additions=additions,
    )

    # 4) Generate statistics page
    from app.tools.generate_stats import main as generate_stats_main
    try:
        generate_stats_main()
    except Exception as e:
        print(f"[WARN] Failed to generate statistics page: {e}", file=sys.stderr)

    # 5) Push the projection to the shared cross-catalog index
    #    (catalog-platform index-worker design §5 / §7 step 4). Soft on
    #    purpose: unset INDEX_URL / INDEX_PUSH_TOKEN logs one line inside, and
    #    a failed push warns without failing the build — the index replaces
    #    snapshots wholesale, so a missed push costs freshness only.
    from app.index_push import push_after_build
    try:
        push_after_build(rows)
    except Exception as e:  # noqa: BLE001 — the index must never stall this pipeline
        print(f"[WARN] Index push failed (site build unaffected): {e}", file=sys.stderr)

    print("Done.")


if __name__ == "__main__":
    main()
