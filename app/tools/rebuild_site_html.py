"""
Re-render site/index.html from the committed site/catalog.csv, without
walking the audio library.

WHY
---
`python -m app.main` is the real build, but it needs ROOT_DIR mounted and it
re-reads metadata from ~1,000 m4b files, which means a template-only change
(new script, new CSS, a different cover base URL) either waits for a full
pipeline run or risks the catalog changing underneath an unrelated commit.

catalog.csv carries every field the page renders, so re-rendering from it
produces exactly what a fresh build's HTML step would — same rows, same
order, same additions log — with none of the I/O and none of the catalog
churn.

    python -m app.tools.rebuild_site_html                 # keep the existing
                                                          # "Generated at"
    python -m app.tools.rebuild_site_html --touch-timestamp

⚠️ It does NOT rebuild catalog.csv, stats.html, chapters.json or the covers.
It is the HTML step alone. If the catalog itself is stale, run app.main.

⚠️ One cosmetic difference from a real build: "Recently Added" breaks ties
within a day using `file_mtime`, which app.main puts on its row dicts and
catalog.csv does not carry. Same-day books can therefore come out in a
different order here. The next pipeline build restores it.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from app.config import DRIVE_FOLDER_URL, SITE_CSV_NAME, SITE_DIR, SITE_INDEX_NAME
from app.web.html_builder import render_index_html

GENERATED_AT_RE = re.compile(r"Generated at:\s*([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:]{8})")
DRIVE_LINK_RE = re.compile(r'href="([^"]+)"[^>]*>Google Drive folder<')


def _existing_generated_at(index_path: Path) -> str | None:
    if not index_path.exists():
        return None
    m = GENERATED_AT_RE.search(index_path.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def _existing_drive_link(index_path: Path) -> str | None:
    """The Drive link already on the page.

    DRIVE_FOLDER_URL comes from the environment, so on a machine without a
    .env this would silently drop the footer link. Reuse what shipped.
    """
    if not index_path.exists():
        return None
    m = DRIVE_LINK_RE.search(index_path.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def _load_additions(site_dir: Path) -> dict:
    path = site_dir / "additions_log.json"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            entries = json.load(f).get("entries", [])
    except (json.JSONDecodeError, OSError):
        return {}
    return {e["key"]: e for e in entries if e.get("key")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site-dir", default=SITE_DIR, type=Path)
    ap.add_argument("--touch-timestamp", action="store_true",
                    help='set "Generated at" to now instead of preserving it')
    args = ap.parse_args(argv)

    site_dir: Path = args.site_dir
    index_path = site_dir / SITE_INDEX_NAME
    csv_path = site_dir / SITE_CSV_NAME
    if not csv_path.exists():
        print(f"::error::{csv_path} not found — nothing to render from")
        return 1

    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print(f"::error::{csv_path} has no rows")
        return 1

    generated_at = (
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if args.touch_timestamp
        else (_existing_generated_at(index_path) or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )
    drive_link = DRIVE_FOLDER_URL or _existing_drive_link(index_path)

    render_index_html(
        rows=rows,
        out_path=index_path,
        generated_at=generated_at,
        csv_link=SITE_CSV_NAME,
        drive_link=drive_link,
        additions=_load_additions(site_dir),
    )
    size_mb = index_path.stat().st_size / 1e6
    print(f"Rendered {index_path} — {len(rows)} books, {size_mb:.1f} MB, "
          f"generated at {generated_at}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
