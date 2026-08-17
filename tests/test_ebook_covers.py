# tests/test_ebook_covers.py
"""
Covers in the ebook manifest (scripts/build_ebook_manifest.py) — the
bookshelf redesign's pipeline half.

Three sources, in the approved order, and the tests pin the ORDER as much as
the mechanics:

  1. sibling audiobook cover (join against site/catalog.csv, conservative —
     a wrong cover is worse than a placeholder);
  2. the image inside the EPUB itself (OPF cover-image entry), staged
     sha256-named under site/covers/ebooks/ for the existing R2 upload step;
  3. null -> the page's typographic spine placeholder.

⚠️ The join's conservatism is the point of half these tests: the "Tamer:
King of Dinosaurs Book 10 must NOT wear book 1's cover" cases are measured
against the real library, not hypothetical.
"""

import json
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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
