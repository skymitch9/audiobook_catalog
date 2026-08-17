# tests/test_ebook_covers.py
"""
Covers in the ebook manifest (scripts/build_ebook_manifest.py) — the
bookshelf redesign's pipeline half.

Three sources, in the approved order, and the tests pin the ORDER as much as
the mechanics:

  1. sibling audiobook cover (join against site/catalog.csv, conservative —
     a wrong cover is worse than a placeholder);
  2. the image inside the EPUB itself (OPF cover-image entry), staged
     sha256-named under site/covers/ebooks/ for the existing R2 upload step —
     DOWNSCALED when oversized, never rejected (2026-08-17);
  3. a hand-placed cover from scripts/ebook_cover_overrides.json;
  4. null -> the page's typographic spine placeholder. ⚠️ Reachable by PDFs
     only: `test_every_published_epub_has_a_cover` is the owner's rule that
     every EPUB resolves one, and `test_the_coverage_guard_actually_fires`
     proves that guard can fail.

⚠️ The join's conservatism is the point of half these tests: the "Tamer:
King of Dinosaurs Book 10 must NOT wear book 1's cover" cases are measured
against the real library, not hypothetical.
"""

import json
import re
import zipfile
from pathlib import Path

import pytest

import scripts.build_ebook_manifest as bem

BASE = "https://covers.heygabi.ai/"


# --------------------------------------------------------------------------- #
# Sibling join — source 1
# --------------------------------------------------------------------------- #


def folder_index(*rows):
    """rows: (folder, catalog_title, href_filename)."""
    by_folder = {}
    for folder, title, fname in rows:
        by_folder.setdefault(folder, []).append(
            (bem._norm_title(title), f"covers/{folder}/{fname}")
        )
    return by_folder


def test_exact_title_match_wins():
    idx = folder_index(("A. American", "Avenging Home", "Avenging Home.jpg"))
    assert (
        bem.sibling_cover_href("Avenging Home", "A. American", idx)
        == "covers/A. American/Avenging Home.jpg"
    )


def test_match_is_case_and_punctuation_insensitive_but_href_is_raw():
    idx = folder_index(("X", "He Who Fights with Monsters 12: A LitRPG Adventure", "b12.jpg"))
    assert (
        bem.sibling_cover_href("He Who Fights with Monsters 12- A LitRPG Adventure", "X", idx)
        == "covers/X/b12.jpg"
    )


def test_subtitle_extension_matches_when_unique():
    # "Moonfall" (epub) beside "Moonfall - Beneath the Dragoneye Moons, Book 13"
    idx = folder_index(
        ("Selkie Myrtle", "Moonfall - Beneath the Dragoneye Moons, Book 13", "moonfall.jpg"),
        ("Selkie Myrtle", "Rise from the Ashes - Beneath the Dragoneye Moons, Book 15", "rise.jpg"),
    )
    assert bem.sibling_cover_href("Moonfall", "Selkie Myrtle", idx) == "covers/Selkie Myrtle/moonfall.jpg"


def test_reverse_extension_never_matches():
    # The Tamer case, measured on this library: the ebook is "… Book 10", the
    # catalog row is book 1 without a number. Matching would pin the WRONG
    # cover; the placeholder is the correct outcome.
    idx = folder_index(("Michael-Scott Earle", "Tamer: King of Dinosaurs", "tamer1.jpg"))
    assert bem.sibling_cover_href("Tamer: King of Dinosaurs Book 10", "Michael-Scott Earle", idx) is None


def test_numeric_continuation_is_not_a_prefix_match():
    # "…Monsters 1" must not claim "…Monsters 10 - …"'s cover.
    idx = folder_index(("X", "He Who Fights with Monsters 10 - A LitRPG Adventure", "b10.jpg"))
    assert bem.sibling_cover_href("He Who Fights with Monsters 1", "X", idx) is None


def test_extension_starting_with_a_number_is_a_sequel_not_a_subtitle():
    idx = folder_index(("X", "Title 2", "t2.jpg"))
    assert bem.sibling_cover_href("Title", "X", idx) is None


def test_ambiguous_extension_matches_nothing():
    idx = folder_index(
        ("X", "Legion: Skin Deep", "a.jpg"),
        ("X", "Legion: Lies of the Beholder", "b.jpg"),
    )
    assert bem.sibling_cover_href("Legion", "X", idx) is None


def test_exact_beats_extension():
    idx = folder_index(
        ("X", "Dungeon Crawler Carl", "dcc1.jpg"),
        ("X", "Dungeon Crawler Carl's Christmas Special", "xmas.jpg"),
    )
    assert bem.sibling_cover_href("Dungeon Crawler Carl", "X", idx) == "covers/X/dcc1.jpg"


def test_duplicate_exact_titles_with_different_covers_are_ambiguous():
    idx = folder_index(("X", "Same Title", "one.jpg"), ("X", "Same Title", "two.jpg"))
    assert bem.sibling_cover_href("Same Title", "X", idx) is None


def test_duplicate_exact_titles_with_the_same_cover_are_fine():
    idx = folder_index(("X", "Same Title", "one.jpg"), ("X", "Same Title", "one.jpg"))
    assert bem.sibling_cover_href("Same Title", "X", idx) == "covers/X/one.jpg"


def test_no_folder_or_unknown_folder_matches_nothing():
    idx = folder_index(("X", "Book", "b.jpg"))
    assert bem.sibling_cover_href("Book", None, idx) is None
    assert bem.sibling_cover_href("Book", "Y", idx) is None
    assert bem.sibling_cover_href("", "X", idx) is None


def test_load_catalog_covers_groups_by_folder(tmp_path):
    p = tmp_path / "catalog.csv"
    p.write_text(
        "title,author,cover_href\n"
        'Book One,A,"covers/Folder A/Book One.jpg"\n'
        'Book Two,B,"covers/Folder B/Book Two.jpg"\n'
        "No Cover,C,\n"
        "Weird,D,notcovers/x/y.jpg\n",
        encoding="utf-8",
    )
    idx = bem.load_catalog_covers(p)
    assert set(idx) == {"Folder A", "Folder B"}
    assert idx["Folder A"] == [("book one", "covers/Folder A/Book One.jpg")]


def test_load_catalog_covers_missing_file_is_empty(tmp_path):
    assert bem.load_catalog_covers(tmp_path / "absent.csv") == {}


# --------------------------------------------------------------------------- #
# EPUB extraction — source 2
# --------------------------------------------------------------------------- #

JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-bytes" * 10

CONTAINER = (
    '<?xml version="1.0"?>'
    '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>'
    "</container>"
)


def make_epub(
    path: Path,
    *,
    title="A Book",
    author="An Author",
    cover_bytes=JPEG,
    cover_href="images/cover.jpg",
    media_type="image/jpeg",
    pattern="epub3",  # 'epub3' | 'epub2' | 'id-fallback' | 'none'
):
    if pattern == "epub3":
        item = f'<item id="cimg" href="{cover_href}" media-type="{media_type}" properties="cover-image"/>'
        meta = ""
    elif pattern == "epub2":
        item = f'<item id="my-cover" href="{cover_href}" media-type="{media_type}"/>'
        meta = '<meta name="cover" content="my-cover"/>'
    elif pattern == "id-fallback":
        item = f'<item id="cover" href="{cover_href}" media-type="{media_type}"/>'
        meta = ""
    else:
        item = ""
        meta = ""
    opf = (
        '<?xml version="1.0"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>{meta}"
        "</metadata>"
        f"<manifest>{item}"
        '<item id="txt" href="text.xhtml" media-type="application/xhtml+xml"/></manifest>'
        "</package>"
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/text.xhtml", "<html/>")
        if cover_bytes is not None and pattern != "none":
            z.writestr("OEBPS/" + cover_href, cover_bytes)
    return path


def test_extracts_epub3_cover_image_property(tmp_path):
    p = make_epub(tmp_path / "b.epub", pattern="epub3")
    data, ext = bem.extract_epub_cover(p)
    assert data == JPEG and ext == ".jpg"


def test_extracts_epub2_meta_cover(tmp_path):
    p = make_epub(tmp_path / "b.epub", pattern="epub2")
    data, ext = bem.extract_epub_cover(p)
    assert data == JPEG and ext == ".jpg"


def test_extracts_conventional_cover_id(tmp_path):
    p = make_epub(tmp_path / "b.epub", pattern="id-fallback")
    data, ext = bem.extract_epub_cover(p)
    assert data == JPEG and ext == ".jpg"


def test_href_resolves_relative_to_the_opf_directory(tmp_path):
    # The cover lives at OEBPS/images/cover.jpg while the zip has no
    # top-level images/ — resolving against the zip root would KeyError.
    p = make_epub(tmp_path / "b.epub", cover_href="images/cover.jpg")
    assert bem.extract_epub_cover(p) is not None


def test_png_media_type_maps_to_png_extension(tmp_path):
    p = make_epub(tmp_path / "b.epub", cover_href="c.png", media_type="image/png")
    _, ext = bem.extract_epub_cover(p)
    assert ext == ".png"


def test_no_cover_entry_is_none(tmp_path):
    p = make_epub(tmp_path / "b.epub", pattern="none")
    assert bem.extract_epub_cover(p) is None


def test_non_image_media_type_is_none(tmp_path):
    p = make_epub(tmp_path / "b.epub", media_type="application/xhtml+xml", cover_href="fake.xhtml")
    assert bem.extract_epub_cover(p) is None


def test_oversized_garbage_that_is_not_an_image_is_skipped(tmp_path):
    # Over the cap AND undecodable -> None. (It used to be "over the cap ->
    # None" full stop; see the downscale tests below for why that was a bug.)
    big = b"\xff" * (bem.MAX_COVER_BYTES + 1)
    p = make_epub(tmp_path / "b.epub", cover_bytes=big)
    assert bem.extract_epub_cover(p) is None


def test_cover_over_the_hard_source_ceiling_is_skipped(tmp_path):
    # Some epubs name a full-page scan (or effectively the whole book) as their
    # cover item. Past MAX_SOURCE_COVER_BYTES we refuse to read it into memory
    # to re-encode it — that is not a cover.
    huge = b"\x00" * (bem.MAX_SOURCE_COVER_BYTES + 1)
    p = make_epub(tmp_path / "b.epub", cover_bytes=huge)
    assert bem.extract_epub_cover(p) is None


def test_empty_cover_is_skipped(tmp_path):
    p = make_epub(tmp_path / "b.epub", cover_bytes=b"")
    assert bem.extract_epub_cover(p) is None


def test_malformed_zip_is_none_not_a_crash(tmp_path):
    p = tmp_path / "broken.epub"
    p.write_text("this is not a zip", encoding="utf-8")
    assert bem.extract_epub_cover(p) is None


def test_missing_cover_member_is_none(tmp_path):
    # OPF names a cover that is not actually in the zip.
    p = tmp_path / "b.epub"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf">'
            '<manifest><item id="c" href="gone.jpg" media-type="image/jpeg" properties="cover-image"/>'
            "</manifest></package>",
        )
    assert bem.extract_epub_cover(p) is None


# --------------------------------------------------------------------------- #
# Downscale-not-reject — the 2026-08-17 fix
#
# ⚠️ MEASURED, and the reason these tests exist: 15 of this library's 16
# "coverless" EPUBs declared a perfectly good cover and were being DROPPED for
# being 2.1–3.4 MB (All The Skills 2/4/6, Arcane Pathfinder 5, six Cradle
# books, The Tenth Island, Undead Knight, The King Tides, Tamer 8, Seirei
# Tsukai vol 16). The fix is to re-encode, never to raise the cap.
# --------------------------------------------------------------------------- #

# ⚠️ Pillow is skipped PER TEST, never at module level. A module-level
# importorskip would take the every-EPUB-has-a-cover guard down with it on any
# machine missing Pillow — the one check that must never be silently absent.
# (Pillow is in requirements.txt precisely so CI does run these.)
requires_pillow = pytest.mark.skipif(
    __import__("importlib.util", fromlist=["util"]).find_spec("PIL") is None,
    reason="Pillow drives the downscale path",
)


def big_jpeg(longest=4000, bytes_over=bem.MAX_COVER_BYTES):
    """A real JPEG comfortably over the cap: noise, so it will not compress away."""
    import io
    import os

    from PIL import Image

    im = Image.frombytes("RGB", (longest, longest), os.urandom(longest * longest * 3))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=98)
    data = buf.getvalue()
    assert len(data) > bytes_over, f"fixture is not oversized ({len(data)} bytes)"
    return data


@requires_pillow
def test_downscale_brings_a_real_oversized_cover_under_the_cap():
    out = bem.downscale_cover(big_jpeg())
    assert out is not None
    assert len(out) <= bem.MAX_COVER_BYTES  # VERIFIED, not assumed


@requires_pillow
def test_downscale_caps_the_longest_side_and_keeps_the_aspect_ratio():
    import io

    from PIL import Image

    im = Image.new("RGB", (3000, 2000), (200, 30, 30))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=98)
    out = bem.downscale_cover(buf.getvalue())
    assert out is not None
    with Image.open(io.BytesIO(out)) as got:
        assert max(got.size) <= bem.DOWNSCALE_RUNGS[0][0]
        assert got.size == (1600, 1067)  # 3:2 preserved


@requires_pillow
def test_downscale_flattens_transparency_onto_white_not_black():
    # RGBA -> JPEG without a matte goes BLACK, which would silently ruin every
    # cover with a transparent margin.
    import io

    from PIL import Image

    im = Image.new("RGBA", (2400, 2400), (255, 255, 255, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    out = bem.downscale_cover(buf.getvalue())
    assert out is not None
    with Image.open(io.BytesIO(out)) as got:
        assert got.mode == "RGB"
        assert got.getpixel((10, 10)) > (240, 240, 240)


def test_downscale_of_non_image_bytes_is_none_not_a_crash():
    assert bem.downscale_cover(b"\xff" * 4096) is None
    assert bem.downscale_cover(b"") is None


@requires_pillow
def test_oversized_epub_cover_is_downscaled_and_staged_as_jpeg(tmp_path):
    # The regression in one test: an EPUB whose declared cover is over the cap
    # now yields a cover, staged under the cap, always .jpg.
    p = make_epub(tmp_path / "b.epub", cover_bytes=big_jpeg(), cover_href="cover.jpg")
    got = bem.extract_epub_cover(p)
    assert got is not None
    data, ext = got
    assert ext == ".jpg" and len(data) <= bem.MAX_COVER_BYTES

    covers = tmp_path / "covers" / "ebooks"
    key = bem.stage_epub_cover(p, covers)
    assert key and key.startswith("ebooks/") and key.endswith(".jpg")
    assert (covers / key.split("/", 1)[1]).stat().st_size <= bem.MAX_COVER_BYTES


@requires_pillow
def test_an_oversized_png_cover_is_downscaled_to_jpg_not_dropped(tmp_path):
    # The old code only ever emitted the source media type's extension; the
    # re-encode always emits JPEG, so the declared type stops mattering here.
    import io

    from PIL import Image
    import os

    im = Image.frombytes("RGB", (2600, 2600), os.urandom(2600 * 2600 * 3))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    p = make_epub(
        tmp_path / "b.epub", cover_bytes=buf.getvalue(), cover_href="c.png", media_type="image/png"
    )
    data, ext = bem.extract_epub_cover(p)
    assert ext == ".jpg" and len(data) <= bem.MAX_COVER_BYTES


def test_an_under_cap_cover_is_passed_through_byte_for_byte(tmp_path):
    # Downscaling must not touch covers that were already fine — the staged
    # bytes are content-addressed, so a needless re-encode would churn every
    # sha256 and re-upload the whole shelf.
    p = make_epub(tmp_path / "b.epub")
    data, ext = bem.extract_epub_cover(p)
    assert data == JPEG and ext == ".jpg"


# --------------------------------------------------------------------------- #
# Hand-placed overrides — source 3
# --------------------------------------------------------------------------- #


def test_load_cover_overrides_reads_path_to_key(tmp_path):
    p = tmp_path / "ov.json"
    p.write_text(
        json.dumps({"covers": {"A\\b.epub": {"key": "ebooks/deadbeef.jpg"}, "C/d.epub": "ebooks/x.jpg"}}),
        encoding="utf-8",
    )
    assert bem.load_cover_overrides(p) == {"A/b.epub": "ebooks/deadbeef.jpg", "C/d.epub": "ebooks/x.jpg"}


def test_missing_or_malformed_overrides_are_empty_never_an_error(tmp_path):
    assert bem.load_cover_overrides(tmp_path / "absent.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert bem.load_cover_overrides(bad) == {}
    wrong = tmp_path / "wrong.json"
    wrong.write_text('{"covers": ["a"]}', encoding="utf-8")
    assert bem.load_cover_overrides(wrong) == {}


def test_the_shipped_overrides_file_parses_and_its_covers_are_staged_or_uploaded():
    """Every override entry must name a cover that actually exists somewhere.

    An override whose key is a typo silently produces a 404 tile — exactly the
    failure the every-EPUB-has-a-cover guard cannot see, because the manifest
    row is non-null and only the image is missing.
    """
    overrides = bem.load_cover_overrides()
    if not overrides:
        pytest.skip("no overrides configured")
    manifest_path = bem.PROJECT_ROOT / "site" / "covers_manifest.json"
    uploaded = set()
    if manifest_path.exists():
        uploaded = set((json.loads(manifest_path.read_text(encoding="utf-8")).get("files") or {}).keys())
    missing = [
        f"{rel} -> {key}"
        for rel, key in overrides.items()
        if key not in uploaded and not (bem.PROJECT_ROOT / "site" / "covers" / key).exists()
    ]
    assert not missing, "override covers neither uploaded nor on disk: " + "; ".join(missing)


def test_override_fills_a_cover_the_automatic_sources_cannot(build_env, monkeypatch, tmp_path):
    root, out, _covers, _catalog = build_env
    make_epub(root / "Author Folder" / "book.epub", pattern="none")  # no embedded cover
    ov = tmp_path / "ov.json"
    ov.write_text(
        json.dumps({"covers": {"Author Folder/book.epub": {"key": "ebooks/abc123.jpg"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(bem, "COVER_OVERRIDES_PATH", ov)
    assert bem.build_manifest() == 0
    (e,) = _written(out)["ebooks"]
    assert e["cover_source"] == "override"
    assert e["cover_url"] == BASE + "ebooks/abc123.jpg"


def test_an_epubs_own_cover_beats_an_override(build_env, monkeypatch, tmp_path):
    # The override is a FALLBACK, never a hijack: a book that later gains a
    # real embedded cover uses its own.
    root, out, _covers, _catalog = build_env
    make_epub(root / "Author Folder" / "book.epub")
    ov = tmp_path / "ov.json"
    ov.write_text(
        json.dumps({"covers": {"Author Folder/book.epub": {"key": "ebooks/abc123.jpg"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(bem, "COVER_OVERRIDES_PATH", ov)
    assert bem.build_manifest() == 0
    (e,) = _written(out)["ebooks"]
    assert e["cover_source"] == "epub"


def test_stage_is_sha256_named_and_idempotent(tmp_path):
    import hashlib

    p = make_epub(tmp_path / "b.epub")
    covers = tmp_path / "covers" / "ebooks"
    key = bem.stage_epub_cover(p, covers)
    digest = hashlib.sha256(JPEG).hexdigest()
    assert key == f"ebooks/{digest}.jpg"
    staged = covers / f"{digest}.jpg"
    assert staged.read_bytes() == JPEG
    first_mtime = staged.stat().st_mtime_ns
    assert bem.stage_epub_cover(p, covers) == key  # second run: same key,
    assert staged.stat().st_mtime_ns == first_mtime  # no rewrite


def test_stage_of_coverless_epub_is_none_and_writes_nothing(tmp_path):
    p = make_epub(tmp_path / "b.epub", pattern="none")
    covers = tmp_path / "covers" / "ebooks"
    assert bem.stage_epub_cover(p, covers) is None
    assert not covers.exists()


# --------------------------------------------------------------------------- #
# The whole build — fields, order of the sources, soft-fail stance
# --------------------------------------------------------------------------- #


@pytest.fixture
def build_env(tmp_path, monkeypatch):
    """A tiny library + redirected module paths; returns (root, out_path, covers_dir, catalog_path)."""
    root = tmp_path / "library"
    (root / "Author Folder").mkdir(parents=True)
    out = tmp_path / "ebooks.json"
    covers = tmp_path / "site-covers" / "ebooks"
    catalog = tmp_path / "catalog.csv"
    monkeypatch.setattr(bem, "ROOT_DIR", root)
    monkeypatch.setattr(bem, "OUT_PATH", out)
    monkeypatch.setattr(bem, "EBOOK_COVERS_DIR", covers)
    monkeypatch.setattr(bem, "CATALOG_PATH", catalog)
    return root, out, covers, catalog


def _written(out: Path) -> dict:
    return json.loads(out.read_text(encoding="utf-8"))


def test_build_emits_cover_fields_from_epub_extraction(build_env):
    root, out, covers, _catalog = build_env
    make_epub(root / "Author Folder" / "book.epub")
    assert bem.build_manifest() == 0
    (e,) = _written(out)["ebooks"]
    assert e["cover_source"] == "epub"
    assert e["cover_url"].startswith("https://covers.heygabi.ai/ebooks/")
    assert e["cover_url"].endswith(".jpg")
    assert len(list(covers.iterdir())) == 1


def test_sibling_cover_wins_over_extraction(build_env):
    root, out, covers, catalog = build_env
    make_epub(root / "Author Folder" / "book.epub", title="Shelf Book")
    catalog.write_text(
        "title,author,cover_href\n"
        'Shelf Book,A,"covers/Author Folder/Shelf Book.jpg"\n',
        encoding="utf-8",
    )
    assert bem.build_manifest() == 0
    (e,) = _written(out)["ebooks"]
    assert e["cover_source"] == "audiobook"
    assert e["cover_url"] == BASE + "Author%20Folder/Shelf%20Book.jpg"
    assert not covers.exists()  # source 1 hit -> no extraction staged


def test_coverless_epub_and_pdf_get_null_cover(build_env):
    root, out, _covers, _catalog = build_env
    make_epub(root / "Author Folder" / "book.epub", pattern="none")
    (root / "Author Folder" / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
    assert bem.build_manifest() == 0
    by_fmt = {e["format"]: e for e in _written(out)["ebooks"]}
    assert by_fmt["epub"]["cover_url"] is None and by_fmt["epub"]["cover_source"] is None
    assert by_fmt["pdf"]["cover_url"] is None and by_fmt["pdf"]["cover_source"] is None


def test_malformed_epub_degrades_to_null_never_breaks_the_build(build_env):
    root, out, _covers, _catalog = build_env
    (root / "Author Folder" / "broken.epub").write_text("not a zip", encoding="utf-8")
    assert bem.build_manifest() == 0  # step 1b's soft-fail stance is sacred
    (e,) = _written(out)["ebooks"]
    assert e["cover_url"] is None and e["cover_source"] is None


def test_dry_run_stages_no_cover_files(build_env, capsys):
    root, out, covers, _catalog = build_env
    make_epub(root / "Author Folder" / "book.epub")
    assert bem.build_manifest(dry=True) == 0
    assert not out.exists()
    assert not covers.exists()
    assert "dry run, nothing written" in capsys.readouterr().out


def test_build_survives_missing_catalog(build_env):
    root, out, _covers, catalog = build_env
    assert not catalog.exists()
    make_epub(root / "Author Folder" / "book.epub")
    assert bem.build_manifest() == 0
    (e,) = _written(out)["ebooks"]
    assert e["cover_source"] == "epub"  # fell through to source 2


def test_manifest_rows_always_carry_the_cover_keys(build_env):
    # The page and the pusher key off these fields existing, even when null.
    root, out, _covers, _catalog = build_env
    (root / "Author Folder" / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
    assert bem.build_manifest() == 0
    (e,) = _written(out)["ebooks"]
    assert "cover_url" in e and "cover_source" in e


# --------------------------------------------------------------------------- #
# Per-book anchors — the estate-search deep link (2026-08-17)
# --------------------------------------------------------------------------- #


def test_anchor_is_stable_and_id_safe():
    a = bem.ebook_anchor("Will Wight/Unsouled - Will Wight.epub")
    assert a == bem.ebook_anchor("Will Wight/Unsouled - Will Wight.epub")  # stable
    assert re.fullmatch(r"b-[0-9a-f]{12}", a), a  # never starts with a digit


def test_anchor_survives_the_characters_real_paths_carry():
    # Spaces, ampersands, colons, non-ASCII — a slug would mangle these; the
    # hash does not care, which is the whole reason it is a hash.
    for rel in (
        "James Swain/The King Tides (Lancaster & Daniels Book 1) - James Swain.epub",
        "Seirei Tsukai no Blade Dance/Seirei Tsukai — Volume 16.epub",
        "Ellen Javernick/What If Everybody Said That- (What If Everybody-).epub",
    ):
        assert re.fullmatch(r"b-[0-9a-f]{12}", bem.ebook_anchor(rel))


def test_different_paths_get_different_anchors():
    paths = [
        "A/Book.epub",
        "B/Book.epub",
        "A/Book 2.epub",
        "A/Book.pdf",
    ]
    assert len({bem.ebook_anchor(p) for p in paths}) == len(paths)


def test_every_manifest_row_carries_an_anchor(build_env):
    root, out, _covers, _catalog = build_env
    make_epub(root / "Author Folder" / "book.epub")
    (root / "Author Folder" / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
    assert bem.build_manifest() == 0
    rows = _written(out)["ebooks"]
    assert len(rows) == 2
    for e in rows:
        assert e["anchor"] == bem.ebook_anchor(e["path"])


def test_the_shipped_manifests_anchors_are_unique():
    """Two books sharing an anchor would silently swallow one another's links."""
    path = bem.PROJECT_ROOT / "site" / "ebooks.json"
    if not path.exists():
        pytest.skip("no committed manifest in this checkout")
    rows = json.loads(path.read_text(encoding="utf-8")).get("ebooks", [])
    anchors = [e.get("anchor") for e in rows]
    assert all(anchors), "every published row must carry an anchor"
    assert len(set(anchors)) == len(anchors), "anchor collision in site/ebooks.json"


# --------------------------------------------------------------------------- #
# THE COVERAGE GUARD — every published EPUB has a cover
#
# Owner, 2026-08-17, verbatim: "all epubs must resolve a cover or that breaks
# the test suite. this is so so important to me."
#
# This runs against the COMMITTED site/ebooks.json, so it gates tests.yml and
# therefore auto-promote. The same rule is enforced a second time by
# app.tools.audit_site (the promote gate) — deliberately two places, because
# they fail at different moments: this one blocks the merge, that one blocks
# the promotion of a ref that somehow got past it.
#
# PDFs are exempt BY DESIGN, not by accident: they carry no embedded art and
# the owner's decision (same day) was a "show PDFs" checkbox on the page, off
# by default, rather than a cover hunt for them.
# --------------------------------------------------------------------------- #

ALLOW_COVERLESS_ENV = "ALLOW_COVERLESS_EPUBS"


def test_every_published_epub_has_a_cover():
    import os

    manifest_path = bem.PROJECT_ROOT / "site" / "ebooks.json"
    if not manifest_path.exists():
        pytest.skip(f"{manifest_path} not present in this checkout")

    if os.environ.get(ALLOW_COVERLESS_ENV) == "1":
        pytest.skip(
            f"{ALLOW_COVERLESS_ENV}=1 — EMERGENCY ESCAPE HATCH, see docs/info/SITE_DATA.md. "
            "Unset it and fix the covers."
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    epubs = [e for e in manifest.get("ebooks", []) if (e.get("format") or "").lower() == "epub"]
    assert epubs, "site/ebooks.json lists no EPUBs at all — the manifest is broken, not clean"

    coverless = [e for e in epubs if not (e.get("cover_url") or "").strip()]
    assert not coverless, (
        f"{len(coverless)} of {len(epubs)} EPUB(s) have no cover_url — every EPUB must resolve one.\n"
        + "\n".join(f"  - {e.get('title')}  ({e.get('path')})" for e in coverless)
        + "\n\nFix, do not silence: re-run `python -m scripts.build_ebook_manifest` (an oversized "
        "cover is downscaled, never rejected), or add a hand-placed cover to "
        "scripts/ebook_cover_overrides.json. Emergency only: "
        f"{ALLOW_COVERLESS_ENV}=1."
    )


def test_the_coverage_guard_actually_fires(tmp_path, monkeypatch):
    """A guard that cannot fail is false confidence — so prove it fails.

    Runs the same assertion body against a scratch manifest with one cover
    nulled, and requires both the failure AND the offending title in the
    message (a guard that says "1 book is broken" without saying WHICH sends
    the next person hunting through 138 rows).
    """
    scratch = tmp_path / "site"
    scratch.mkdir()
    (scratch / "ebooks.json").write_text(
        json.dumps(
            {
                "ebooks": [
                    {"format": "epub", "title": "Fine Book", "path": "A/fine.epub", "cover_url": "https://x/1.jpg"},
                    {"format": "epub", "title": "Broken Book", "path": "A/broken.epub", "cover_url": None},
                    {"format": "pdf", "title": "A PDF", "path": "A/doc.pdf", "cover_url": None},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(bem, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv(ALLOW_COVERLESS_ENV, raising=False)

    with pytest.raises(AssertionError) as excinfo:
        test_every_published_epub_has_a_cover()
    message = str(excinfo.value)
    assert "Broken Book" in message and "A/broken.epub" in message
    assert "1 of 2 EPUB(s)" in message
    assert "A PDF" not in message  # PDFs are exempt by design


def test_the_escape_hatch_is_honoured(tmp_path, monkeypatch):
    scratch = tmp_path / "site"
    scratch.mkdir()
    (scratch / "ebooks.json").write_text(
        json.dumps({"ebooks": [{"format": "epub", "title": "Broken", "path": "b.epub", "cover_url": None}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(bem, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv(ALLOW_COVERLESS_ENV, "1")
    # ⚠️ pytest.skip raises Skipped, which derives from BaseException, NOT
    # Exception — `pytest.raises(Exception)` here does not catch it, it skips
    # THIS test, and the assertion below never runs. That is exactly the
    # silent no-op a guard test exists to avoid.
    with pytest.raises(pytest.skip.Exception) as excinfo:
        test_every_published_epub_has_a_cover()
    assert "EMERGENCY" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# PDF page-1 auto-covers, and THE COVER-LIKENESS GATE — source 2b (2026-08-17)
#
# ⚠️ Owner approval, verbatim: "Apply and make it automatic but we need to check
# that first page ... make sure it's an image or at least some kind of cover
# page and not just a chapter or some huge block of text."
#
# So the gate is the feature, and these tests pin BOTH directions:
#   - the four real covers he approved must pass (if the gate refuses one, the
#     GATE is wrong, not the data);
#   - a text-heavy interior page of those same PDFs, re-rendered as a fake
#     page 1, must be refused. That is the watched-failing proof — a gate that
#     only ever says yes proves nothing.
# --------------------------------------------------------------------------- #

requires_pymupdf = pytest.mark.skipif(
    __import__("importlib.util", fromlist=["util"]).find_spec("pymupdf") is None,
    reason="PyMuPDF drives the PDF page-1 auto-cover",
)


def _png(width, height, painter=None):
    """A PNG of the given size; `painter(draw, w, h)` may add content."""
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (width, height), (255, 255, 255))
    if painter is not None:
        painter(ImageDraw.Draw(im), width, height)
    buf = __import__("io").BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def make_pdf(path: Path, pages, *, title_text=None):
    """A real PDF. `pages` is a list of dicts: {text=..., image=<png bytes>}."""
    import pymupdf

    doc = pymupdf.open()
    for spec in pages:
        page = doc.new_page(width=432, height=648)  # 6x9in, a book page
        if spec.get("image") is not None:
            page.insert_image(page.rect, stream=spec["image"])
        if spec.get("text"):
            page.insert_textbox(
                pymupdf.Rect(36, 36, 396, 612), spec["text"], fontsize=9, fontname="helv"
            )
    doc.save(path)
    doc.close()
    return path


def cover_png():
    """A saturated full-bleed cover: high ink, high colour."""

    def paint(draw, w, h):
        for y in range(h):
            draw.line([(0, y), (w, y)], fill=(180, 40 + y % 60, 30))

    return _png(432, 648, paint)


def scanned_text_png():
    """A SCAN of a printed page: full-page image, no extractable text, mostly paper.

    ⚠️ The case the structural half of the gate cannot see. It is one image
    covering the whole page with zero characters — identical in shape to a real
    cover — and only the ink/colour probe tells them apart.
    """

    def paint(draw, w, h):
        for i in range(40):
            y = 40 + i * 14
            draw.line([(50, y), (w - 50, y)], fill=(20, 20, 20), width=2)

    return _png(432, 648, paint)


def verdict_for(pdf_path):
    import pymupdf

    with pymupdf.open(pdf_path) as doc:
        return bem.classify_cover_page(bem.page_cover_signals(doc[0]))


@requires_pymupdf
def test_the_pdf_gate_accepts_a_full_bleed_cover(tmp_path):
    p = make_pdf(tmp_path / "b.pdf", [{"image": cover_png()}])
    verdict, reason = verdict_for(p)
    assert verdict == "cover", reason


@requires_pymupdf
def test_the_pdf_gate_accepts_a_cover_with_its_title_on_it(tmp_path):
    # Real covers DO carry text — the Stormlight Handbook's has 102 characters
    # of title and series line. A gate that demanded zero text would refuse it.
    p = make_pdf(tmp_path / "b.pdf", [{"image": cover_png(), "text": "THE WAY OF KINGS\nBook One"}])
    verdict, reason = verdict_for(p)
    assert verdict == "cover", reason


@requires_pymupdf
def test_the_pdf_gate_refuses_a_wall_of_text(tmp_path):
    # The owner's exact worry: "not just a chapter or some huge block of text".
    p = make_pdf(tmp_path / "b.pdf", [{"text": "Chapter One. " * 200}])
    verdict, reason = verdict_for(p)
    assert verdict == "text", reason
    assert "text page" in reason


@requires_pymupdf
def test_the_pdf_gate_refuses_a_text_page_that_sits_on_a_full_page_image(tmp_path):
    # ⚠️ MEASURED on the real library: every Stormlight Handbook interior page
    # and Alloy of Law's page 2 carry a FULL-PAGE background image AND 2,000+
    # characters. Image coverage alone would wave all of them through.
    p = make_pdf(tmp_path / "b.pdf", [{"image": cover_png(), "text": "Chapter One. " * 200}])
    verdict, reason = verdict_for(p)
    assert verdict == "text", reason


@requires_pymupdf
def test_the_pdf_gate_refuses_a_title_page_with_a_small_logo(tmp_path):
    # A little decorative art and a line of text — a title or legal page. This
    # is adventuregame p1 / alloyoflaw p2 / terris p2, measured.
    def build(path):
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page(width=432, height=648)
        page.insert_image(pymupdf.Rect(190, 80, 240, 130), stream=cover_png())
        page.insert_textbox(pymupdf.Rect(36, 300, 396, 400), "A Book\nby An Author", fontsize=11)
        doc.save(path)
        doc.close()
        return path

    verdict, reason = verdict_for(build(tmp_path / "b.pdf"))
    assert verdict == "text", reason
    assert "no dominant image" in reason


@requires_pymupdf
def test_the_pdf_gate_refuses_a_scan_of_a_printed_page(tmp_path):
    # ⚠️ Image-dominant AND textless — structurally a cover. Only the pixel
    # probe (mostly paper, no colour) catches it, and with no AI rung
    # configured an ambiguous page is REFUSED rather than guessed at.
    p = make_pdf(tmp_path / "b.pdf", [{"image": scanned_text_png()}])
    verdict, reason = verdict_for(p)
    assert verdict != "cover", reason


@requires_pymupdf
def test_the_pdf_gate_refuses_a_blank_first_page(tmp_path):
    p = make_pdf(tmp_path / "b.pdf", [{}])
    verdict, _ = verdict_for(p)
    assert verdict != "cover"


@requires_pymupdf
def test_a_refused_pdf_stages_nothing_and_says_why(tmp_path):
    p = make_pdf(tmp_path / "b.pdf", [{"text": "Chapter One. " * 200}])
    covers = tmp_path / "covers" / "ebooks"
    key, reason = bem.stage_pdf_cover(p, covers)
    assert key is None
    assert "text page" in reason  # NAMED, never a bare None
    assert not covers.exists()  # and nothing written


@requires_pymupdf
def test_an_accepted_pdf_stages_a_sha256_named_jpeg_under_the_cap(tmp_path):
    p = make_pdf(tmp_path / "b.pdf", [{"image": cover_png()}])
    covers = tmp_path / "covers" / "ebooks"
    key, reason = bem.stage_pdf_cover(p, covers)
    assert key and key.startswith("ebooks/") and key.endswith(".jpg"), reason
    staged = covers / key.split("/", 1)[1]
    assert re.fullmatch(r"[0-9a-f]{64}\.jpg", staged.name)
    assert staged.stat().st_size <= bem.MAX_COVER_BYTES
    assert staged.read_bytes()[:2] == b"\xff\xd8"  # a real JPEG
    # Idempotent: same bytes, same key, no rewrite.
    first = staged.stat().st_mtime_ns
    assert bem.stage_pdf_cover(p, covers)[0] == key
    assert staged.stat().st_mtime_ns == first


@requires_pymupdf
def test_a_corrupt_pdf_is_a_named_refusal_not_a_crash(tmp_path):
    p = tmp_path / "broken.pdf"
    p.write_text("%PDF-1.4 and then nothing", encoding="utf-8")
    key, reason = bem.stage_pdf_cover(p, tmp_path / "covers")
    assert key is None and reason


def test_the_ai_rung_is_skipped_when_no_key_is_configured(monkeypatch):
    # ⚠️ Rung (b) is optional and UNCONFIGURED here. The contract is that its
    # absence refuses the ambiguous page rather than crashing or guessing.
    monkeypatch.delenv(bem.AI_COVER_KEY_ENV, raising=False)
    assert bem.ai_cover_verdict(b"\xff\xd8\xff\xe0not-really-a-jpeg") is None


# --------------------------------------------------------------------------- #
# The four the owner approved, against the REAL files
# --------------------------------------------------------------------------- #

APPROVED_PDFS = [
    "Brandon Sanderson/mistborn_adventuregame.pdf",
    "Brandon Sanderson/mistborn_alloyoflaw.pdf",
    "Brandon Sanderson/mistborn_terris_wroughtofcopper.pdf",
    "Brandon Sanderson/SL001_Stormlight_Handbook_digital.pdf",
]


@requires_pymupdf
@pytest.mark.parametrize("rel", APPROVED_PDFS)
def test_the_owner_approved_pdfs_pass_the_gate(rel):
    """⚠️ These four are real covers, personally approved 2026-08-17.

    If this fails, the GATE is wrong — do not "fix" it by editing the list.
    Skips where the library is absent (CI), like the other on-disk tests.
    """
    src = Path(bem.ROOT_DIR) / rel
    if not src.exists():
        pytest.skip(f"{src} not present (no audio library on this machine)")
    verdict, reason = verdict_for(src)
    assert verdict == "cover", f"{rel}: {reason}"


@requires_pymupdf
@pytest.mark.parametrize("rel", APPROVED_PDFS)
def test_an_interior_page_of_the_same_pdfs_is_refused(rel):
    """The watched-failing half: a text-heavy interior page as a fake page 1.

    Same file, same renderer, same gate — only the page differs. Page 5 is a
    body page in all four (1,900-4,900 characters, measured).
    """
    import pymupdf

    src = Path(bem.ROOT_DIR) / rel
    if not src.exists():
        pytest.skip(f"{src} not present (no audio library on this machine)")
    with pymupdf.open(src) as doc:
        if doc.page_count < 6:
            pytest.skip("too few pages to have an interior page")
        verdict, reason = bem.classify_cover_page(bem.page_cover_signals(doc[5]))
    assert verdict != "cover", f"{rel} p5 was accepted as a cover: {reason}"


# --------------------------------------------------------------------------- #
# The needs-a-human list, and the PDF half of the coverage guard
# --------------------------------------------------------------------------- #


def test_needs_human_cover_names_every_coverless_row_with_a_reason():
    rows = [
        {"path": "A/fine.epub", "title": "Fine", "format": "epub", "cover_url": "https://x/1.jpg"},
        {"path": "A/doc.pdf", "title": "A PDF", "format": "pdf", "cover_url": None},
        {"path": "A/blank.pdf", "title": "Blank", "format": "pdf", "cover_url": "   "},
    ]
    out = bem.build_needs_human_cover(rows, {"A/doc.pdf": "page 1 is a text page — 4000 chars"})
    assert [e["path"] for e in out] == ["A/doc.pdf", "A/blank.pdf"]
    assert out[0]["reason"].startswith("page 1 is a text page")
    assert out[0]["title"] == "A PDF" and out[0]["format"] == "pdf"
    assert out[1]["reason"] == bem.DEFAULT_COVERLESS_REASON  # never blank


def test_needs_human_cover_is_empty_when_everything_resolves():
    rows = [{"path": "A/x.pdf", "title": "X", "format": "pdf", "cover_url": "https://x/1.jpg"}]
    assert bem.build_needs_human_cover(rows) == []


def test_the_manifest_publishes_the_list_even_when_empty(build_env):
    # ⚠️ The empty list is a POSITIVE statement ("nothing waits on a person"),
    # and it is what lets the promote gate tell "no gap" from "old ref".
    root, out, _covers, _catalog = build_env
    make_epub(root / "Author Folder" / "book.epub")
    assert bem.build_manifest() == 0
    payload = _written(out)
    assert payload[bem.NEEDS_HUMAN_COVER_KEY] == []


@requires_pymupdf
def test_a_refused_pdf_reaches_the_published_list_by_name(build_env):
    root, out, _covers, _catalog = build_env
    make_pdf(root / "Author Folder" / "manual.pdf", [{"text": "Chapter One. " * 200}])
    assert bem.build_manifest() == 0
    payload = _written(out)
    (row,) = payload["ebooks"]
    assert row["cover_url"] is None and row["cover_source"] is None
    (named,) = payload[bem.NEEDS_HUMAN_COVER_KEY]
    assert named["path"] == "Author Folder/manual.pdf"
    assert "text page" in named["reason"]


@requires_pymupdf
def test_a_cover_like_pdf_is_auto_covered_as_pdf_page1(build_env):
    root, out, covers, _catalog = build_env
    make_pdf(root / "Author Folder" / "art.pdf", [{"image": cover_png()}])
    assert bem.build_manifest() == 0
    payload = _written(out)
    (row,) = payload["ebooks"]
    assert row["cover_source"] == "pdf_page1"
    assert row["cover_url"].startswith("https://covers.heygabi.ai/ebooks/")
    assert row["cover_url"].endswith(".jpg")
    assert len(list(covers.iterdir())) == 1
    assert payload[bem.NEEDS_HUMAN_COVER_KEY] == []


@requires_pymupdf
def test_a_sibling_audiobook_cover_is_never_overwritten_by_the_render(build_env):
    """⚠️ 26 of this library's 30 PDFs are covered by their sibling audiobook.

    The auto-cover path must only ever touch the coverless ones.
    """
    root, out, covers, catalog = build_env
    make_pdf(root / "Author Folder" / "Shelf Book.pdf", [{"image": cover_png()}])
    catalog.write_text(
        "title,author,cover_href\n" 'Shelf Book,A,"covers/Author Folder/Shelf Book.jpg"\n',
        encoding="utf-8",
    )
    assert bem.build_manifest() == 0
    (row,) = _written(out)["ebooks"]
    assert row["cover_source"] == "audiobook"
    assert not covers.exists()  # nothing rendered, nothing staged


@requires_pymupdf
def test_a_hand_placed_override_outranks_the_rendered_page(build_env, monkeypatch, tmp_path):
    """The reverse of the EPUB order, and deliberately so.

    An embedded EPUB cover is the publisher's own art; a rendered PDF page is a
    machine guess, and a person who placed a cover has already overruled it.
    """
    root, out, covers, _catalog = build_env
    make_pdf(root / "Author Folder" / "art.pdf", [{"image": cover_png()}])
    ov = tmp_path / "ov.json"
    ov.write_text(
        json.dumps({"covers": {"Author Folder/art.pdf": {"key": "ebooks/abc123.jpg"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(bem, "COVER_OVERRIDES_PATH", ov)
    assert bem.build_manifest() == 0
    (row,) = _written(out)["ebooks"]
    assert row["cover_source"] == "override"
    assert row["cover_url"] == BASE + "ebooks/abc123.jpg"
    assert not covers.exists()  # the render was never attempted


@requires_pymupdf
def test_dry_runs_render_nothing(build_env, capsys):
    root, out, covers, _catalog = build_env
    make_pdf(root / "Author Folder" / "art.pdf", [{"image": cover_png()}])
    assert bem.build_manifest(dry=True) == 0
    assert not out.exists() and not covers.exists()


def test_every_published_pdf_resolves_a_cover_or_is_named():
    """The published manifest's PDF half of the coverage rule.

    A PDF may legitimately have no cover — its first page can be a wall of
    text, and shipping that as cover art is exactly what the owner's likeness
    check refuses. What it may NOT be is silently coverless: every coverless
    PDF has to be named, with a reason, in `needs_human_cover`.
    """
    import os

    manifest_path = bem.PROJECT_ROOT / "site" / "ebooks.json"
    if not manifest_path.exists():
        pytest.skip(f"{manifest_path} not present in this checkout")
    if os.environ.get(ALLOW_COVERLESS_ENV) == "1":
        pytest.skip(f"{ALLOW_COVERLESS_ENV}=1 — EMERGENCY ESCAPE HATCH")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pdfs = [e for e in manifest.get("ebooks", []) if (e.get("format") or "").lower() == "pdf"]
    if not pdfs:
        pytest.skip("no PDFs in the published manifest")
    listed = {e.get("path") for e in manifest.get(bem.NEEDS_HUMAN_COVER_KEY, [])}
    unnamed = [
        e for e in pdfs if not (e.get("cover_url") or "").strip() and e.get("path") not in listed
    ]
    assert not unnamed, (
        f"{len(unnamed)} of {len(pdfs)} PDF(s) have no cover and are not named in "
        f"'{bem.NEEDS_HUMAN_COVER_KEY}':\n"
        + "\n".join(f"  - {e.get('title')}  ({e.get('path')})" for e in unnamed)
        + "\n\nRe-run `python -m scripts.build_ebook_manifest`: it auto-covers a PDF whose "
        "page 1 passes the cover-likeness gate and names every one it refuses."
    )


def test_the_pdf_coverage_guard_actually_fires(tmp_path, monkeypatch):
    """A guard that cannot fail is false confidence — so prove this one fails.

    A coverless PDF that IS named passes; the same PDF unnamed fails, and the
    message says which book.
    """
    scratch = tmp_path / "site"
    scratch.mkdir()
    naked = {"format": "pdf", "title": "Unnamed Manual", "path": "A/manual.pdf", "cover_url": None}
    listed = {"format": "pdf", "title": "Known Gap", "path": "A/known.pdf", "cover_url": None}
    covered = {"format": "pdf", "title": "Fine", "path": "A/f.pdf", "cover_url": "https://x/1.jpg"}

    def write(rows, needs):
        (scratch / "ebooks.json").write_text(
            json.dumps({"ebooks": rows, bem.NEEDS_HUMAN_COVER_KEY: needs}), encoding="utf-8"
        )

    monkeypatch.setattr(bem, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv(ALLOW_COVERLESS_ENV, raising=False)

    # Named -> passes.
    write([covered, listed], [{"path": "A/known.pdf", "reason": "page 1 is a text page"}])
    test_every_published_pdf_resolves_a_cover_or_is_named()

    # Unnamed -> fails, NAMING the offender.
    write([covered, listed, naked], [{"path": "A/known.pdf", "reason": "page 1 is a text page"}])
    with pytest.raises(AssertionError) as excinfo:
        test_every_published_pdf_resolves_a_cover_or_is_named()
    message = str(excinfo.value)
    assert "Unnamed Manual" in message and "A/manual.pdf" in message
    assert "1 of 3 PDF(s)" in message
    assert "Known Gap" not in message  # the named one is not an offender


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
