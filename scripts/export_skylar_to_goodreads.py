"""
Export Skylar's reviews from Firestore as a Goodreads-compatible CSV.

Usage:
    python scripts/export_skylar_to_goodreads.py [--out skylar_goodreads.csv]

Requires:
    gcloud auth application-default login
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CATALOG_CSV = PROJECT_ROOT / "output_files" / "audiobook_catalog.csv"
GCP_PROJECT = "audiobook-catalog"
USERNAME = "skylar"

GOODREADS_FIELDS = [
    "Book Id", "Title", "Author", "Author l-f", "Additional Authors",
    "ISBN", "ISBN13", "My Rating", "Average Rating", "Publisher", "Binding",
    "Number of Pages", "Year Published", "Original Publication Year",
    "Date Read", "Date Added", "Bookshelves", "Bookshelves with positions",
    "Exclusive Shelf", "My Review", "Spoiler", "Private Notes",
    "Read Count", "Owned Copies",
]


def to_slug(text: str) -> str:
    s = text.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def slug_to_title(slug: str) -> str:
    """Best-effort title reconstruction from a slug when no catalog match exists."""
    # Strip trailing audiobook edition noise like -part-1-of-2, -book-1, -unabridged
    cleaned = re.sub(r"-(part-\d+-of-\d+|book-\d+|unabridged|dramatized-adaptation).*$", "", slug)
    return cleaned.replace("-", " ").title()


def author_last_first(author: str) -> str:
    """Convert 'First Last' to 'Last, First' for the Author l-f column."""
    parts = author.strip().split()
    if len(parts) >= 2:
        return f"{parts[-1]}, {' '.join(parts[:-1])}"
    return author


def format_date(ts) -> str:
    """Convert Firestore timestamp to YYYY/MM/DD."""
    if ts is None:
        return ""
    try:
        return ts.strftime("%Y/%m/%d")
    except Exception:
        return str(ts)[:10].replace("-", "/")


def load_catalog(path: Path) -> dict[str, dict]:
    """Build slug → catalog row. Tries most recent timestamped CSV if main is stale."""
    candidates = [path]
    if not path.exists() or path.stat().st_size < 10_000:
        timestamped = sorted(path.parent.glob("audiobook_catalog_2*.csv"), reverse=True)
        if timestamped:
            candidates = [timestamped[0]] + candidates

    best_path, best_rows = candidates[0], {}
    for p in candidates:
        if not p.exists():
            continue
        rows: dict[str, dict] = {}
        with open(p, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows[to_slug(row["title"])] = row
        if len(rows) > len(best_rows):
            best_rows = rows
            best_path = p

    print(f"Loaded {len(best_rows)} catalog entries from {best_path.name}")
    return best_rows


def match_book(book_id: str, catalog: dict[str, dict]) -> dict | None:
    """Try exact slug match, then prefix match (bookId may have extra edition detail)."""
    if book_id in catalog:
        return catalog[book_id]
    # Try progressively shorter prefixes of the bookId
    parts = book_id.split("-")
    for end in range(len(parts) - 1, 2, -1):
        prefix = "-".join(parts[:end])
        if prefix in catalog:
            return catalog[prefix]
    return None


def build_goodreads_row(
    book_id: str,
    rating: float,
    review_text: str,
    created_at,
    updated_at,
    shelf: str,
    catalog: dict[str, dict],
    row_num: int,
) -> dict:
    cat = match_book(book_id, catalog)

    title = cat["title"] if cat else slug_to_title(book_id)
    author = cat["author"] if cat else ""
    year = cat.get("year", "") if cat else ""
    avg_rating = cat.get("hardcover_rating", "") if cat else ""

    gr_rating = 0
    if rating:
        gr_rating = min(5, max(1, round(float(rating))))

    return {
        "Book Id": row_num,
        "Title": title,
        "Author": author,
        "Author l-f": author_last_first(author) if author else "",
        "Additional Authors": "",
        "ISBN": "",
        "ISBN13": "",
        "My Rating": gr_rating,
        "Average Rating": avg_rating,
        "Publisher": "",
        "Binding": "Audiobook",
        "Number of Pages": "",
        "Year Published": year,
        "Original Publication Year": year,
        "Date Read": format_date(updated_at),
        "Date Added": format_date(created_at),
        "Bookshelves": shelf,
        "Bookshelves with positions": f"{shelf} (#1)" if shelf else "",
        "Exclusive Shelf": shelf,
        "My Review": review_text or "",
        "Spoiler": "",
        "Private Notes": "",
        "Read Count": 1 if shelf == "read" else 0,
        "Owned Copies": 0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="skylar_goodreads.csv")
    args = parser.parse_args()

    try:
        from google.cloud import firestore
    except ImportError:
        print("google-cloud-firestore not installed. Run: pip install -r requirements.txt")
        sys.exit(1)

    db = firestore.Client(project=GCP_PROJECT)
    catalog = load_catalog(CATALOG_CSV)

    # Fetch Skylar's reviews
    print(f"Fetching reviews for '{USERNAME}'...")
    reviews = [
        doc.to_dict()
        for doc in db.collection("reviews").stream()
        if (doc.to_dict().get("displayName") or "").lower() == USERNAME
    ]
    print(f"Found {len(reviews)} reviews")

    # Fetch Skylar's profile for currently-reading
    profile = db.collection("profiles").document(USERNAME).get().to_dict() or {}
    currently_reading = profile.get("currentlyReading", "")

    rows = []
    unmatched = []

    for i, rev in enumerate(reviews, start=1):
        book_id = rev.get("bookId", "")
        cat = match_book(book_id, catalog)
        if not cat:
            unmatched.append(book_id)
        row = build_goodreads_row(
            book_id=book_id,
            rating=rev.get("rating", 0),
            review_text=rev.get("text", ""),
            created_at=rev.get("createdAt"),
            updated_at=rev.get("updatedAt"),
            shelf="read",
            catalog=catalog,
            row_num=i,
        )
        rows.append(row)

    # Add currently-reading entry
    if currently_reading:
        cr_slug = to_slug(currently_reading)
        cat = catalog.get(cr_slug)
        cr_row = build_goodreads_row(
            book_id=cr_slug,
            rating=0,
            review_text="",
            created_at=None,
            updated_at=None,
            shelf="currently-reading",
            catalog=catalog,
            row_num=len(rows) + 1,
        )
        if not cat:
            cr_row["Title"] = currently_reading
        rows.append(cr_row)
        print(f"Added currently-reading: {currently_reading}")

    out_path = Path(args.out)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=GOODREADS_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    matched = len(reviews) - len(unmatched)
    print(f"\nExported {len(rows)} rows to {out_path}")
    print(f"Catalog matched: {matched}/{len(reviews)} reviews")
    if unmatched:
        print(f"Unmatched ({len(unmatched)}) — titles derived from slug:")
        for b in unmatched[:10]:
            print(f"  {b}")
        if len(unmatched) > 10:
            print(f"  ... and {len(unmatched) - 10} more")


if __name__ == "__main__":
    main()
