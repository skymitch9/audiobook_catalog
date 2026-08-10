#!/usr/bin/env python3
"""Emit `site/ebooks.json` — every ebook file in the library, with real metadata.

## Why this lives here and not in library_catalog

Because the heavy lifting and the source of truth stay in one project. This
pipeline already walks the whole book tree three times a day, already renames
ASIN-named epubs using their embedded metadata (`rename_epubs.py`, wired in as
sync step 1a), and already files loose companions next to their audiobook. It
knows about every ebook on disk. `library_catalog` should *read* that knowledge,
not re-derive it — one pipeline, one source of data.

A previous attempt did re-derive it, in the other repo, by guessing titles from
filenames. It produced `BtDEM 1 Oathbound Healer` where the embedded metadata
says `Oathbound Healer`. That is the whole argument for this file.

## What it emits

`site/ebooks.json`, alongside the other site JSONs the pipeline publishes:

```json
{
  "generated_at": "2026-08-10T02:00:00Z",
  "root": "C:/Users/nbasl/OpenAudible/books",
  "count": 118,
  "ebooks": [
    {
      "path": "Brandon Sanderson/Dragonsteel_Prime_by_Brandon_Sanderson.epub",
      "filename": "Dragonsteel_Prime_by_Brandon_Sanderson.epub",
      "format": "epub",
      "title": "Dragonsteel Prime",
      "author": "Brandon Sanderson",
      "source": "opf",
      "beside_audiobook": "Brandon Sanderson",
      "size_bytes": 812345,
      "modified": "2026-01-14T09:12:03Z"
    }
  ]
}
```

⚠️ **`source` is the field a consumer must respect.** `opf` means the title and
author were read out of the file itself and are trustworthy. `filename` means
they were parsed from the name because the file carries no usable metadata —
a PDF, or an EPUB with an empty `dc:title`. A consumer should treat `filename`
rows as provisional and let a person confirm them.

⚠️ **No `work_key` is emitted, deliberately.** That key is computed by exactly
one implementation, in `library_catalog/packages/core/src/titles.ts`. Emitting it
here would put a second copy of that fold in a second language — precisely the
bug this household has already shipped once (four author-splitters, two of which
disagree). This file publishes raw title and author; the consumer folds them.

## Usage

    python scripts/build_ebook_manifest.py            # write site/ebooks.json
    python scripts/build_ebook_manifest.py --dry      # print a summary only
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import ROOT_DIR  # noqa: E402
from scripts.rename_epubs import get_epub_metadata, sanitize_filename  # noqa: E402,F401

# Kept in step with app.metadata.COMPANION_EXTS, but listed explicitly because
# this file cares about *ebooks* specifically and that constant is about
# companions generally. If they ever diverge, that is a real difference and not
# a bug to paper over.
EBOOK_EXTS = {".epub", ".mobi", ".azw3", ".kepub", ".pdf"}

OUT_PATH = PROJECT_ROOT / "site" / "ebooks.json"


def title_author_from_filename(path: Path) -> tuple[str, str | None]:
    """Fallback when a file carries no usable embedded metadata.

    The pipeline's own step 1a renames root-level epubs to `Title - Author.epub`,
    so that shape is common and worth reading properly. Anything else returns the
    stem as the title with no author, which is honest — a wrong author is far
    worse than a missing one, because the consumer keys on it.
    """
    stem = path.stem
    # "Title - Author" — split on the LAST " - ", since titles contain hyphens
    # ("He Who Fights with Monsters 10- A LitRPG Adventure - Travis Deverell").
    if " - " in stem:
        title, author = stem.rsplit(" - ", 1)
        return title.strip(), author.strip() or None
    return stem.strip(), None


def scan(root: Path) -> list[dict]:
    ebooks: list[dict] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in EBOOK_EXTS:
            continue

        rel = path.relative_to(root)
        title: str | None = None
        author: str | None = None
        source = "filename"

        # ⚠️ Recursive, unlike rename_epubs' `glob("*.epub")`, which only ever
        # sees the root. 83 of the 118 ebooks here live in author folders and
        # have never been through step 1a, so reading their embedded metadata is
        # the only way to get a real title for them.
        if path.suffix.lower() == ".epub":
            meta = get_epub_metadata(path)
            if meta and meta.get("title"):
                title = meta["title"]
                author = meta.get("author")
                source = "opf"

        if not title:
            title, author = title_author_from_filename(path)

        # The immediate parent, when the file sits in an author folder rather
        # than loose in the root. `sort_companion_files` puts companions beside
        # the audiobook they belong to, so this is a real signal about which
        # book it accompanies — but it is NOT reliably an author name: this
        # library has folders named for series ("Highschool DXD", "Seirei
        # Tsukai no Blade Dance"). Published as-is; the consumer decides.
        beside = rel.parts[0] if len(rel.parts) > 1 else None

        stat = path.stat()
        ebooks.append(
            {
                "path": str(rel).replace("\\", "/"),
                "filename": path.name,
                "format": path.suffix.lower().lstrip("."),
                "title": title,
                "author": author,
                "source": source,
                "beside_audiobook": beside,
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        )
    return ebooks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry", action="store_true", help="summarise, write nothing")
    args = parser.parse_args()

    root = Path(ROOT_DIR)
    if not root.is_dir():
        print(f"[ebooks] ROOT_DIR not found: {root}")
        return 1

    ebooks = scan(root)

    by_format: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for e in ebooks:
        by_format[e["format"]] = by_format.get(e["format"], 0) + 1
        by_source[e["source"]] = by_source.get(e["source"], 0) + 1

    print(f"[ebooks] {len(ebooks)} file(s) under {root}")
    print(f"[ebooks]   by format: {by_format}")
    print(f"[ebooks]   metadata from: {by_source}")

    if args.dry:
        for e in ebooks[:10]:
            print(f"    [{e['source']:8}] {e['title']}  —  {e['author']}")
        print("[ebooks] dry run, nothing written")
        return 0

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "root": str(root).replace("\\", "/"),
        "count": len(ebooks),
        "ebooks": ebooks,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Written whole rather than appended: unlike additions_log.json this is a
    # snapshot of what is on disk right now, not a history. A file that is
    # deleted should disappear from it.
    tmp = OUT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(OUT_PATH)
    print(f"[ebooks] wrote {OUT_PATH.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
