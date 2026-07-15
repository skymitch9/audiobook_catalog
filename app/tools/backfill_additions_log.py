#!/usr/bin/env python3
"""
One-time backfill of site/additions_log.json from historical data.

Two sources, best date wins per book:
  1. Git history of site/catalog.csv — the commit date a title first appeared
     in the catalog (source "git"). Exact for everything added since the repo
     started tracking the CSV.
  2. Audible purchase dates — for books already present in the very first
     commit (where git can't tell us when they arrived), fall back to the
     audible-cli purchase_date (source "purchase"). Anything unmatched gets
     the first-commit date with source "baseline".

Existing log entries are never overwritten, so re-running is safe and the
normal pipeline's "pipeline" entries always win.

Usage:
    python -m app.tools.backfill_additions_log                # full backfill
    python -m app.tools.backfill_additions_log --no-purchases # git history only
"""
from __future__ import annotations

import argparse
import csv
import io
import subprocess
import sys

from app.additions_log import book_key, load_log, save_log
from app.config import PROJECT_ROOT, SITE_DIR

CATALOG_REL = "site/catalog.csv"


def _git(*args: str) -> str:
    r = subprocess.run(
        ["git", *args], capture_output=True, text=True, encoding="utf-8",
        errors="replace", cwd=str(PROJECT_ROOT),
    )
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {(r.stderr or '').strip()[:300]}")
    return r.stdout


def catalog_history():
    """[(commit_date, sha)] for every commit touching catalog.csv, oldest first."""
    out = _git("log", "--reverse", "--format=%H %cs", "--", CATALOG_REL)
    commits = []
    for line in out.splitlines():
        parts = line.strip().split()
        if len(parts) == 2:
            commits.append((parts[1], parts[0]))
    return commits


def books_at(sha: str):
    """{book_key: (title, author)} parsed from catalog.csv at a commit."""
    try:
        raw = _git("show", f"{sha}:{CATALOG_REL}")
    except RuntimeError:
        return {}
    books = {}
    for row in csv.DictReader(io.StringIO(raw)):
        title, author = row.get("title", ""), row.get("author", "")
        key = book_key(title, author)
        if key != "|":
            books[key] = (title, author)
    return books


def purchase_dates_by_title():
    """{normalized_title: YYYY-MM-DD} from audible-cli exports or books.json."""
    from app.tools.audit_new_purchases import audible_cli_books, books_json_path, norm

    books = audible_cli_books()
    if not books:
        path = books_json_path()
        if path is None:
            print("  [purchases] no audible-cli profiles and no books.json — skipping")
            return {}
        import json
        books = json.loads(path.read_text(encoding="utf-8"))
        print(f"  [purchases] using {path}")
    dates = {}
    for b in books:
        if (b.get("author") or "") == "OpenAudible":
            continue
        n = norm(b.get("title_short") or b.get("title") or "")
        date = (b.get("purchase_date") or b.get("release_date") or "")[:10]
        if n and date:
            # Keep the earliest date if a title appears in multiple profiles
            if n not in dates or date < dates[n]:
                dates[n] = date
    print(f"  [purchases] {len(dates)} purchase dates loaded")
    return dates


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill site/additions_log.json")
    parser.add_argument("--no-purchases", action="store_true",
                        help="skip Audible purchase-date lookup for baseline books")
    args = parser.parse_args()

    commits = catalog_history()
    if not commits:
        print(f"No git history for {CATALOG_REL} — nothing to backfill")
        return 2
    first_date = commits[0][0]
    print(f"Walking {len(commits)} commits of {CATALOG_REL} ({first_date} .. {commits[-1][0]})")

    # First-seen commit date per book
    first_seen: dict[str, tuple[str, str, str]] = {}  # key -> (date, title, author)
    baseline_keys: set[str] = set()
    for i, (date, sha) in enumerate(commits):
        for key, (title, author) in books_at(sha).items():
            if key not in first_seen:
                first_seen[key] = (date, title, author)
                if i == 0:
                    baseline_keys.add(key)
    print(f"  {len(first_seen)} distinct books; {len(baseline_keys)} predate tracking (first commit)")

    purchases = {} if args.no_purchases else purchase_dates_by_title()
    if purchases:
        from app.tools.audit_new_purchases import norm

    entries = load_log(SITE_DIR)
    counts = {"git": 0, "purchase": 0, "baseline": 0, "kept": 0}
    for key, (date, title, author) in first_seen.items():
        if key in entries:
            counts["kept"] += 1
            continue
        source = "git"
        if key in baseline_keys:
            source, date = "baseline", first_date
            if purchases:
                pdate = purchases.get(norm(title))
                if not pdate:  # tolerate subtitle differences either direction
                    n = norm(title)
                    pdate = next((d for t, d in purchases.items() if n and (n in t or t in n)), None)
                if pdate:
                    source, date = "purchase", pdate
        entries[key] = {"key": key, "title": title, "author": author,
                        "added": date, "source": source}
        counts[source] += 1

    save_log(SITE_DIR, entries)
    print(f"Wrote {len(entries)} entries -> {SITE_DIR / 'additions_log.json'}")
    print(f"  from git history: {counts['git']}, from purchase dates: {counts['purchase']}, "
          f"baseline: {counts['baseline']}, already logged (kept): {counts['kept']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
