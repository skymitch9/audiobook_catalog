# app/main.py
from __future__ import annotations

import sys
from pathlib import Path
from datetime import datetime

from app.config import (
    ROOT_DIR,
    OUTPUT_DIR,
    SITE_DIR,
    SITE_INDEX_NAME,
    SITE_CSV_NAME,
    DRIVE_FOLDER_URL,
    EXTS,
)
from app.metadata import extract_metadata, walk_library
from app.writers import write_csv, render_output_html, stage_site_files


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
    rows = []
    for p in files:
        try:
            rows.append(extract_metadata(p))
        except Exception as e:
            print(f"[WARN] Failed reading {p}: {e}", file=sys.stderr)

    if not rows:
        print("No audiobook files found.")
        return

    # 1) Write CSV (timestamped) into output_files/
    write_csv(rows, out_csv)

    # 2) Write HTML (timestamped) into output_files/
    #    The download link here points to the timestamped CSV file name,
    #    so opening this HTML locally still downloads the matching CSV.
    render_output_html(
        rows=rows,
        out_path=out_html,
        generated_at=generated_at,
        csv_link=out_csv.name,            # relative to this HTML in output_files/
        drive_link=DRIVE_FOLDER_URL or None,
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
    )

    # 4) Generate series completion tracker
    try:
        from app.tools.generate_series_tracker import analyze_series, generate_html
        print("Generating series tracker...")
        catalog_path = SITE_DIR / SITE_CSV_NAME
        tracker_path = SITE_DIR / 'series-tracker.html'
        analysis = analyze_series(catalog_path)
        generate_html(analysis, tracker_path)
        print(f"Series tracker: {tracker_path}")
    except Exception as e:
        print(f"[WARN] Failed to generate series tracker: {e}", file=sys.stderr)

    print("Done.")


if __name__ == "__main__":
    main()
