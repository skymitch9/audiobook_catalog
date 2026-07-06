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
import subprocess
import sys
import tempfile
from pathlib import Path

from app.config import SITE_DIR

CATALOG_PATH = SITE_DIR / "catalog.csv"
PROJECT_ROOT = SITE_DIR.parent


AUDIBLE_PROFILES = ("skylar", "samantha")


def audible_cli_books():
    """FRESH library rows straight from Audible via audible-cli exports for
    every registered profile. Returns [] when no profile works (fall back to
    the container's books.json then). Each row carries the owning profile,
    which the auto-downloader uses directly."""
    books = []
    for prof in AUDIBLE_PROFILES:
        try:
            with tempfile.TemporaryDirectory() as td:
                out = Path(td) / "lib.tsv"
                r = subprocess.run(
                    [sys.executable, "-m", "audible_cli", "-P", prof,
                     "library", "export", "--output", str(out)],
                    capture_output=True, text=True, encoding="utf-8", timeout=600)
                if r.returncode != 0 or not out.exists():
                    print(f"  [audible-cli] export failed for {prof}: {(r.stderr or '')[:150]}")
                    continue
                with open(out, encoding="utf-8", newline="") as f:
                    for row in csv.DictReader(f, delimiter="	"):
                        books.append({
                            "title_short": row.get("title") or "",
                            "title": row.get("title") or "",
                            "author": row.get("authors") or "",
                            "purchase_date": row.get("purchase_date") or "",
                            "release_date": row.get("release_date") or "",
                            "asin": row.get("asin") or "",
                            "profile": prof,
                        })
        except Exception as e:
            print(f"  [audible-cli] export error for {prof}: {e}")
    return books


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

    def key(b):
        return b.get("purchase_date") or b.get("release_date") or ""

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


def run_audit(top=50, books=None):
    """Report missing recent purchases. Prefers FRESH audible-cli exports;
    falls back to the container/desktop books.json. Returns the missing
    list, or None when no source is available."""
    src = "audible-cli exports"
    if books is None:
        books = audible_cli_books()
    if not books:
        path = books_json_path()
        if path is None:
            print("no audible-cli profiles and no books.json — skipping purchase audit")
            return None
        books = json.loads(path.read_text(encoding="utf-8"))
        src = str(path)
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
