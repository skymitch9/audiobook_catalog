"""
Import reviews from a Pagebound export (markdown) into the site's reviews.

Cross-references every reviewed title against site/catalog.csv (normalized
prefix match) and, for books we actually have, writes a review doc under the
given display name — keyed exactly how the site keys reviews
({bookIdFromTitle(catalog title)}_{name lower}), with the original review
date preserved. Add-only: existing reviews are never overwritten. Entries
without an "Overall" rating (pure DNFs) are skipped.

Expected markdown shape (see docs/IridescentSea_pagebound_reviews.md):
    ## Title (Series, #1) — Author
    **Jun 25, 2026 · Overall 3.5**
    Enjoyment 4.0 · Quality 3.5 · ...      <- dropped
    <emoji line>                           <- dropped
    review body, quotes, would-I-read-again <- kept verbatim

Usage:
    python -m app.tools.import_pagebound_reviews docs/export.md --user "Sparkling Ember" --dry-run
    python -m app.tools.import_pagebound_reviews docs/export.md --user "Sparkling Ember"
"""

import argparse
import csv
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from app.config import SITE_DIR
from app.tools.club_books import API_KEY as FS_KEY

BASE = "https://firestore.googleapis.com/v1/projects/audiobook-catalog/databases/(default)/documents"
CATALOG_PATH = SITE_DIR / "catalog.csv"


def norm(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def book_id(t):
    """Mirror of reviews.js bookIdFromTitle."""
    return re.sub(r"^-|-$", "", re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", t.lower())))


def catalog_titles():
    out = {}
    with open(CATALOG_PATH, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            t = (row.get("title") or "").strip()
            if t:
                out[norm(t)] = t
    return out


def match_catalog(title, cat):
    n = norm(title)
    variants = [n, "the " + n]
    if n.startswith("the "):
        variants.append(n[4:])
    for v in variants:
        for cn, orig in cat.items():
            if cn == v or cn.startswith(v + " "):
                return orig
    return None


def parse_entries(md_text):
    """[(title, rating, datetime, review_text)] for rated entries."""
    entries = []
    for block in md_text.split("\n---\n"):
        m = re.search(r"^## (.+)$", block, re.M)
        if not m:
            continue
        title = re.split(r" \(| — ", m.group(1))[0].strip()
        rm = re.search(r"Overall (\d(?:\.\d)?)", block)
        if not rm:
            continue  # DNF without a rating
        dm = re.search(r"\*\*(\w+ \d+, \d{4})", block)
        when = (datetime.strptime(dm.group(1), "%b %d, %Y").replace(tzinfo=timezone.utc)
                if dm else datetime.now(timezone.utc))
        lines = block.split("\n")
        start = next(i for i, l in enumerate(lines) if l.startswith("**")) + 1
        body = []
        for line in lines[start:]:
            s = line.strip()
            if s.startswith("Enjoyment "):
                continue
            if s == "*(Rating only, no written review.)*":
                continue
            if s and not re.search(r"[a-zA-Z0-9]", s):
                continue  # emoji-only line
            body.append(line.rstrip())
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(body)).strip()
        entries.append((title, float(rm.group(1)), when, text))
    return entries


def review_exists(doc_id):
    try:
        urllib.request.urlopen(f"{BASE}/reviews/{doc_id}?key={FS_KEY}", timeout=15)
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def write_review(doc_id, bid, user, rating, text, when):
    ts = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    fields = {
        "bookId": {"stringValue": bid},
        "displayName": {"stringValue": user},
        "rating": {"doubleValue": rating},
        "text": {"stringValue": text},
        "createdAt": {"timestampValue": ts},
        "updatedAt": {"timestampValue": ts},
    }
    req = urllib.request.Request(
        f"{BASE}/reviews/{doc_id}?key={FS_KEY}",
        data=json.dumps({"fields": fields}).encode(),
        headers={"Content-Type": "application/json"}, method="PATCH")
    with urllib.request.urlopen(req, timeout=20):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("file", help="pagebound export markdown file")
    parser.add_argument("--user", required=True, help="display name to attribute reviews to")
    parser.add_argument("--dry-run", action="store_true", help="report matches, write nothing")
    args = parser.parse_args()

    cat = catalog_titles()
    md = open(args.file, encoding="utf-8").read()
    written = skipped = unmatched = 0
    for title, rating, when, text in parse_entries(md):
        hit = match_catalog(title, cat)
        if not hit:
            unmatched += 1
            continue
        bid = book_id(hit)
        doc_id = urllib.parse.quote(f"{bid}_{args.user.lower()}", safe="()")
        if review_exists(doc_id):
            print(f"SKIP (exists): {hit}")
            skipped += 1
            continue
        if args.dry_run:
            print(f"WOULD WRITE {rating}★  {hit}  ({len(text)} chars)")
        else:
            write_review(doc_id, bid, args.user, rating, text, when)
            print(f"WROTE {rating}★  {hit}  ({len(text)} chars)")
        written += 1
    mode = "dry-run" if args.dry_run else "written"
    print(f"\n{mode}: {written}, already existed: {skipped}, not in library: {unmatched}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
