#!/usr/bin/env python3
"""
Detect new books added since last commit.
Compares current catalog.csv with previous version from git.
Outputs new_books.json for Discord notification.
"""
import csv
import json
import subprocess
import sys
from pathlib import Path


def get_previous_catalog():
    """Get catalog.csv from previous commit."""
    try:
        result = subprocess.run(["git", "show", "HEAD~1:site/catalog.csv"], capture_output=True, text=True, check=True)
        return result.stdout
    except subprocess.CalledProcessError:
        # No previous commit or file doesn't exist
        return None


def parse_csv_content(content):
    """Parse CSV content into list of book dicts."""
    if not content:
        return []

    lines = content.strip().split("\n")
    if len(lines) < 2:
        return []

    reader = csv.DictReader(lines)
    return list(reader)


def main():
    # Get current catalog
    current_csv = Path("site/catalog.csv")
    if not current_csv.exists():
        print("No current catalog found")
        sys.exit(0)

    with open(current_csv, "r", encoding="utf-8") as f:
        current_books = list(csv.DictReader(f))

    # Get previous catalog
    previous_content = get_previous_catalog()
    previous_books = parse_csv_content(previous_content)

    # Create sets of book identifiers (title + author)
    def book_id(book):
        return f"{book.get('title', '')}|{book.get('author', '')}"

    previous_ids = {book_id(b) for b in previous_books}

    # Find new books
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

    print(f"Found {len(new_books)} new books")
    if new_books:
        print("New books:")
        for book in new_books[:5]:
            print(f"  - {book['title']} by {book['author']}")
        if len(new_books) > 5:
            print(f"  ... and {len(new_books) - 5} more")


if __name__ == "__main__":
    main()
