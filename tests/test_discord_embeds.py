#!/usr/bin/env python3
"""Test script to verify Discord embed creation without sending."""
import json
from pathlib import Path
import sys

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.tools.send_discord_notification import create_embed

# Load new books data
new_books_file = Path(__file__).parent.parent / "new_books.json"
if new_books_file.exists():
    with open(new_books_file, "r", encoding="utf-8") as f:
        new_books_data = json.load(f)
else:
    new_books_data = {"new_count": 0, "total_count": 0, "books": []}

# Create embeds
site_url = "https://skymitch9.github.io/audiobook_catalog/"
embeds = create_embed(new_books_data, site_url)

# Print results
print(f"✓ Created {len(embeds)} embeds")
print(f"✓ New books: {new_books_data.get('new_count', 0)}")
print(f"✓ Total books: {new_books_data.get('total_count', 0)}")
print(f"\nEmbed structure validation:")
print(f"  - Total embeds: {len(embeds)} (Discord limit: 10)")
print(f"  - Main embed: 1")
print(f"  - Book embeds: {len(embeds) - 1}")

# Validate embed structure
for i, embed in enumerate(embeds):
    title_len = len(embed.get("title", ""))
    desc_len = len(embed.get("description", ""))
    fields_count = len(embed.get("fields", []))
    
    print(f"\nEmbed {i + 1}:")
    print(f"  - Title length: {title_len} (limit: 256)")
    print(f"  - Description length: {desc_len} (limit: 4096)")
    print(f"  - Fields count: {fields_count} (limit: 25)")
    
    if title_len > 256:
        print(f"  ⚠️  WARNING: Title exceeds limit!")
    if desc_len > 4096:
        print(f"  ⚠️  WARNING: Description exceeds limit!")
    if fields_count > 25:
        print(f"  ⚠️  WARNING: Too many fields!")

print("\n✓ Embed validation complete!")
print("\nSample embed JSON (first embed):")
print(json.dumps(embeds[0], indent=2))
