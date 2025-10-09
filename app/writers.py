# app/writers.py
# Slim orchestrator: CSV writing + delegate HTML building/staging to web/html_builder.py

import csv
import shutil
from pathlib import Path
from typing import List, Dict, Optional

from app.config import OUTPUT_DIR

def write_csv(rows: List[Dict[str, str]], out_path: Path) -> None:
    fieldnames = [
        "title","series","series_index_display","series_index_sort",
        "author","narrator","year","genre","duration_hhmm",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})

def write_html(
    rows: List[Dict[str, str]],
    out_path: Path,
    generated_at: str,
    csv_link: str = "catalog.csv",
    drive_link: Optional[str] = None,
) -> None:
    # Delegate to the builder module
    from app.web.html_builder import render_index_html
    render_index_html(rows, out_path, generated_at, csv_link, drive_link)

def stage_site_files(
    timestamped_html: Path,
    timestamped_csv: Path,
    site_dir: Path,
    site_index_name: str,
    site_csv_name: str,
) -> None:
    """
    Copy timestamped outputs to stable filenames inside 'site/', and sync static assets + covers.
    """
    site_dir.mkdir(parents=True, exist_ok=True)

    # index + csv
    (site_dir / site_index_name).write_bytes(timestamped_html.read_bytes())
    (site_dir / site_csv_name).write_bytes(timestamped_csv.read_bytes())

    # static assets (JS/CSS)
    from app.web.html_builder import STATIC_DIR
    static_dst = site_dir / "static"
    shutil.copytree(STATIC_DIR, static_dst, dirs_exist_ok=True)

    # covers
    covers_src = OUTPUT_DIR / "covers"
    covers_dst = site_dir / "covers"
    if covers_src.exists():
        shutil.copytree(covers_src, covers_dst, dirs_exist_ok=True)
