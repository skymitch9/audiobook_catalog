#!/usr/bin/env python3
"""
Rename ASIN-named .epub files to their actual book title + author.

Format: "Title - Author.epub"
Sanitizes filenames for Windows (no :?"<>| etc.)

Usage:
    python scripts/rename_epubs.py              # dry run (preview)
    python scripts/rename_epubs.py --execute    # actually rename
"""

import argparse
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


def get_epub_metadata(epub_path: Path) -> dict | None:
    """Extract title and author from an epub's OPF metadata."""
    try:
        with zipfile.ZipFile(epub_path) as z:
            container = ET.parse(z.open("META-INF/container.xml"))
            ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
            opf_path = container.find(".//c:rootfile", ns).get("full-path")

            opf = ET.parse(z.open(opf_path))
            metadata = opf.find(".//{http://www.idpf.org/2007/opf}metadata")
            if metadata is None:
                return None

            dc = "http://purl.org/dc/elements/1.1/"
            title_el = metadata.find(f"{{{dc}}}title")
            creator_el = metadata.find(f"{{{dc}}}creator")

            title = title_el.text.strip() if title_el is not None and title_el.text else None
            author = creator_el.text.strip() if creator_el is not None and creator_el.text else None

            # Author might be "Last, First" — flip it
            if author and ", " in author and author.count(",") == 1:
                parts = author.split(", ")
                author = f"{parts[1]} {parts[0]}"

            return {"title": title, "author": author}
    except Exception as e:
        print(f"  [ERROR] Can't read {epub_path.name}: {e}")
        return None


def sanitize_filename(name: str) -> str:
    """Remove characters illegal in Windows filenames."""
    # Replace illegal chars with hyphen
    name = re.sub(r'[<>:"/\\|?*]', "-", name)
    # Collapse multiple hyphens/spaces
    name = re.sub(r"-{2,}", "-", name)
    name = re.sub(r"\s{2,}", " ", name)
    # Strip leading/trailing dots and spaces
    name = name.strip(". ")
    # Truncate to reasonable length (255 minus .epub)
    if len(name) > 240:
        name = name[:240]
    return name


def main():
    parser = argparse.ArgumentParser(description="Rename ASIN-named epubs to title - author.epub")
    parser.add_argument("--execute", action="store_true", help="Actually rename (default is dry run)")
    parser.add_argument("--dir", default=r"C:\Users\nbasl\OpenAudible\books", help="Directory to scan")
    args = parser.parse_args()

    source = Path(args.dir)
    if not source.exists():
        print(f"[ERROR] Directory not found: {source}")
        return

    epubs = sorted(source.glob("*.epub"))
    if not epubs:
        print("No .epub files found in the root of the directory.")
        return

    print(f"Found {len(epubs)} epub(s) in {source}")
    print(f"Mode: {'EXECUTE' if args.execute else 'DRY RUN'}\n")

    renamed = 0
    skipped = 0
    failed = 0

    for epub in epubs:
        meta = get_epub_metadata(epub)
        if not meta or not meta["title"]:
            print(f"  [SKIP] No title metadata: {epub.name}")
            skipped += 1
            continue

        title = meta["title"]
        author = meta.get("author", "")

        if author:
            new_name = f"{title} - {author}.epub"
        else:
            new_name = f"{title}.epub"

        new_name = sanitize_filename(new_name)
        new_path = epub.parent / new_name

        if new_path == epub:
            skipped += 1
            continue

        if new_path.exists():
            print(f"  [EXISTS] {new_name} — skipping {epub.name}")
            skipped += 1
            continue

        print(f"  [RENAME] {epub.name} -> {new_name}")
        if args.execute:
            try:
                epub.rename(new_path)
                renamed += 1
            except Exception as e:
                print(f"    [ERROR] {e}")
                failed += 1
        else:
            renamed += 1

    print(f"\n{'Renamed' if args.execute else 'Would rename'}: {renamed}")
    print(f"Skipped: {skipped}")
    if failed:
        print(f"Failed: {failed}")


if __name__ == "__main__":
    main()
