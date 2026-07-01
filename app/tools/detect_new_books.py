#!/usr/bin/env python3
"""
Detect new books by comparing current catalog against a saved snapshot.
On first run (no snapshot), saves current state as baseline.
On subsequent runs, finds books in catalog that weren't in the snapshot.
After Discord notification fires, the snapshot is updated.

Outputs new_books.json for Discord notification.
"""
import csv
import json
import os
import sys
from pathlib import Path

SNAPSHOT_PATH = Path("last_catalog_snapshot.json")


def load_snapshot() -> set:
    """Load the previous catalog snapshot (set of book IDs)."""
    if not SNAPSHOT_PATH.exists():
        return set()
    try:
        with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data.get("book_ids", []))
    except Exception:
        return set()


def save_snapshot(book_ids: list, total_count: int) -> None:
    """Save current catalog state as the new snapshot."""
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "book_ids": book_ids,
            "total_count": total_count,
        }, f)


def main():
    # Get current catalog
    current_csv = Path("site/catalog.csv")
    if not current_csv.exists():
        print("No current catalog found")
        sys.exit(0)

    with open(current_csv, "r", encoding="utf-8") as f:
        current_books = list(csv.DictReader(f))

    # Build current book IDs
    def book_id(book):
        return f"{book.get('title', '')}|{book.get('author', '')}"

    current_ids = [book_id(b) for b in current_books]
    current_id_set = set(current_ids)

    # Load previous snapshot
    previous_ids = load_snapshot()

    # First run — no snapshot exists, create baseline
    if not previous_ids:
        print(f"First run: saving baseline snapshot ({len(current_books)} books)")
        save_snapshot(list(current_id_set), len(current_books))
        # Still output new_books.json with 0 new (so Discord doesn't fire)
        output = {
            "new_count": 0,
            "total_count": len(current_books),
            "books": [],
        }
        with open("new_books.json", "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2)
        return

    # Find new books (in current but not in snapshot)
    new_books = []
    for book in current_books:
        if book_id(book) not in previous_ids:
            new_books.append(
                {
                    "title": book.get("title", ""),
                    "author": book.get("author", ""),
                    "series": book.get("series", ""),
                    "series_index": book.get("series_index_display", ""),
                    "narrator": book.get("narrator", ""),
                    "cover": book.get("cover_href", ""),
                    "year": book.get("year", ""),
                    "genre": book.get("genre", ""),
                    "duration": book.get("duration_hhmm", ""),
                }
            )

    # Save to file for Discord notification
    output = {
        "new_count": len(new_books),
        "total_count": len(current_books),
        "books": new_books[:10],  # Limit to 10 for Discord
    }

    with open("new_books.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    # Only update snapshot if --update-snapshot flag is passed (done by CI after Discord fires)
    if "--update-snapshot" in sys.argv:
        save_snapshot(list(current_id_set), len(current_books))
        print("  Snapshot updated.")

    print(f"Found {len(new_books)} new books (total: {len(current_books)})")

    # Set GitHub Actions output for conditional Discord notification
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as gh:
            gh.write(f"has_new_books={'true' if new_books else 'false'}\n")
            gh.write(f"new_count={len(new_books)}\n")

    if new_books:
        print("New books:")
        for book in new_books[:5]:
            print(f"  - {book['title']} by {book['author']}")
        if len(new_books) > 5:
            print(f"  ... and {len(new_books) - 5} more")


if __name__ == "__main__":
    main()
