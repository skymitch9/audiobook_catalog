# app/main.py
from __future__ import annotations

import sys
import io

# Force UTF-8 on stdout/stderr so emoji and non-Latin characters in print()
# don't crash with UnicodeEncodeError on Windows consoles using cp1252.
if sys.stdout.encoding and sys.stdout.encoding.lower().replace('-', '') != 'utf8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

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
from app.core.catalog_twins import apply_catalog_twins
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

    # The CURATED twin table — two audio editions of one work, where a person
    # has written down which edition keeps the catalogue identity. Runs AFTER
    # the two filename passes above and never overlaps them: those fold files
    # that share a name, this folds files that share a BOOK. See
    # app/core/catalog_twins.py for why the join is written down instead of
    # computed, and scripts/catalog_twins.json for the table itself.
    #
    # 🔴 NO FILE IS TOUCHED. The retired edition keeps its bytes, its Drive
    # copy, its R2 archive key and its upload_manifest entry; it stops
    # producing a ROW. Owner, 2026-09-02: "Keep the audible one but make sure
    # all source files stay."
    #
    # ⚠️ Every refusal is printed. An entry that refuses leaves the duplicate
    # on the site, so a silent refusal is a defect that hides itself.
    deduped_files, twins = apply_catalog_twins(deduped_files, Path(ROOT_DIR))
    if twins.applied:
        print(f"[INFO] Retired {twins.applied} duplicate catalog row(s) via the twin table "
              f"(files untouched): {', '.join(twins.dropped)}")
    for label, why in twins.refused:
        print(f"[WARN] catalog twin refused — {label}: {why}", file=sys.stderr)

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

    # 0b) Actually stamp `universe` and `series_gap` onto every row —
    #     report_coverage above is print-only by its own contract (see its
    #     docstring), so surfacing the results on the CSV/site is this
    #     separate pass. Split into app/core/reference_stamps.py, mirroring
    #     app/library_link.py's stamp_after_build/_safe split, so that
    #     module's own try/except is the only branching this call site adds
    #     (keeps main() under the repo's flake8 complexity ceiling). Does NOT
    #     consult library_catalog or app/library_link.py's cross-catalog
    #     matching — series gaps are this catalog's own holdings only.
    from app.core.reference_stamps import stamp_reference_data_safe

    stamp_reference_data_safe(rows)

    # 0) Record first-seen dates for any new books (drives "Recently Added"
    #    and the upload-history view; immune to file moves/re-syncs)
    from app.additions_log import update_additions_log
    additions = update_additions_log(rows, SITE_DIR)

    # 0c) Stamp "Other versions available" — library_catalog's work id +
    #     formats for every row it has already matched to an audiobook.
    #     Mirrors app/index_push.py's failure posture: unconfigured ->
    #     one log line, a fetch failure warns without failing the build (the
    #     try/except lives in stamp_after_build_safe, not here, to keep this
    #     function's branching count off this call site).
    #     Runs BEFORE write_csv/stage_site_files so the stamp lands in both.
    from app.library_link import stamp_after_build_safe
    stamp_after_build_safe(rows)

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

    # 5) NO INDEX PUSH HERE — deliberately, since 2026-08-17 (owner decision
    #    "option A"). The shared-index push used to hang off the end of this
    #    build; it is now sync STEP 7 in scripts/sync_to_drive.py, which runs
    #    it ONCE per cycle after the ebook manifest (step 1b), the covers
    #    (5.7), the gated manifest publish (5.8) and the commit (6).
    #    Two reasons it moved OUT of the builder:
    #      * building the site and publishing to another service are different
    #        jobs — the manual `catalog` step is documented as "rebuilds, does
    #        NOT ship", and while the push lived here it silently shipped;
    #      * a push from here runs BEFORE 5.7 uploads a new book's cover to
    #        R2, so estate search could carry a cover_url that 404s for the
    #        minutes in between.
    #    `python -m app.main` on its own therefore does not refresh estate
    #    search: run `python -m app.index_push` after it, or prefer
    #    `sync_to_drive.py --rebuild-only`, which does both. See
    #    docs/access/PIPELINE.md.

    # 6) Club Discord announcements (backlog #2): schedule changes, due-date
    #    nudges, read started/finished — posted to each club's OWN webhook
    #    (clubs/{id}/settings/discord, service-account read). Soft on purpose:
    #    no credentials logs one line inside, and any failure warns without
    #    failing the build. Manual check: python -m app.club_announcements --dry-run
    from app.club_announcements import announce_after_build
    try:
        announce_after_build()
    except Exception as e:  # noqa: BLE001 — announcements must never stall this pipeline
        print(f"[WARN] Club announcements failed (site build unaffected): {e}", file=sys.stderr)

    print("Done.")


if __name__ == "__main__":
    main()
