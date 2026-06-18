"""Check that every book in the library has a working Drive folder link."""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import ROOT_DIR, EXTS
from app.metadata import walk_library, extract_metadata

# Load Drive map
map_path = PROJECT_ROOT / "author_drive_map.json"
with open(map_path, "r", encoding="utf-8") as f:
    drive_map = json.load(f)

# Load aliases
alias_path = PROJECT_ROOT / "scripts" / "author_aliases.json"
aliases = {}
if alias_path.exists():
    with open(alias_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
        aliases = {k.lower(): v for k, v in raw.items() if not k.startswith("_")}

# Get all books
files = walk_library(ROOT_DIR, EXTS)
print(f"Total audiobook files: {len(files)}")

# Check each book
authors_seen = set()
missing_authors = {}
matched = 0
total = 0

for fp in files:
    try:
        meta = extract_metadata(fp)
        author = meta.get("author", "").strip()
        if not author:
            continue
        total += 1
        authors_seen.add(author)

        # Check if author has a drive link (case-insensitive check)
        found = False
        
        # Extract primary author (first name before comma)
        primary_author = author.split(",")[0].strip()
        
        # Apply alias resolution
        alias_key = primary_author.lower()
        if alias_key in aliases:
            resolved = aliases[alias_key]
            if not resolved.startswith("__FOLDER_ID__"):
                primary_author = resolved
        
        for folder_name in drive_map:
            # Exact match (case-insensitive) on full author or primary
            if author.lower() == folder_name.lower() or primary_author.lower() == folder_name.lower():
                found = True
                break
            # Author/primary is part of folder name
            if primary_author.lower() in folder_name.lower():
                found = True
                break
            # Folder name part matches (split on / and -)
            parts = folder_name.replace("/", " - ").split(" - ")
            for part in parts:
                if part.strip().lower() == primary_author.lower():
                    found = True
                    break
            if found:
                break

        if found:
            matched += 1
        else:
            if author not in missing_authors:
                missing_authors[author] = []
            missing_authors[author].append(fp.name)
    except Exception as e:
        pass

print(f"Books with author metadata: {total}")
print(f"Unique authors: {len(authors_seen)}")
print(f"Books with Drive links: {matched}/{total} ({100*matched//total}%)")
print(f"Authors MISSING Drive links: {len(missing_authors)}")

if missing_authors:
    print(f"\n{'='*60}")
    print("MISSING AUTHORS (no Drive folder found):")
    print(f"{'='*60}")
    for author in sorted(missing_authors.keys()):
        books = missing_authors[author]
        print(f"\n  {author} ({len(books)} book{'s' if len(books) > 1 else ''}):")
        for b in books[:3]:
            print(f"    - {b}")
        if len(books) > 3:
            print(f"    ... and {len(books)-3} more")

print(f"\n{'='*60}")
if not missing_authors:
    print("ALL BOOKS HAVE DRIVE LINKS ✓")
else:
    print(f"ACTION: Create Drive folders for {len(missing_authors)} authors, then run:")
    print("  python scripts/update_drive_map.py")
print(f"{'='*60}")
