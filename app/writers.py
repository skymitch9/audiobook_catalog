# app/writers.py
from __future__ import annotations

import csv
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from app.config import OUTPUT_DIR
from app.web.html_builder import TEMPLATE_DIR  # not strictly needed, but useful when debugging
from app.web.html_builder import STATIC_DIR, render_index_html


# --------------------------
# CSV
# --------------------------
def write_csv(rows: List[Dict[str, str]], out_path: Path) -> None:
    """
    Writes the catalog CSV. Does NOT include covers (as requested).
    """
    fieldnames = [
        "title",
        "series",
        "series_index_display",
        "series_index_sort",
        "author",
        "narrator",
        "year",
        "genre",
        "duration_hhmm",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})
    print(f"Wrote CSV: {out_path}")


# --------------------------
# HTML (timestamped in output_files)
# --------------------------
def render_output_html(
    rows: List[Dict[str, str]],
    out_path: Path,
    generated_at: str,
    csv_link: str,
    drive_link: Optional[str],
) -> None:
    """
    Renders the timestamped HTML into output_files/ using the inline-CSS/JS template.
    csv_link should be a name relative to the HTML file (usually the timestamped CSV filename).
    """
    render_index_html(
        rows=rows,
        out_path=out_path,
        generated_at=generated_at,
        csv_link=csv_link,
        drive_link=drive_link,
    )
    print(f"Wrote HTML: {out_path}")


# --------------------------
# Site staging
# --------------------------
def _copy_covers_to_site(site_dir: Path) -> None:
    """
    Copy OUTPUT_DIR/covers into site/covers (dirs_exist_ok).
    If no covers yet, silently skip.
    """
    covers_src = OUTPUT_DIR / "covers"
    if covers_src.exists():
        covers_dst = site_dir / "covers"
        shutil.copytree(covers_src, covers_dst, dirs_exist_ok=True)


def _copy_static_to_site(site_dir: Path) -> None:
    """
    Copy app/web/static into site/static if it exists.
    Inline JS template works without it, but if you keep extra assets,
    this ensures they’re available.
    """
    static_dst = site_dir / "static"
    if STATIC_DIR.exists():
        shutil.copytree(STATIC_DIR, static_dst, dirs_exist_ok=True)


def stage_site_files(
    out_html: Path,
    out_csv: Path,
    site_dir: Path,
    site_index_name: str,
    site_csv_name: str,
    rows: List[Dict[str, str]],
    generated_at: str,
    drive_link: Optional[str],
) -> None:
    """
    Prepare the deployable site directory:
      - site/
        - index.html               (rendered fresh with csv_link = site_csv_name)
        - catalog.csv              (copied from the timestamped CSV)
        - covers/                  (copied from OUTPUT_DIR/covers/)
        - static/                  (copied from app/web/static/ if present)
    """
    site_dir.mkdir(parents=True, exist_ok=True)

    # 1) Copy CSV into site/ as the canonical name (e.g., catalog.csv)
    csv_dst = site_dir / site_csv_name
    shutil.copy2(out_csv, csv_dst)

    # 2) Copy covers/ and static/ assets
    _copy_covers_to_site(site_dir)
    _copy_static_to_site(site_dir)

    # 3) Render site/index.html with csv_link pointing at the site CSV file name
    site_index_path = site_dir / site_index_name
    render_index_html(
        rows=rows,
        out_path=site_index_path,
        generated_at=generated_at,
        csv_link=site_csv_name,  # RELATIVE link for GitHub Pages
        drive_link=drive_link,
    )

    print(f"Staged site: {site_index_path} (CSV → {csv_dst})")
