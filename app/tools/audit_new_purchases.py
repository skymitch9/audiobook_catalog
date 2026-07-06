"""
Are any recent Audible purchases missing from the local library?

Book sorting into author folders breaks OpenAudible's own file tracking, so
its to-download list is meaningless (it marks nearly everything). This tool
is the replacement: take the newest N purchases from OpenAudible's
books.json, diff them against site/catalog.csv by normalized title, and
report anything we don't actually have. Those (and anything newer) are the
only books worth downloading — everything else would be a dupe.

books.json is found in the first of: $OPENAUDIBLE_DATA_DIR, the Docker
scratch dir (runtime/openaudible), the desktop install
(C:/Users/<user>/OpenAudible).

Usage:
    python -m app.tools.audit_new_purchases            # top 50, report
    python -m app.tools.audit_new_purchases --top 100
Exit code: 0 = library current, 1 = purchases missing locally, 2 = no data.
"""

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path

from app.config import SITE_DIR

CATALOG_PATH = SITE_DIR / "catalog.csv"
PROJECT_ROOT = SITE_DIR.parent


def books_json_path():
    candidates = []
    env = os.getenv("OPENAUDIBLE_DATA_DIR")
    if env:
        candidates.append(Path(env) / "books.json")
    candidates.append(PROJECT_ROOT / "runtime" / "openaudible" / "books.json")
    candidates.append(Path.home() / "OpenAudible" / "books.json")
    for p in candidates:
        if p.exists():
            return p
    return None


def norm(title):
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def newest_purchases(books, top):
    real = [b for b in books if (b.get("author") or "") != "OpenAudible"]
    key = lambda b: (b.get("purchase_date") or b.get("release_date") or "")
    return sorted(real, key=key, reverse=True)[:top]


def missing_purchases(books, catalog_titles, top=50):
    """[(purchase_date, title)] of recent purchases with no local match."""
    out = []
    for b in newest_purchases(books, top):
        title = b.get("title_short") or b.get("title") or ""
        n = norm(title)
        have = any(n in c or c in n for c in catalog_titles) if n else False
        if not have:
            date = (b.get("purchase_date") or b.get("release_date") or "")[:10]
            out.append((date, title))
    return out


def load_catalog_titles():
    titles = set()
    with open(CATALOG_PATH, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            n = norm(row.get("title"))
            if n:
                titles.add(n)
    return titles


def run_audit(top=50):
    """Report missing recent purchases. Returns the missing list, or None
    when no books.json is available (desktop app closed and no container)."""
    src = books_json_path()
    if src is None:
        print("no OpenAudible books.json found — skipping purchase audit")
        return None
    books = json.loads(src.read_text(encoding="utf-8"))
    missing = missing_purchases(books, load_catalog_titles(), top)
    print(f"newest {top} purchases in {src} vs catalog: {len(missing)} missing")
    for date, title in missing:
        print(f"  [MISSING] {date} | {title}")
    if not missing:
        print("  library is current — nothing to download")
    return missing


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--top", type=int, default=50, help="how many recent purchases to check")
    args = parser.parse_args()
    missing = run_audit(args.top)
    if missing is None:
        return 2
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
