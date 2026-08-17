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
      "modified": "2026-01-14T09:12:03Z",
      "cover_url": "https://covers.heygabi.ai/Brandon Sanderson/….jpg",
      "cover_source": "audiobook"
    }
  ]
}
```

## Covers (bookshelf redesign, 2026-08-17)

Each row carries `cover_url` (absolute, or null) and `cover_source`:

  - `'audiobook'` — the ebook sits beside an audiobook whose cover the
    catalog already publishes. Joined against site/catalog.csv at build time,
    CONSERVATIVELY (see `sibling_cover_href`): a wrong cover is worse than a
    placeholder, so only an exact title match — or a single unambiguous
    "catalog title = ebook title + subtitle" extension — wins.
  - `'epub'` — extracted from the EPUB itself (an epub is a zip carrying its
    cover; `extract_epub_cover` reads the OPF's cover-image entry). The image
    is staged sha256-named under site/covers/ebooks/ (gitignored, like every
    other cover) and rides the EXISTING step 5.7 (scripts/upload_covers_r2.py)
    to the same R2 bucket under the `ebooks/` key prefix, recorded in
    site/covers_manifest.json. Step 1b stages, 5.7 uploads, step 6 commits —
    so a published ebooks.json never references an un-uploaded cover.
  - `'override'` — a hand-placed cover recorded in
    `scripts/ebook_cover_overrides.json` for a book that resolves neither of
    the above. Stored as an R2 object key, never a remote URL: a person
    fetches the image once and stages it like any other ebook cover, so the
    pipeline makes no network call and a dead upstream link cannot blank a
    cover already in the bucket.
  - `null` — the page renders its typographic spine placeholder. ⚠️ For a
    `.pdf` only: **every EPUB must resolve a cover**, enforced by
    `tests/test_ebook_covers.py::test_every_published_epub_has_a_cover` and
    by `app.tools.audit_site` (the promote gate).

Extraction is soft the way this whole file is soft: a malformed epub or a
missing OPF entry degrades to null, never breaks the build.

⚠️ **An oversized cover is DOWNSCALED, not rejected** (2026-08-17). The old
code returned None above `MAX_COVER_BYTES`, which is what left 15 of this
library's 16 "coverless" EPUBs coverless — every one of them declaring a
perfectly good 2–3 MB cover. See `downscale_cover`. Never fix a cover miss by
raising the cap.

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
import csv
import hashlib
import json
import posixpath
import re
import sys
import urllib.parse
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import ROOT_DIR  # noqa: E402
from app.index_push import canonical_cover_url  # noqa: E402  (the ONE URL canonicaliser — never a second copy)
from scripts.rename_epubs import get_epub_metadata, sanitize_filename  # noqa: E402,F401

# Kept in step with app.metadata.COMPANION_EXTS, but listed explicitly because
# this file cares about *ebooks* specifically and that constant is about
# companions generally. If they ever diverge, that is a real difference and not
# a bug to paper over.
EBOOK_EXTS = {".epub", ".mobi", ".azw3", ".kepub", ".pdf"}

OUT_PATH = PROJECT_ROOT / "site" / "ebooks.json"

# The catalog the sibling-cover join reads. At step 1b this is the PREVIOUS
# run's catalog (the rebuild happens later in the pipeline) — a brand-new
# audiobook's cover joins on the NEXT run, which is the conservative direction.
CATALOG_PATH = PROJECT_ROOT / "site" / "catalog.csv"

# Extracted EPUB covers are staged here, sha256-named, and picked up by the
# existing step 5.7 (scripts/upload_covers_r2.py) — its R2 object key is the
# path relative to site/covers, so these land under the `ebooks/` prefix in
# the SAME bucket, recorded in the same covers_manifest.json. The directory is
# inside gitignored site/covers/ — covers are never committed (the repo went
# fat once; see docs/info/covers-r2.md).
EBOOK_COVERS_DIR = PROJECT_ROOT / "site" / "covers" / "ebooks"
EBOOK_COVER_PREFIX = "ebooks"

# The page-weight budget for one shelf tile. An embedded cover over this is NOT
# rejected — it is DOWNSCALED (see `downscale_cover`).
#
# ⚠️ MEASURED, 2026-08-17: rejecting instead of downscaling is what left 15 of
# the 16 "coverless" EPUBs coverless. Every one of them declares a perfect
# cover; they were 2.1–3.4 MB (All The Skills 2/4/6, Arcane Pathfinder 5, six
# Cradle books, The Tenth Island, Undead Knight, The King Tides, Tamer 8,
# Seirei Tsukai vol 16) and the build silently dropped them all.
#
# ⚠️ NEVER "fix" this by raising the cap. The cap is the page-weight budget for
# a grid that renders ~168 tiles; a 3.4 MB tile is the bug, not the limit.
MAX_COVER_BYTES = 2 * 1024 * 1024

# Downscale targets. 1600px on the longest side is ~2.7x the largest rendered
# tile (the reading card's 158px face at 3x DPR is 474px), so the shelf keeps
# retina headroom while the bytes collapse; JPEG q85 is the usual
# visually-lossless knee. Fallback rungs exist because a few covers are
# noise-heavy enough to miss the cap at the first setting.
DOWNSCALE_RUNGS = ((1600, 85), (1400, 82), (1200, 78), (1000, 72), (800, 65))

# Above this, the manifest entry is not a cover at all — some epubs name a
# full-page scan or effectively the whole book as their cover item, and
# reading that into memory to re-encode it is the cost this ceiling refuses.
MAX_SOURCE_COVER_BYTES = 40 * 1024 * 1024

_COVER_EXT_BY_MEDIA_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

# Hand-placed covers for ebooks that resolve none of the automatic sources —
# source 3, below. Path-keyed, and it stores an R2 OBJECT KEY, never a remote
# URL: the image is fetched ONCE by a person, staged under site/covers/ebooks/
# like every other ebook cover, and uploaded by the existing step 5.7. The
# pipeline therefore never makes a network call to build the manifest, and a
# dead upstream link can never blank a cover that is already in the bucket.
COVER_OVERRIDES_PATH = PROJECT_ROOT / "scripts" / "ebook_cover_overrides.json"


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


# ---------------------------------------------------------------------------
# Covers, source 1: the sibling audiobook's cover (join against catalog.csv)
# ---------------------------------------------------------------------------
_NORM_RE = re.compile(r"[^a-z0-9]+")


def _norm_title(s: str | None) -> str:
    """Case/punctuation-folded comparison form. Comparison ONLY — never emitted
    (the no-fold rule protects what travels; a local join key travels nowhere)."""
    return _NORM_RE.sub(" ", (s or "").lower()).strip()


def load_catalog_covers(catalog_path: Path) -> dict[str, list[tuple[str, str]]]:
    """catalog.csv's covers, grouped by the author/series folder its cover_href
    lives under — the same folder name `beside_audiobook` carries.

    Soft on purpose: a missing or unreadable catalog means no sibling joins
    this run (covers degrade to extraction/placeholder), never a failed build.
    """
    by_folder: dict[str, list[tuple[str, str]]] = {}
    try:
        with catalog_path.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                href = (r.get("cover_href") or "").strip().replace("\\", "/")
                parts = href.split("/", 2)
                if len(parts) != 3 or parts[0] != "covers" or not parts[1] or not parts[2]:
                    continue
                by_folder.setdefault(parts[1], []).append((_norm_title(r.get("title")), href))
    except OSError:
        return {}
    except Exception as e:  # a torn CSV mid-rebuild must not kill step 1b
        print(f"[ebooks] [WARN] catalog unreadable for cover join ({e}) — no sibling covers this run")
        return {}
    return by_folder


def _subtitle_extension(ebook_norm: str, catalog_norm: str) -> bool:
    """True when the catalog title is the ebook title PLUS a subtitle.

    One direction only, deliberately:
      - catalog extends ebook ("Moonfall" -> "Moonfall - Beneath the Dragoneye
        Moons, Book 13") is a subtitle and safe;
      - ebook extends catalog ("Tamer: King of Dinosaurs Book 10" beside the
        catalog's "Tamer: King of Dinosaurs") is a DIFFERENT VOLUME and would
        pin book 1's cover on book 10 — measured against this library.
    The extension may not begin with a digit: "Title 2" for ebook "Title" is a
    sequel, and "…Monsters 1" vs "…Monsters 10" is blocked by the space rule.
    """
    if not ebook_norm or not catalog_norm.startswith(ebook_norm) or catalog_norm == ebook_norm:
        return False
    rest = catalog_norm[len(ebook_norm):]
    if not rest.startswith(" "):
        return False  # "…monsters 1" prefix of "…monsters 10"
    nxt = rest.lstrip()
    return bool(nxt) and not nxt[0].isdigit()


def sibling_cover_href(title: str | None, beside: str | None, by_folder: dict[str, list[tuple[str, str]]]) -> str | None:
    """The catalog cover_href for the audiobook this ebook sits beside, or None.

    Conservative by design — a wrong cover is worse than a placeholder:
      1. exact normalised title match in the same folder, if it names exactly
         one cover file;
      2. else a subtitle extension (see _subtitle_extension) matching exactly
         one cover file;
      3. anything ambiguous, reversed, or numeric-continued -> None.
    """
    t = _norm_title(title)
    if not t or not beside:
        return None
    candidates = by_folder.get(beside)
    if not candidates:
        return None
    exact = {href for ct, href in candidates if ct == t}
    if exact:
        return exact.pop() if len(exact) == 1 else None
    prefixed = {href for ct, href in candidates if _subtitle_extension(t, ct)}
    if len(prefixed) == 1:
        return prefixed.pop()
    return None


# ---------------------------------------------------------------------------
# Covers, source 2: the image inside the EPUB itself
# ---------------------------------------------------------------------------
def downscale_cover(data: bytes) -> bytes | None:
    """Re-encode an oversized cover to fit under MAX_COVER_BYTES, or None.

    ⚠️ This function exists because the old code REJECTED anything over the
    cap, and 15 of this library's 16 coverless EPUBs were rejections of
    perfectly good 2–3 MB covers. Downscale, don't reject.

    Always JPEG out (the caller therefore always names the staged file
    `.jpg`), and always VERIFIED against the cap before returning — a rung
    that still misses falls through to the next, and exhausting them returns
    None rather than a file that blows the page-weight budget.

    Soft like everything else in this file: no Pillow, an unreadable image, a
    truncated one — all None, never an exception. Pillow is in
    requirements.txt but the import stays deferred so a machine without it
    degrades to the old behaviour instead of failing to import the module.
    """
    try:
        import io

        from PIL import Image, ImageFile
    except ImportError:
        print("[ebooks] [WARN] Pillow not installed — oversized covers stay skipped")
        return None

    # Some epub covers are truncated JPEGs that still decode 99% fine; a
    # slightly short image beats no cover at all.
    ImageFile.LOAD_TRUNCATED_IMAGES = True

    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            # Flatten alpha/palette onto white: the output is JPEG, which has
            # no alpha channel, and RGBA->RGB without a matte goes black.
            if im.mode in ("RGBA", "LA", "P"):
                rgba = im.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.split()[-1])
                base = flat
            elif im.mode != "RGB":
                base = im.convert("RGB")
            else:
                base = im.copy()
    except Exception as e:  # noqa: BLE001 — a bad image is a null cover, not a crash
        print(f"[ebooks] [WARN] could not decode an oversized cover ({type(e).__name__}: {e})")
        return None

    try:
        for longest, quality in DOWNSCALE_RUNGS:
            im = base.copy()
            im.thumbnail((longest, longest), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
            out = buf.getvalue()
            if len(out) <= MAX_COVER_BYTES:  # VERIFY, never assume
                return out
    except Exception as e:  # noqa: BLE001
        print(f"[ebooks] [WARN] could not re-encode an oversized cover ({type(e).__name__}: {e})")
        return None
    finally:
        base.close()

    print("[ebooks] [WARN] a cover missed the size cap on every rung — skipped")
    return None


def extract_epub_cover(epub_path: Path) -> tuple[bytes, str] | None:
    """(image_bytes, file_extension) for the epub's cover image, or None.

    An epub is a zip; META-INF/container.xml names the OPF, and the OPF's
    manifest names the cover by either of the two common patterns:
      - EPUB3: <item properties="cover-image" …>
      - EPUB2: <meta name="cover" content="ITEM-ID"/> -> <item id="ITEM-ID" …>
      - last resort: an <item id="cover"|"cover-image"> with an image type.

    ⚠️ Never raises, same stance as get_epub_metadata: malformed zip, broken
    XML, missing entry, oversized or non-image cover all return None.
    """
    try:
        with zipfile.ZipFile(epub_path) as z:
            container = ET.parse(z.open("META-INF/container.xml"))
            ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
            rootfile = container.find(".//c:rootfile", ns)
            if rootfile is None:
                return None
            opf_path = rootfile.get("full-path")
            if not opf_path:
                return None
            opf = ET.parse(z.open(opf_path))
            opf_ns = "{http://www.idpf.org/2007/opf}"
            items = opf.findall(f".//{opf_ns}manifest/{opf_ns}item") or opf.findall(".//manifest/item")

            def _is_image(item) -> bool:
                return (item.get("media-type") or "").strip().lower().startswith("image/")

            cover_item = None
            for item in items:  # EPUB3
                if "cover-image" in (item.get("properties") or "").split() and _is_image(item):
                    cover_item = item
                    break
            if cover_item is None:  # EPUB2 <meta name="cover">
                cover_id = None
                for meta in opf.iter():
                    tag = meta.tag if isinstance(meta.tag, str) else ""
                    if (tag == "meta" or tag.endswith("}meta")) and (meta.get("name") or "").strip().lower() == "cover":
                        cover_id = (meta.get("content") or "").strip()
                        break
                if cover_id:
                    for item in items:
                        if item.get("id") == cover_id and _is_image(item):
                            cover_item = item
                            break
            if cover_item is None:  # conventional ids
                for item in items:
                    if (item.get("id") or "").strip().lower() in ("cover", "cover-image", "cover-img") and _is_image(item):
                        cover_item = item
                        break
            if cover_item is None:
                return None

            href = urllib.parse.unquote((cover_item.get("href") or "").strip())
            if not href:
                return None
            member = posixpath.normpath(posixpath.join(posixpath.dirname(opf_path), href))
            info = z.getinfo(member)
            if info.file_size == 0 or info.file_size > MAX_SOURCE_COVER_BYTES:
                return None

            # Over the page-weight cap -> DOWNSCALE, don't reject (the whole
            # point of the 2026-08-17 fix). The re-encode always emits JPEG,
            # so the declared media-type stops mattering on this path.
            if info.file_size > MAX_COVER_BYTES:
                shrunk = downscale_cover(z.read(member))
                if shrunk is None:
                    return None
                print(
                    f"[ebooks] downscaled cover for {epub_path.name}: "
                    f"{info.file_size / 1048576:.1f} MB -> {len(shrunk) / 1048576:.2f} MB"
                )
                return shrunk, ".jpg"

            ext = _COVER_EXT_BY_MEDIA_TYPE.get((cover_item.get("media-type") or "").strip().lower())
            if ext is None:
                return None
            return z.read(member), ext
    except Exception:
        return None


def stage_epub_cover(epub_path: Path, covers_dir: Path) -> str | None:
    """Extract and stage one epub's cover; return its R2 object key, or None.

    sha256-named (content-addressed), so re-runs are idempotent and identical
    covers dedupe to one object. The write is skipped when the file already
    exists — the upload step's own sha diff makes the push idempotent too.
    """
    got = extract_epub_cover(epub_path)
    if got is None:
        return None
    data, ext = got
    digest = hashlib.sha256(data).hexdigest()
    out = covers_dir / f"{digest}{ext}"
    try:
        if not out.exists():
            covers_dir.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
    except OSError as e:
        print(f"[ebooks] [WARN] could not stage cover for {epub_path.name}: {e}")
        return None
    return f"{EBOOK_COVER_PREFIX}/{digest}{ext}"


# ---------------------------------------------------------------------------
# Covers, source 3: a hand-placed cover for a book that carries none
# ---------------------------------------------------------------------------
def load_cover_overrides(path: Path | None = None) -> dict[str, str]:
    """`{ebook path -> R2 object key}` from scripts/ebook_cover_overrides.json.

    The escape hatch for the genuinely coverless book — an EPUB with no
    embedded image and no sibling audiobook, where the owner's rule ("all the
    epubs should have covers, minimum") can only be met by a person finding
    one. Adding an entry is a two-step, deliberately manual job: stage the
    image under site/covers/ebooks/<sha256>.<ext> and record its key here.

    Soft like the rest of the file: a missing, malformed or unreadable file
    means no overrides this run, never a failed build.
    """
    # Resolved at CALL time, never bound as a default argument: the tests
    # (and any future relocation) monkeypatch the module attribute, and a
    # default captured at def-time would silently ignore that.
    path = COVER_OVERRIDES_PATH if path is None else path
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        print(f"[ebooks] [WARN] cover overrides unreadable ({e}) — none applied this run")
        return {}
    covers = raw.get("covers") if isinstance(raw, dict) else None
    if not isinstance(covers, dict):
        return {}
    out: dict[str, str] = {}
    for rel, entry in covers.items():
        key = entry.get("key") if isinstance(entry, dict) else entry
        if isinstance(rel, str) and isinstance(key, str) and key.strip():
            out[rel.replace("\\", "/")] = key.strip()
    return out


# ---------------------------------------------------------------------------
# The per-book anchor (estate-search deep links, 2026-08-17)
# ---------------------------------------------------------------------------
def ebook_anchor(rel_path: str) -> str:
    """The stable `#fragment` that scrolls ebooks.heygabi.ai to this book.

    ⚠️ ONE implementation, emitted into the manifest, read by BOTH consumers —
    `app/index_push.py` (which builds `detail_url` from it) and
    `app/web/templates/ebooks.html` (which stamps it as the tile's element id).
    Neither recomputes it. A second copy of this fold in JavaScript is exactly
    the drift this repo has already shipped once, and here it would break
    silently: every estate-search result would land on a dead anchor and the
    page would simply not scroll, with no error anywhere.

    Derived from the same identity `index_push` keys on — the manifest `path`,
    which its `source_id` ('ebook:<path>') is also built from, unique by
    construction (one file, one path). Hashed rather than slugified because
    the raw path carries spaces, slashes, ampersands and non-ASCII; 12 hex
    digits is 48 bits, ample for ~170 books. Prefixed because a fragment id
    must not start with a digit.
    """
    return "b-" + hashlib.sha256(rel_path.encode("utf-8")).hexdigest()[:12]


def scan(
    root: Path,
    catalog_covers: dict[str, list[tuple[str, str]]] | None = None,
    covers_dir: Path | None = None,
    extract: bool = True,
    cover_overrides: dict[str, str] | None = None,
) -> list[dict]:
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

        # Covers, in the approved order: sibling audiobook's cover first (the
        # catalog already publishes it), the epub's own embedded image second,
        # a hand-placed override third, null fourth (the page's typographic
        # spine placeholder).
        rel_posix = str(rel).replace("\\", "/")
        cover_url: str | None = None
        cover_source: str | None = None
        href = sibling_cover_href(title, beside, catalog_covers or {})
        if href:
            cover_url = canonical_cover_url(href) or None
            cover_source = "audiobook" if cover_url else None
        elif extract and covers_dir is not None and path.suffix.lower() == ".epub":
            key = stage_epub_cover(path, covers_dir)
            if key:
                cover_url = canonical_cover_url("covers/" + key) or None
                cover_source = "epub" if cover_url else None
        if cover_url is None:
            override = (cover_overrides or {}).get(rel_posix)
            if override:
                cover_url = canonical_cover_url("covers/" + override) or None
                cover_source = "override" if cover_url else None

        stat = path.stat()
        ebooks.append(
            {
                "path": rel_posix,
                "anchor": ebook_anchor(rel_posix),
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
                "cover_url": cover_url,
                "cover_source": cover_source,
            }
        )
    return ebooks


def build_manifest(dry: bool = False) -> int:
    """Scan the library and write `site/ebooks.json`. Returns an exit code.

    The callable form of this script, so the sync pipeline (sync step 1b in
    `scripts/sync_to_drive.py`) runs the SAME implementation the CLI does —
    a second copy of the scan is exactly the drift this file's header warns
    about. `dry` prints a summary and writes nothing.
    """
    root = Path(ROOT_DIR)
    if not root.is_dir():
        print(f"[ebooks] ROOT_DIR not found: {root}")
        return 1

    # Dry runs stay read-only: the sibling join is a pure read, but epub
    # extraction stages files under site/covers/ebooks/, so it is skipped.
    ebooks = scan(
        root,
        catalog_covers=load_catalog_covers(CATALOG_PATH),
        covers_dir=EBOOK_COVERS_DIR,
        extract=not dry,
        cover_overrides=load_cover_overrides(),
    )

    by_format: dict[str, int] = {}
    by_source: dict[str, int] = {}
    by_cover: dict[str, int] = {}
    for e in ebooks:
        by_format[e["format"]] = by_format.get(e["format"], 0) + 1
        by_source[e["source"]] = by_source.get(e["source"], 0) + 1
        by_cover[e["cover_source"] or "placeholder"] = by_cover.get(e["cover_source"] or "placeholder", 0) + 1

    print(f"[ebooks] {len(ebooks)} file(s) under {root}")
    print(f"[ebooks]   by format: {by_format}")
    print(f"[ebooks]   metadata from: {by_source}")
    print(f"[ebooks]   covers: {by_cover}" + ("  (dry: epub extraction skipped)" if dry else ""))

    if dry:
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
    try:
        shown = OUT_PATH.relative_to(PROJECT_ROOT)
    except ValueError:  # tests redirect OUT_PATH outside the repo
        shown = OUT_PATH
    print(f"[ebooks] wrote {shown}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry", action="store_true", help="summarise, write nothing")
    args = parser.parse_args()
    return build_manifest(dry=args.dry)


if __name__ == "__main__":
    raise SystemExit(main())
