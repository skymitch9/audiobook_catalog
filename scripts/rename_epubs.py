#!/usr/bin/env python3
"""
Rename ASIN-named .epub files to their actual book title + author.

Format: "Title - Author.epub"
Sanitizes filenames for Windows (no :?"<>| etc.)

Usage:
    python scripts/rename_epubs.py              # dry run (preview)
    python scripts/rename_epubs.py --execute    # actually rename

⚠️ `get_epub_metadata` IS THE ESTATE'S ONE EPUB METADATA READER, not a helper
local to this script. Five call sites import it: `sync_to_drive.py` STEP 1a,
`build_ebook_manifest.py` (which writes `site/ebooks.json` -> the gated
manifest -> the search index's `creator` field), `fix_epub_titles.py` and
`sort_ebooks.py`. A change to the author it returns is therefore a change to a
PERSISTED value in four places; see `normalize_creator` below before touching
it.

WHY THE AUTHOR NEEDS NORMALISING AT ALL (measured 2026-09-02)
------------------------------------------------------------
Vendor EPUBs put the SORT form in `<dc:creator>`: 22 of the 132 epubs in this
library say `Wight, Will`, `English, Miles`, `XX, Mashton`, `the Mad, Sir
Bedivere` and so on. Nothing in this repo writes those strings — they are the
publisher's own OPF — and Audiobookshelf reads that OPF verbatim when it scans
an ebook-only item, which is why the shelf grew 22 flipped author records
beside the real ones. This function is the one place the pipeline already
touches that name, so it is the place it gets fixed.

⚠️ IT DOES NOT AND CANNOT FIX THE SHELF. ABS reads the bytes inside the epub,
which this script deliberately never rewrites; renaming the FILE does not
change the OPF. Records already minted are merged by
`scripts/merge_abs_authors.py`; new arrivals will keep minting them until
somebody decides that rewriting a publisher's OPF is acceptable. That is an
owner decision, not a side effect of a rename. See docs/KNOWN_ISSUES.md KI-10.
"""

import argparse
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

# Surname particles. A comma-separated name whose LEFT side begins with one of
# these is a person ("the Mad, Sir Bedivere"), not a list of two people.
_SURNAME_PARTICLES = {
    "the", "van", "von", "de", "del", "della", "der", "den", "di", "da", "das",
    "dos", "du", "la", "le", "el", "bin", "ibn", "al", "ter", "ten", "of",
}


def _is_flipped_pair(surname: str, given: str) -> bool:
    """Is `"<surname>, <given>"` ONE person written back to front?

    ⚠️ THIS IS THE REFUSAL, NOT A GUESS. `"Wight, Will"` (one person, flipped)
    and `"Rik Hoskin, Julius Gopez"` (two people, in order) are the same shape,
    and no amount of string inspection separates them in general. So this
    answers yes only on the two forms that are unambiguous in this library, and
    no otherwise — leaving the raw string alone rather than inventing a person.

    ⚠️ THE OLD HEURISTIC DID INVENT ONE. Before 2026-09-02 this was a bare
    `if ", " in author and author.count(",") == 1: flip`, which turned
    `"Rik Hoskin, Julius Gopez"` (the White Sand graphic novel's writer and its
    artist) into `"Julius Gopez Rik Hoskin"` — a human being who does not
    exist, who reached `site/ebooks.json`, the gated manifest, the search
    index's `creator` field and an Audiobookshelf author record. Measured: it
    was wrong on exactly 1 of the 132 epubs here and right on the other 21
    flipped ones, which is precisely why it survived so long.
    """
    surname_tokens = surname.split()
    if len(surname_tokens) == 1:
        # "Wight, Will" / "XX, Mashton" / "Clayton, Meg Waite" — a bare surname
        # on the left is never a complete personal name, so this is a flip.
        return True
    if surname_tokens[0].lower().strip(".") in _SURNAME_PARTICLES:
        # "the Mad, Sir Bedivere" — a particle cannot open a person's full name.
        return True
    # Two or more capitalised tokens on BOTH sides: indistinguishable from a
    # co-author list. Refuse.
    return False


def normalize_creator(raw: str | None) -> tuple[str | None, bool]:
    """OPF `dc:creator` -> a display-order name. Returns (name, ambiguous).

    `ambiguous` is True when the string held a comma this function declined to
    resolve, so a caller can NAME the case instead of skipping it silently.
    """
    if not raw:
        return None, False
    name = re.sub(r"\s+", " ", raw).strip().strip(",").strip()
    if not name:
        return None, False
    if name.count(",") != 1:
        # 0 commas: already display order, nothing to do.
        # 2+ commas: a list ("Hoskin, Rik, Sanderson, Brandon") whose pairing is
        # not recoverable — two flipped people, or three plain ones. Refuse.
        return name, ("," in name)
    surname, given = (p.strip() for p in name.split(","))
    if not surname or not given:
        return name, False
    if _is_flipped_pair(surname, given):
        return f"{given} {surname}", False
    return name, True


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
            raw_author = creator_el.text.strip() if creator_el is not None and creator_el.text else None

            # ⚠️ FIRST `<dc:creator>` ONLY, unchanged on purpose. Four epubs
            # here carry two creator elements; folding them in would rewrite
            # the author of already-published rows in `site/ebooks.json`, the
            # gated manifest and the index. That is a migration with its own
            # decision, not a side effect of fixing the flip.
            author, ambiguous = normalize_creator(raw_author)

            return {
                "title": title,
                "author": author,
                "author_raw": raw_author,
                "author_ambiguous": ambiguous,
            }
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
    ambiguous: list[str] = []

    for epub in epubs:
        meta = get_epub_metadata(epub)
        if not meta or not meta["title"]:
            print(f"  [SKIP] No title metadata: {epub.name}")
            skipped += 1
            continue

        title = meta["title"]
        author = meta.get("author", "")
        if meta.get("author_ambiguous"):
            # ⚠️ NAMED, never a silent pass-through: the raw string held a comma
            # that could be a flipped surname or a second author, and we
            # declined to guess. The name below is what will be used verbatim.
            ambiguous.append(f"{epub.name}: {meta.get('author_raw')!r}")

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
    if ambiguous:
        print(f"\nAuthor left AS WRITTEN — a comma we would not guess at ({len(ambiguous)}):")
        for line in ambiguous:
            print(f"  - {line}")
        print("  (a flipped surname and a two-author list are the same shape;"
              " see normalize_creator)")


if __name__ == "__main__":
    main()
