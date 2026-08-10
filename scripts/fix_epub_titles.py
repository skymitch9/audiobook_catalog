#!/usr/bin/env python3
"""Correct wrong `dc:title` values inside EPUBs, and remove byte-identical copies.

## Why this edits the files rather than the catalog

`library_catalog` builds its works from `site/ebooks.json`, which this pipeline
builds from each EPUB's embedded OPF metadata. Fixing a title in the catalog
would be undone by the next import. The file is the source of truth, so the file
is what gets corrected.

## What is wrong, specifically

Two Beneath the Dragoneye Moons files carry the **series name** as their
`dc:title`:

    BtDEM 15 Rise from the Ashes ...epub   dc:title = "Beneath the Dragoneye Moons"
    BtDEM 16 Of Gods and Dragons ...epub   dc:title = "Beneath the Dragoneye Moons"

Two different books with one title fold to one `work_key`, so the catalog merged
them into a single work carrying two editions. Their siblings are fine —
BtDEM 12 correctly says "Phoenix Peaks - MM" — so this is a defect in two files,
not a convention.

Titles are taken from the filename, which is right in both cases. No "- MM"
suffix is added: some siblings have it and some do not, and inventing one would
be guessing at something only the author knows.

## Duplicates

Two pairs are byte-identical — verified by size AND md5 before anything is
deleted, not assumed from the name:

    Selkie Myth/Copy of BtDEM 12 Phoenix Peaks ...   == the non-"Copy of" file
    Untapped - Dakota Krout.epub (library root)      == Dakota Krout/Untapped ...

⚠️ **The root-level copy is the one deleted, never the one in the author
folder.** `sort_companion_files()` files companions beside their audiobook, so
the author folder is where a companion belongs; a root-level twin is the
leftover.

## Safety

- Every modified EPUB is backed up to `<name>.epub.bak` beside itself first.
- A deletion happens only after md5 equality is confirmed **in this run**.
- The rewritten zip keeps `mimetype` first and STORED, which is the one rule a
  hand-rolled EPUB gets wrong and which readers reject when broken.
- The result is re-read with the pipeline's own `get_epub_metadata` and the run
  fails if the new title did not take.
- Dry run is the default.

    python scripts/fix_epub_titles.py            # show the plan
    python scripts/fix_epub_titles.py --commit
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import ROOT_DIR  # noqa: E402
from scripts.rename_epubs import get_epub_metadata  # noqa: E402

DC = "http://purl.org/dc/elements/1.1/"
OPF = "http://www.idpf.org/2007/opf"
CONTAINER_NS = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}

# (relative path, the title it should carry)
TITLE_FIXES = [
    ("Selkie Myth/BtDEM 15 Rise from the Ashes - Selkie Myth - 20250515.epub",
     "Rise from the Ashes"),
    ("Selkie Myth/BtDEM 16 Of Gods and Dragons - Selkie Myth - 20270827.epub",
     "Of Gods and Dragons"),
]

# (duplicate to remove, the original it must match byte-for-byte)
DUPLICATES = [
    ("Selkie Myth/Copy of BtDEM 12 Phoenix Peaks - Selkie Myth - 20250115.epub",
     "Selkie Myth/BtDEM 12 Phoenix Peaks - Selkie Myth - 20250115.epub"),
    ("Untapped - Dakota Krout.epub",
     "Dakota Krout/Untapped - Dakota Krout.epub"),
]


def md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def set_epub_title(path: Path, new_title: str) -> None:
    """Rewrite the EPUB with a corrected dc:title.

    A zip cannot be edited in place, so every entry is copied to a new archive
    with the OPF swapped. `mimetype` must be first and uncompressed or readers
    and Calibre both reject the file.
    """
    with zipfile.ZipFile(path) as z:
        container = ET.fromstring(z.read("META-INF/container.xml"))
        rootfile = container.find(".//c:rootfile", CONTAINER_NS)
        if rootfile is None:
            raise ValueError("no rootfile in container.xml")
        opf_name = rootfile.get("full-path")
        if not opf_name:
            raise ValueError("rootfile has no full-path")

        # Keep the OPF's namespace prefixes as they were, or the rewritten file
        # sprouts ns0: everywhere and some readers baulk.
        ET.register_namespace("", OPF)
        ET.register_namespace("dc", DC)
        opf_tree = ET.fromstring(z.read(opf_name))
        title_el = opf_tree.find(f".//{{{DC}}}title")
        if title_el is None:
            raise ValueError("no dc:title in the OPF")
        title_el.text = new_title
        new_opf = ET.tostring(opf_tree, encoding="utf-8", xml_declaration=True)

        entries = [(i, z.read(i.filename)) for i in z.infolist()]

    tmp = path.with_suffix(".epub.tmp")
    with zipfile.ZipFile(tmp, "w") as out:
        # mimetype first, stored.
        for info, data in entries:
            if info.filename == "mimetype":
                out.writestr(zipfile.ZipInfo("mimetype"), data, zipfile.ZIP_STORED)
                break
        for info, data in entries:
            if info.filename == "mimetype":
                continue
            payload = new_opf if info.filename == opf_name else data
            out.writestr(info, payload, zipfile.ZIP_DEFLATED)
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Correct wrong dc:title values in EPUBs and remove verified duplicates."
    )
    parser.add_argument("--commit", action="store_true", help="actually modify files")
    args = parser.parse_args()

    root = Path(ROOT_DIR)
    problems = 0

    print("== title fixes ==")
    for rel, want in TITLE_FIXES:
        p = root / rel
        if not p.is_file():
            print(f"  [missing] {rel}")
            problems += 1
            continue
        meta = get_epub_metadata(p)
        have = (meta or {}).get("title")
        if have == want:
            print(f"  [ok already] {want}")
            continue
        print(f"  {rel.split('/')[-1][:50]}")
        print(f"      {have!r} -> {want!r}")
        if not args.commit:
            continue
        shutil.copy2(p, p.with_suffix(".epub.bak"))
        set_epub_title(p, want)
        after = (get_epub_metadata(p) or {}).get("title")
        if after != want:
            print(f"      [FAIL] title is still {after!r}")
            problems += 1
        else:
            print(f"      [done] backup at {p.name}.bak")

    print("\n== duplicates ==")
    for dup_rel, keep_rel in DUPLICATES:
        dup, keep = root / dup_rel, root / keep_rel
        if not dup.is_file():
            print(f"  [gone already] {dup_rel}")
            continue
        if not keep.is_file():
            print(f"  [SKIP] the file to keep is missing: {keep_rel}")
            problems += 1
            continue
        # ⚠️ Verified in THIS run. A name that says "Copy of" is not evidence.
        if dup.stat().st_size != keep.stat().st_size or md5(dup) != md5(keep):
            print(f"  [SKIP - NOT IDENTICAL] {dup_rel}")
            problems += 1
            continue
        print(f"  identical, remove: {dup_rel}")
        print(f"                keep: {keep_rel}")
        if args.commit:
            dup.unlink()
            print("      [deleted]")

    if not args.commit:
        print("\nDRY RUN. Nothing changed. Re-run with --commit.")
    elif problems:
        print(f"\n[FAIL] {problems} problem(s) - read the output above")
        return 1
    else:
        print("\n[OK] all fixes applied and verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
