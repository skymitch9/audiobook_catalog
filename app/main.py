# app/main.py

import sys
from pathlib import Path
from datetime import datetime

from app.config import (
    ROOT_DIR,
    EXTS,
    SITE_DIR,
    SITE_INDEX_NAME,
    SITE_CSV_NAME,
    OUTPUT_DIR,
)
from app.metadata import extract_metadata, walk_library
from app.writers import write_csv, write_html, stage_site_files


def main():
    if not ROOT_DIR.exists():
        print(f"[ERROR] ROOT_DIR not found: {ROOT_DIR}")
        sys.exit(1)

    ts = datetime.now()
    stamp = ts.strftime("%Y%m%d_%H%M%S")
    generated_at = ts.strftime("%Y-%m-%d %H:%M:%S")

    # Write BOTH timestamped outputs into output_files/
    out_html = OUTPUT_DIR / f"audiobook_catalog_{stamp}.html"
    out_csv = OUTPUT_DIR / f"audiobook_catalog_{stamp}.csv"

    rows = []
    for p in walk_library(ROOT_DIR, EXTS):
        try:
            rows.append(extract_metadata(p))
        except Exception as e:
            print(f"[WARN] Failed reading {p}: {e}")

    if not rows:
        print("No audiobook files found.")
        return

    # Create outputs
    write_csv(rows, out_csv)
    # The CSV link in the HTML points to the stable name that will live in /site
    write_html(rows, out_html, generated_at, csv_link=SITE_CSV_NAME)

    # Stage the latest copies for GitHub Pages (stable names in /site)
    stage_site_files(out_html, out_csv, SITE_DIR, SITE_INDEX_NAME, SITE_CSV_NAME)

    print(f"Generated (timestamped): {out_html} , {out_csv}")
    print(f"Staged for Pages: {SITE_DIR / SITE_INDEX_NAME} , {SITE_DIR / SITE_CSV_NAME}")
    print("Commit & push the 'site/' folder to update GitHub Pages.")


if __name__ == "__main__":
    main()
