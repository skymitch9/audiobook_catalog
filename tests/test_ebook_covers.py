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


def test_oversized_cover_is_skipped(tmp_path):
    big = b"\xff" * (bem.MAX_COVER_BYTES + 1)
    p = make_epub(tmp_path / "b.epub", cover_bytes=big)
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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
