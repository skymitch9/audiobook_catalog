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
      "audiobook_title": "Dragonsteel Prime - A Cosmere Novel",
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
  - `'pdf_page1'` — a PDF's own page 1, RENDERED (PyMuPDF) and staged through
    the same sha256/downscale/upload path as an EPUB cover. Owner approval,
    2026-08-17: "Apply and make it automatic but we need to check that first
    page ... make sure it's an image or at least some kind of cover page and
    not just a chapter or some huge block of text."
    ⚠️ **Gated, and the gate is the feature.** `classify_cover_page` reads
    page 1's text length, its union image coverage, and its ink and colour
    fractions, and only a page that is image-dominant, near-textless and
    actually inked is rendered. A chapter page, a legal page, or a SCAN of a
    printed page is refused. An ambiguous middle case is refused too, unless
    an optional Claude vision check is configured (it is not, on this machine
    — see `AI_COVER_KEY_ENV`). ⚠️ It never overwrites a sibling-audiobook
    cover: 26 of this library's 30 PDFs are covered that way already.
  - `null` — the page renders its typographic spine placeholder. ⚠️ For a
    `.pdf` only: **every EPUB must resolve a cover**, enforced by
    `tests/test_ebook_covers.py::test_every_published_epub_has_a_cover` and
    by `app.tools.audit_site` (the promote gate).

A refused (or otherwise coverless) book is NAMED in the manifest's top-level
`needs_human_cover` list — `{path, title, format, reason}` — which the same
two guards read: a coverless PDF must either resolve a cover **or** appear
there, so a text-first PDF cannot break promote and a silent cover gap cannot
exist. The list is published even when empty; see `build_needs_human_cover`.

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

## `audiobook_title` — what the AUDIOBOOK catalog calls this book (2026-08-17)

⚠️ Emitted per row, `null` when this ebook has no resolvable audiobook
sibling. It exists for the gated shelf's **content notes**, which read and
write the estate-wide `user_content_warnings` store — a store keyed by
`bookIdFromTitle(title)` where *title* is the audiobook catalog's spelling.

The ebook's own title comes from epub metadata and is a THIRD spelling of the
same book (`library_catalog/docs/info/content-warnings.md` §2 measured 27 of
92 shared books producing a different key from the library's own title). Key a
note on it and the note is filed where nobody looks **and** finds none of the
notes written elsewhere — both halves fail silently and both look exactly like
"nobody has added a warning yet". So the shelf keys on `audiobook_title` when
there is one, and on its own title only when there is not (that IS this
catalog's spelling for an ebook-only file).

Derived by `sibling_catalog_match` — the SAME conservative join the sibling
cover uses, extended to hand back the matched row's raw title. One join, so a
cover and a content note can never disagree about which audiobook this is.

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

# ---------------------------------------------------------------------------
# PDF page-1 auto-cover: the gate (owner approval, 2026-08-17)
#
# ⚠️ The owner's requirement, verbatim: "Apply and make it automatic but we
# need to check that first page ... make sure it's an image or at least some
# kind of cover page and not just a chapter or some huge block of text."
#
# So page 1 is RENDERED only if it looks like a cover. Two structural signals,
# both cheap, both read straight off the page:
#
#   text_chars     — extractable text on the page. A cover carries a title;
#                    a chapter or a legal page carries paragraphs.
#   image_coverage — the fraction of the page area covered by the UNION of
#                    its image boxes (grid-sampled). A cover is one big image
#                    (or a few tiles that add up to one); a title page has a
#                    small decorative logo, or nothing.
#
# ...plus two PIXEL signals that separate a real cover from a SCANNED PAGE OF
# TEXT, which is image-dominant and carries no extractable text at all — the
# one input the structural rule alone would wave through:
#
#   ink_fraction   — pixels that are not near-white. A scanned text page is
#                    mostly paper.
#   colour_fraction— pixels with real saturation. A scanned text page is grey.
#
# ⚠️ MEASURED against the four real PDFs and nine interior-page counterexamples
# (see tests/test_ebook_covers.py::test_the_pdf_gate_*). Ground truth, page 1:
#
#   file                       chars  imgcov    ink  colour   verdict
#   mistborn_adventuregame         0   1.000  0.983   0.726   cover
#   mistborn_alloyoflaw            2   1.000  0.962   0.508   cover  (8 tiles!)
#   mistborn_terris                0   1.000  0.986   0.813   cover
#   SL001_Stormlight_Handbook    102   1.000  0.999   0.812   cover
#
# and the counterexamples that must be REFUSED:
#
#   adventuregame p1             101   0.045  0.286   0.000   coverage
#   adventuregame p2/p5    1977/4868   0.000      -       -   text
#   alloyoflaw p1               1993   1.000  0.965   0.844   text  ⚠️
#   alloyoflaw p2                 89   0.045  0.282   0.000   coverage
#   terris p2/p5            108/3772   0.045      -       -   coverage/text
#   Stormlight p1/p2/p5    2572-7505   1.000  0.97+   0.03+   text  ⚠️
#
# ⚠️ The two marked ⚠️ are why BOTH signals are needed. Alloy of Law's page 2
# and every Stormlight interior page carry a FULL-PAGE image (coverage 1.0) and
# are unmistakably text pages — only the text count rejects them. Conversely
# Alloy of Law's own cover is EIGHT tiled images whose largest is 17% of the
# page — only the UNION coverage accepts it. Neither signal alone is the gate.
PDF_COVER_TEXT_MAX_CHARS = 300        # a cover carries a title, not paragraphs
PDF_TEXT_PAGE_CHARS = 800             # past this it is unambiguously a text page
PDF_IMAGE_COVERAGE_MIN = 0.60         # a cover is dominated by its art
PDF_IMAGE_COVERAGE_FLOOR = 0.30       # below this there is no dominant image
PDF_INK_MIN = 0.50                    # a scanned text page is mostly paper
PDF_COLOUR_MIN = 0.15                 # ...and mostly grey
PDF_COVERAGE_GRID = 40                # 1600 sample points; union, not max-box
PDF_PIXEL_PROBE_DPI = 36              # ~300px wide — enough for ink and colour
PDF_NEAR_WHITE_LEVEL = 239            # min(r,g,b) >= this counts as paper
PDF_SATURATION_LEVEL = 29             # max-min >= this counts as coloured

# The rendered cover. 1600px on the longest side matches DOWNSCALE_RUNGS[0], so
# a PDF cover and an EPUB cover land at the same page weight; anything over the
# cap still falls through `downscale_cover` like every other cover here.
PDF_RENDER_LONGEST_PX = 1600
PDF_RENDER_JPEG_QUALITY = 85

# The optional AI rung, for the AMBIGUOUS middle only (see `classify_cover_page`).
#
# ⚠️ NOT CONFIGURED on this machine, checked 2026-08-17: `.env` and
# `.env.example` name no Anthropic key (ROOT_DIR, GITHUB_TOKEN, HARDCOVER_TOKEN,
# DOESTHEDOGDIE_API_KEY, PIPELINE_TRIGGER_TOKEN, LIBRARY_MAPPING_TOKEN,
# POLL_SYNC_TOKEN ... and no ANTHROPIC_*). So the rung is skipped and every
# ambiguous page-1 is REFUSED by name. To turn it on, add ANTHROPIC_API_KEY to
# `.env` — `app.config` already calls load_dotenv() at import.
#
# ⚠️ Deliberately keyed on an EXPLICIT env var rather than a bare Anthropic()
# client. The SDK would also resolve an `ant auth login` profile from the
# owner's home directory, which would make an UNATTENDED pipeline (three runs a
# day) silently spend his personal credits. An unattended job gets an explicit
# key or it gets nothing.
AI_COVER_KEY_ENV = "ANTHROPIC_API_KEY"
AI_COVER_MODEL = "claude-haiku-4-5"  # the cheapest vision-capable model
AI_COVER_PROBE_DPI = 72              # ~600px — plenty for "cover or text page?"

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


def load_catalog_covers(catalog_path: Path) -> dict[str, list[tuple[str, str, str]]]:
    """catalog.csv's rows, grouped by the author/series folder its cover_href
    lives under — the same folder name `beside_audiobook` carries.

    Each row is `(normalised_title, cover_href, catalog_title)`. ⚠️ The THIRD
    element is the catalog's OWN spelling of the title, raw and unfolded, and
    it is not decoration: `sibling_catalog_match` hands it to the manifest as
    `audiobook_title`, which is the key the content-notes feature files and
    reads reader notes under (see this file's `scan()` and
    `site/ebook-notes.js`). The comparison form is fold-only and never
    emitted; the raw string is what travels.

    Soft on purpose: a missing or unreadable catalog means no sibling joins
    this run (covers degrade to extraction/placeholder), never a failed build.
    """
    by_folder: dict[str, list[tuple[str, str, str]]] = {}
    try:
        with catalog_path.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                href = (r.get("cover_href") or "").strip().replace("\\", "/")
                parts = href.split("/", 2)
                if len(parts) != 3 or parts[0] != "covers" or not parts[1] or not parts[2]:
                    continue
                raw = (r.get("title") or "").strip()
                by_folder.setdefault(parts[1], []).append((_norm_title(raw), href, raw))
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


def _agreed_row(rows: list[tuple[str, str, str]]) -> tuple[str, str, str] | None:
    """The one catalog row these candidates agree on, or None.

    Agreement is on the **cover_href**, which is the original rule and is kept
    verbatim: two catalog rows naming the same cover are one book spelled
    twice; two naming different covers are genuinely ambiguous and refused.
    Among rows that agree, the pick is `sorted()[0]` — deterministic, so a
    rebuild never silently swaps which spelling of the title travels.
    """
    if len({href for _, href, _ in rows}) != 1:
        return None
    return sorted(rows)[0]


def sibling_catalog_match(
    title: str | None, beside: str | None, by_folder: dict[str, list[tuple[str, str, str]]]
) -> tuple[str, str] | None:
    """`(cover_href, catalog_title)` for the audiobook this ebook sits beside.

    ⚠️ ONE join, TWO consumers. It began life as the cover join and is now
    also the **identity** join: `catalog_title` is *what the audiobook catalog
    itself calls this book*, which is the convention every content warning in
    this estate is keyed by (`library_catalog/docs/info/content-warnings.md`
    §2 — the library reaches the same answer from its own
    `audiobook_holding.title` cache). Deriving it here rather than a second
    time keeps one implementation of "which audiobook is this ebook?", and
    means a cover and a content note can never disagree about it.

    Conservative by design — a wrong cover is worse than a placeholder, and a
    wrong identity is worse still (it files a reader's note under a key nobody
    reads, which looks exactly like "nobody has added one yet"):
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
    exact = [r for r in candidates if r[0] == t]
    picked = _agreed_row(exact) if exact else None
    if picked is None and not exact:
        prefixed = [r for r in candidates if _subtitle_extension(t, r[0])]
        picked = _agreed_row(prefixed) if prefixed else None
    if picked is None:
        return None
    return picked[1], picked[2]


def sibling_cover_href(
    title: str | None, beside: str | None, by_folder: dict[str, list[tuple[str, str, str]]]
) -> str | None:
    """The catalog cover_href for the audiobook this ebook sits beside, or None.

    A thin read of `sibling_catalog_match` — kept as its own name because the
    cover tests, and every reader who arrives here looking for covers, know it
    by this one.
    """
    match = sibling_catalog_match(title, beside, by_folder)
    return match[0] if match else None


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


def extract_epub_cover(epub_path: Path) -> tuple[bytes, str] | None:  # noqa: C901
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


def _stage_cover_bytes(data: bytes, ext: str, covers_dir: Path, label: str) -> str | None:
    """Write one cover into the staging dir; return its R2 object key, or None.

    ⚠️ The ONE staging implementation — EPUB extraction and PDF page-1 renders
    both land here, so both are sha256-named (content-addressed), both dedupe
    to one object, and both ride the same step 5.7 upload. A second copy of
    this fold would be a second naming scheme, and the upload step keys on the
    path relative to site/covers.

    The write is skipped when the file already exists; the upload step's own
    sha diff makes the push idempotent too.
    """
    digest = hashlib.sha256(data).hexdigest()
    out = covers_dir / f"{digest}{ext}"
    try:
        if not out.exists():
            covers_dir.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
    except OSError as e:
        print(f"[ebooks] [WARN] could not stage cover for {label}: {e}")
        return None
    return f"{EBOOK_COVER_PREFIX}/{digest}{ext}"


def stage_epub_cover(epub_path: Path, covers_dir: Path) -> str | None:
    """Extract and stage one epub's cover; return its R2 object key, or None."""
    got = extract_epub_cover(epub_path)
    if got is None:
        return None
    data, ext = got
    return _stage_cover_bytes(data, ext, covers_dir, epub_path.name)


# ---------------------------------------------------------------------------
# Covers, source 2b: the PDF's own page 1, rendered — BEHIND A LIKENESS GATE
#
# Owner approval, 2026-08-17: "Apply and make it automatic but we need to check
# that first page ... make sure it's an image or at least some kind of cover
# page and not just a chapter or some huge block of text."
#
# So this is not "render page 1". It is "render page 1 IF page 1 looks like a
# cover, and otherwise say so by name". A refused PDF stays coverless and lands
# in the manifest's `needs_human_cover` list; a text page is NEVER shipped as
# a cover. See the threshold block near the top of this file for the measured
# ground truth the numbers are tuned against.
# ---------------------------------------------------------------------------
def page_cover_signals(page) -> dict:
    """The four gate signals for one PyMuPDF page.

    Returns `{text_chars, image_coverage, ink_fraction, colour_fraction}`. The
    two pixel signals are None when Pillow is unavailable — the caller degrades
    to structure-only and says so in its reason (never silently).

    Soft like the rest of this file: a page that will not render its own
    pixmap yields None pixel signals rather than raising.
    """
    rect = page.rect
    area = abs(rect.width * rect.height) or 1.0

    text_chars = len(page.get_text("text").strip())

    # Union coverage, grid-sampled. ⚠️ NOT max-single-image: Alloy of Law's
    # cover is eight tiles whose largest is 17% of the page, and a max-box
    # rule refuses it. Boxes are clipped to the page first, since a bleed
    # image can extend past the crop box and inflate a naive area sum.
    boxes = []
    for info in page.get_image_info():
        try:
            x0, y0, x1, y1 = info["bbox"]
        except (KeyError, TypeError, ValueError):
            continue
        x0, x1 = (x0, x1) if x0 <= x1 else (x1, x0)
        y0, y1 = (y0, y1) if y0 <= y1 else (y1, y0)
        x0, y0 = max(x0, rect.x0), max(y0, rect.y0)
        x1, y1 = min(x1, rect.x1), min(y1, rect.y1)
        if x1 > x0 and y1 > y0:
            boxes.append((x0, y0, x1, y1))

    n = PDF_COVERAGE_GRID
    hits = 0
    if boxes:
        for i in range(n):
            x = rect.x0 + (i + 0.5) * rect.width / n
            for j in range(n):
                y = rect.y0 + (j + 0.5) * rect.height / n
                if any(bx0 <= x <= bx1 and by0 <= y <= by1 for bx0, by0, bx1, by1 in boxes):
                    hits += 1
    coverage = hits / float(n * n)

    ink, colour = _page_pixel_signals(page)
    return {
        "text_chars": text_chars,
        "image_coverage": coverage,
        "ink_fraction": ink,
        "colour_fraction": colour,
        "page_area": area,
    }


def _page_pixel_signals(page) -> tuple[float | None, float | None]:
    """(non-white fraction, saturated fraction) of a low-DPI render, or (None, None).

    ⚠️ This is the SCANNED-TEXT-PAGE defence. A scan of a printed page is one
    full-page image with no extractable text — structurally identical to a
    cover — but it is ~85% white paper with no colour, where every one of this
    library's four real covers is 96-99% inked and 50-81% coloured.

    Computed with Pillow band arithmetic rather than a Python pixel loop: at
    36 DPI a letter page is ~120k pixels, and the loop form measured slower
    than the rest of the gate put together.
    """
    try:
        import io  # noqa: F401  (kept for symmetry with downscale_cover's imports)

        from PIL import Image, ImageChops
    except ImportError:
        return None, None

    try:
        import pymupdf

        pix = page.get_pixmap(dpi=PDF_PIXEL_PROBE_DPI)
        if pix.n != 3 or pix.alpha:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    except Exception as e:  # noqa: BLE001 — an unrenderable page is not a crash
        print(f"[ebooks] [WARN] could not probe PDF page pixels ({type(e).__name__}: {e})")
        return None, None

    try:
        total = im.width * im.height or 1
        r, g, b = im.split()
        low = ImageChops.darker(ImageChops.darker(r, g), b)   # per-pixel min channel
        high = ImageChops.lighter(ImageChops.lighter(r, g), b)  # per-pixel max channel
        sat = ImageChops.difference(high, low)
        near_white = sum(low.histogram()[PDF_NEAR_WHITE_LEVEL:])
        coloured = sum(sat.histogram()[PDF_SATURATION_LEVEL:])
        return 1.0 - near_white / total, coloured / total
    except Exception as e:  # noqa: BLE001
        print(f"[ebooks] [WARN] could not measure PDF page pixels ({type(e).__name__}: {e})")
        return None, None
    finally:
        im.close()


def classify_cover_page(signals: dict) -> tuple[str, str]:
    """('cover' | 'text' | 'ambiguous', a human-readable reason).

    The reason is not decoration — it is what lands in the manifest's
    `needs_human_cover` list and in the build log, so a person can see WHY a
    PDF was refused without re-deriving it. "1 book was refused" without
    saying which page looked like what is the failure this string exists to
    prevent.
    """
    chars = signals["text_chars"]
    cov = signals["image_coverage"]
    ink = signals["ink_fraction"]
    colour = signals["colour_fraction"]
    shape = f"{chars} chars of text, {cov:.0%} image coverage"
    if ink is not None and colour is not None:
        shape += f", {ink:.0%} ink, {colour:.0%} colour"
    else:
        shape += " (pixel check unavailable — Pillow not installed)"

    # 1. Unambiguously a text page. Checked FIRST because a full-page
    #    background image makes coverage useless here: every Stormlight
    #    Handbook interior page is 100% covered AND 2,500+ characters.
    if chars > PDF_TEXT_PAGE_CHARS:
        return "text", f"page 1 is a text page — {shape}"

    # 2. No dominant image and no colour: a title, legal or contents page.
    if cov < PDF_IMAGE_COVERAGE_FLOOR and not (colour is not None and colour >= PDF_COLOUR_MIN):
        return "text", f"page 1 carries no dominant image — {shape}"

    # 3. Cover-like: little text, image-dominant, and actually inked/coloured
    #    rather than a scan of a printed page. When the pixel probe is
    #    unavailable the structural half stands alone, and the reason says so.
    pixel_ok = (ink is None and colour is None) or (
        (ink or 0) >= PDF_INK_MIN or (colour or 0) >= PDF_COLOUR_MIN
    )
    if chars <= PDF_COVER_TEXT_MAX_CHARS and cov >= PDF_IMAGE_COVERAGE_MIN and pixel_ok:
        return "cover", f"page 1 looks like a cover — {shape}"

    # 4. Everything else. Refused unless the AI rung is configured and agrees.
    return "ambiguous", f"page 1 is ambiguous — {shape}"


def ai_cover_verdict(jpeg_bytes: bytes) -> bool | None:
    """True/False from one Claude vision call, or None when the rung is off.

    ⚠️ Rung (b) of the gate, and OPTIONAL BY DESIGN: it is consulted only for
    the ambiguous middle, never for a page the deterministic rung already
    settled. `AI_COVER_KEY_ENV` is unset on this machine, so in practice this
    returns None and the caller refuses the page by name — which is the safe
    direction, and the whole reason the deterministic rung is tuned to leave
    the middle small.

    Never raises: no key, no SDK, no network, a malformed answer — all None.
    """
    import os

    key = (os.environ.get(AI_COVER_KEY_ENV) or "").strip()
    if not key:
        return None
    try:
        import base64

        import anthropic
    except ImportError:
        print("[ebooks] [WARN] anthropic SDK not installed — AI cover check skipped")
        return None

    try:
        client = anthropic.Anthropic(api_key=key)
        response = client.messages.create(
            model=AI_COVER_MODEL,
            max_tokens=16,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": base64.standard_b64encode(jpeg_bytes).decode("ascii"),
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                "Is this a book or product COVER (title art, front jacket), "
                                "or an INTERIOR text page (a chapter, contents, copyright or "
                                "legal page, or a scan of printed text)? "
                                "Answer with exactly one word: COVER or INTERIOR."
                            ),
                        },
                    ],
                }
            ],
        )
    except Exception as e:  # noqa: BLE001 — a cover check must never break the build
        print(f"[ebooks] [WARN] AI cover check failed ({type(e).__name__}: {e})")
        return None

    # ⚠️ Check stop_reason before reading content: a refusal returns HTTP 200
    # with an empty content list, and content[0] would IndexError.
    if getattr(response, "stop_reason", None) == "refusal":
        print("[ebooks] [WARN] AI cover check was refused — treating as unavailable")
        return None
    answer = " ".join(
        b.text for b in response.content if getattr(b, "type", None) == "text"
    ).strip().upper()
    if answer.startswith("COVER"):
        return True
    if answer.startswith("INTERIOR"):
        return False
    print(f"[ebooks] [WARN] AI cover check gave an unusable answer ({answer!r})")
    return None


def render_pdf_page(page, longest_px: int, quality: int) -> bytes | None:
    """One page as JPEG bytes at roughly `longest_px` on its long side, or None."""
    try:
        rect = page.rect
        longest_pt = max(abs(rect.width), abs(rect.height)) or 1.0
        dpi = int(72.0 * longest_px / longest_pt)
        dpi = max(36, min(dpi, 300))
        pix = page.get_pixmap(dpi=dpi)
        return pix.tobytes("jpeg", jpg_quality=quality)
    except Exception as e:  # noqa: BLE001
        print(f"[ebooks] [WARN] could not render a PDF page ({type(e).__name__}: {e})")
        return None


def stage_pdf_cover(pdf_path: Path, covers_dir: Path) -> tuple[str | None, str]:
    """(R2 object key or None, the reason) for one PDF's page-1 auto-cover.

    ⚠️ Returns a REASON in both directions, always. A None key with no reason
    is exactly the silent cover gap the owner's check exists to stop; the
    caller records the reason in the manifest's `needs_human_cover` list.
    """
    try:
        import pymupdf
    except ImportError:
        return None, "PyMuPDF not installed — PDF page-1 auto-cover unavailable"

    try:
        with pymupdf.open(pdf_path) as doc:
            if doc.is_encrypted and doc.needs_pass:
                return None, "PDF is password-protected"
            if doc.page_count < 1:
                return None, "PDF has no pages"
            page = doc[0]
            signals = page_cover_signals(page)
            verdict, reason = classify_cover_page(signals)

            if verdict == "ambiguous":
                probe = render_pdf_page(page, 600, 80)
                said_cover = ai_cover_verdict(probe) if probe else None
                if said_cover is True:
                    verdict = "cover"
                    reason += " — AI vision check says cover"
                elif said_cover is False:
                    return None, reason + " — AI vision check says interior page"
                else:
                    return None, reason + " — no AI check available, so refused"
            if verdict != "cover":
                return None, reason

            data = render_pdf_page(page, PDF_RENDER_LONGEST_PX, PDF_RENDER_JPEG_QUALITY)
    except Exception as e:  # noqa: BLE001 — step 1b's soft-fail stance is sacred
        return None, f"PDF page 1 unreadable ({type(e).__name__}: {e})"

    if data is None:
        return None, reason + " — but page 1 would not render"
    if len(data) > MAX_COVER_BYTES:
        shrunk = downscale_cover(data)
        if shrunk is None:
            return None, reason + " — but the render missed the page-weight cap"
        print(
            f"[ebooks] downscaled rendered cover for {pdf_path.name}: "
            f"{len(data) / 1048576:.1f} MB -> {len(shrunk) / 1048576:.2f} MB"
        )
        data = shrunk

    key = _stage_cover_bytes(data, ".jpg", covers_dir, pdf_path.name)
    if key is None:
        return None, reason + " — but the render could not be staged"
    return key, reason


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
    cover_notes: dict[str, str] | None = None,
) -> list[dict]:
    """Every ebook under `root`, as manifest rows.

    `cover_notes` is an OUT parameter: pass a dict and it is filled with
    `{path -> why this book has no cover}` for the books the automatic sources
    could not settle. `build_manifest` turns it into the published
    `needs_human_cover` list. Kept off the rows themselves so the row schema
    every consumer reads stays exactly as documented.
    """
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

        # Covers, in the approved order:
        #   1. the sibling audiobook's cover — the catalog already publishes it,
        #      and ⚠️ it is NEVER overwritten: 26 of this library's 30 PDFs get
        #      their cover this way and the auto-render must not touch them;
        #   2. the epub's own embedded image — the book's OWN art;
        #   3. a hand-placed override;
        #   4. a PDF's rendered page 1, behind the likeness gate;
        #   5. null — the page's typographic spine placeholder.
        #
        # ⚠️ The override outranks the PDF render, which is the REVERSE of the
        # EPUB order (where the book's own cover beats an override). Deliberate:
        # an embedded EPUB cover is the publisher's own art and authoritative,
        # whereas a rendered page 1 is a MACHINE GUESS — a person who has gone
        # to the trouble of placing a cover has overruled the guess by doing so.
        rel_posix = str(rel).replace("\\", "/")
        suffix = path.suffix.lower()
        cover_url: str | None = None
        cover_source: str | None = None
        # ONE join, two answers: the sibling's cover AND the sibling's own
        # title. The title is the content-notes key (see `audiobook_title` in
        # the row below) and is recorded even when the cover it came with is
        # later beaten by an embedded EPUB cover — identity and artwork are
        # different questions and must not be able to disagree.
        sibling = sibling_catalog_match(title, beside, catalog_covers or {})
        href = sibling[0] if sibling else None
        audiobook_title = sibling[1] if sibling else None
        if href:
            cover_url = canonical_cover_url(href) or None
            cover_source = "audiobook" if cover_url else None
        elif extract and covers_dir is not None and suffix == ".epub":
            key = stage_epub_cover(path, covers_dir)
            if key:
                cover_url = canonical_cover_url("covers/" + key) or None
                cover_source = "epub" if cover_url else None
        if cover_url is None:
            override = (cover_overrides or {}).get(rel_posix)
            if override:
                cover_url = canonical_cover_url("covers/" + override) or None
                cover_source = "override" if cover_url else None
        if cover_url is None and extract and covers_dir is not None and suffix == ".pdf":
            key, why = stage_pdf_cover(path, covers_dir)
            if key:
                cover_url = canonical_cover_url("covers/" + key) or None
                cover_source = "pdf_page1" if cover_url else None
                print(f"[ebooks] auto-cover for {path.name}: {why}")
            if cover_url is None and cover_notes is not None:
                cover_notes[rel_posix] = why

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
                # ⚠️ WHAT THE AUDIOBOOK CATALOG CALLS THIS BOOK, or null.
                # The content-notes key (site/ebook-notes.js): the estate's
                # warnings are keyed by `bookIdFromTitle(<audiobook title>)`,
                # and the ebook's own epub-metadata title is a DIFFERENT
                # spelling — filing under it writes notes nobody reads and
                # reads none of the notes written elsewhere, both silently.
                # A raw title, never a slug: `bookIdFromTitle` has exactly one
                # implementation and it is in JavaScript (site/reviews.js).
                "audiobook_title": audiobook_title,
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "cover_url": cover_url,
                "cover_source": cover_source,
            }
        )
    return ebooks


# ---------------------------------------------------------------------------
# The needs-a-human list (PDF auto-covers, 2026-08-17)
# ---------------------------------------------------------------------------
NEEDS_HUMAN_COVER_KEY = "needs_human_cover"

# What a coverless book says when nothing more specific was recorded — an EPUB
# that resolved none of its three sources, or a PDF from a dry run.
DEFAULT_COVERLESS_REASON = (
    "no sibling audiobook cover, no embedded cover, and no hand-placed override"
)


def build_needs_human_cover(ebooks: list[dict], cover_notes: dict[str, str] | None = None) -> list[dict]:
    """Every coverless row, with the reason it is coverless.

    ⚠️ The point of this list is the OTHER half of the coverage guard. "Every
    EPUB has a cover" is enforceable because an EPUB always can have one; a PDF
    whose page 1 is genuinely a wall of text cannot, and refusing it is the
    CORRECT outcome. So the PDF rule is "resolves a cover OR is named here" —
    which lets a text-first PDF through the promote gate while making a SILENT
    cover gap impossible: a coverless PDF that is not on this list fails.

    Lists coverless rows of EVERY format, deliberately. An EPUB appearing here
    does NOT excuse it from `test_every_published_epub_has_a_cover` — that rule
    is unconditional, and this list is descriptive, not a way to opt out of it.
    """
    notes = cover_notes or {}
    out = []
    for e in ebooks:
        if (e.get("cover_url") or "").strip():
            continue
        out.append(
            {
                "path": e.get("path"),
                "title": e.get("title"),
                "format": e.get("format"),
                "reason": notes.get(e.get("path"), DEFAULT_COVERLESS_REASON),
            }
        )
    return out


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
    # extraction and PDF rendering stage files under site/covers/ebooks/, so
    # both are skipped.
    cover_notes: dict[str, str] = {}
    ebooks = scan(
        root,
        catalog_covers=load_catalog_covers(CATALOG_PATH),
        covers_dir=EBOOK_COVERS_DIR,
        extract=not dry,
        cover_overrides=load_cover_overrides(),
        cover_notes=cover_notes,
    )
    needs_human_cover = build_needs_human_cover(ebooks, cover_notes)

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
    print(f"[ebooks]   covers: {by_cover}" + ("  (dry: cover staging skipped)" if dry else ""))
    if needs_human_cover:
        print(f"[ebooks]   needs a human cover: {len(needs_human_cover)}")
        for e in needs_human_cover:
            print(f"[ebooks]     - {e['title']}  ({e['path']}) — {e['reason']}")

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
        # ⚠️ Published even when empty, and that is the point: the promote gate
        # treats an ABSENT key as "this ref predates the list" (a warning) and a
        # PRESENT key as enforceable. An empty list is the positive statement
        # "nothing is waiting on a person", which is what makes a silent cover
        # gap impossible on any manifest this script writes.
        "needs_human_cover": needs_human_cover,
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
