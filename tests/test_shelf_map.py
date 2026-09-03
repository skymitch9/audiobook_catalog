"""
The shelf map generator — scripts/build_shelf_map.py.

⚠️ WHY THESE EXIST. The version shipped until 2026-09-02 wrote
`{"<slug>": "<abs-item-uuid>"}` and the site turned each uuid into an
`/audiobookshelf/item/<uuid>` deep link for 1,077 books. ABS item ids are not
stable — every id from the 2026-08-20 flat layout 404'd after the hardlink
reshape (docs/TODO.md, "Shelf link on every book", constraint 1).

Two of the tests below are regression fences rather than unit tests, and they
are the ones worth keeping:

  * `test_shipped_map_contains_no_uuids` reads the FILE THAT SHIPS and fails if
    a uuid ever reappears in it. Nothing else notices that regression: a map
    full of uuids looks fine, deploys fine, and produces links that work right
    up until the next library rebuild.
  * `test_slug_matches_the_javascript` pins the Python slug to the JS one via a
    shared fixture. If they drift, `shelfLinkFor()` returns null for every book
    and 1,082 shelf buttons vanish with no error anywhere.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from build_shelf_map import (  # noqa: E402
    book_id_from_title,
    build_books_block,
    media_kind,
)

SLUG_CASES = json.loads(
    (PROJECT_ROOT / "tests" / "fixtures" / "slug_cases.json").read_text(encoding="utf-8")
)["cases"]
SHELF_MAP = PROJECT_ROOT / "site" / "shelf_book_map.json"

#: A uuid as ABS mints them: 8-4-4-4-12 hex.
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)


# ---------------------------------------------------------------------------
# the cross-language pin
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("case", SLUG_CASES, ids=lambda c: c["slug"] or "(empty)")
def test_slug_matches_the_javascript(case):
    """
    ⚠️ The same table drives site/__tests__/shelf-link.test.js. Two languages,
    one answer. Verified 2026-09-02: 17/17 cases agree in both.
    """
    assert book_id_from_title(case["title"]) == case["slug"]


def test_slug_survives_none_and_empty():
    assert book_id_from_title("") == ""
    assert book_id_from_title(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# media_kind — read off the LIST endpoint, which has no libraryFiles
# ---------------------------------------------------------------------------
def _item(
    item_id: str,
    title: str,
    tracks: int = 0,
    author: str = "A",
    ebook: str = "",
    missing: bool = False,
) -> dict:
    item = {
        "id": item_id,
        "media": {
            "numTracks": tracks,
            "ebookFormat": ebook or None,
            "metadata": {"title": title, "authorName": author},
        },
    }
    if missing:
        item["isMissing"] = True
    return item


def test_media_kind_reads_tracks():
    assert media_kind(_item("1", "T", tracks=3)) == "audio"
    assert media_kind(_item("2", "T", tracks=0)) == "ebook"


def test_media_kind_sees_an_item_holding_audio_and_an_epub_as_both():
    """Measured 2026-09-02: 31 live items carry numTracks AND ebookFormat."""
    assert media_kind(_item("3", "T", tracks=3, ebook="epub")) == "both"
    assert media_kind(_item("4", "T", tracks=0, ebook="epub")) == "ebook"


def test_media_kind_falls_back_to_audio_files_array():
    """Some payloads carry audioFiles without numTracks."""
    it = {"id": "3", "media": {"audioFiles": [{}, {}], "metadata": {"title": "T"}}}
    assert media_kind(it) == "audio"


def test_media_kind_survives_a_junk_item():
    assert media_kind({}) == "ebook"
    assert media_kind({"media": None}) == "ebook"


# ---------------------------------------------------------------------------
# the join
# ---------------------------------------------------------------------------
def _rows(*titles: str) -> list:
    return [{"title": t, "author": "A", "series": "", "series_index_sort": ""} for t in titles]


def test_exact_match_records_the_abs_title_not_the_id():
    """⚠️ Constraint 1: the value is a TITLE to search for, never an id."""
    books, stats = build_books_block(
        _rows("Unsouled"),
        [_item("abc-123", "Unsouled", tracks=5)],
        [],
    )
    assert books == {"unsouled": {"t": "Unsouled", "m": "audio"}}
    assert "abc-123" not in json.dumps(books)
    assert stats["exact"] == 1


def test_an_unmatched_catalog_book_gets_no_entry():
    """⚠️ Constraint 3: no counterpart means no button, not a dead link."""
    books, stats = build_books_block(
        _rows("A Book Nobody Owns"),
        [_item("x", "Something Else Entirely", tracks=5)],
        [],
    )
    assert books == {}
    assert stats["unmatched"] == 1


def test_a_slug_in_both_libraries_becomes_both():
    """The one place 'both' is a real fact rather than an inference."""
    books, _ = build_books_block(
        _rows("Unsouled"),
        [_item("a", "Unsouled", tracks=5)],
        [_item("b", "Unsouled", tracks=0)],
    )
    assert books["unsouled"]["m"] == "both"


def test_same_title_twice_in_one_library_prefers_the_audio_item_and_merges_to_both():
    """
    🔴 The Emberdark bug (2026-09-02). ABS held an ebook-only item AND two audio
    editions under one title; the builder took the first item it saw and the
    card said "Read on the shelf" for a book with 1.4 GB of audio. The entry
    must come out 'both', titled from the audio item.
    """
    books, stats = build_books_block(
        _rows("Isles of the Emberdark"),
        [
            _item("epub", "Isles of the Emberdark", tracks=0, ebook="epub"),
            _item("audible", "Isles of the Emberdark", tracks=12),
            _item("kickstarter", "Isles of the Emberdark", tracks=9),
        ],
        [],
    )
    assert books["isles-of-the-emberdark"]["m"] == "both"
    assert stats["exact"] == 1  # one catalog row matched, however many items


def test_a_missing_husk_beside_the_real_item_does_not_win():
    """
    Arcane Pathfinder 4, measured 2026-09-02: a size-0 `isMissing` husk with no
    tracks sat next to the real audio item. The husk must neither set the kind
    to ebook nor promote a plain audio book to 'both'.
    """
    books, _ = build_books_block(
        _rows("Arcane Pathfinder Book 4"),
        [
            _item("husk", "Arcane Pathfinder Book 4", tracks=0, missing=True),
            _item("real", "Arcane Pathfinder Book 4", tracks=20),
        ],
        [],
    )
    assert books["arcane-pathfinder-book-4"]["m"] == "audio"


def test_same_titled_series_volumes_stay_available_to_the_fuzzy_pass():
    """
    ⚠️ ABS titles every "Space Knight" volume "Space Knight". The exact pass
    must claim ONE of them for the row that slugs to it and leave the others
    for `space-knight-book-3`; the first cut of the Emberdark fix claimed the
    whole group and 20 rows lost their button (measured 2026-09-02).
    """
    rows = [
        {"title": "Space Knight", "author": "A", "series": "Space Knight", "series_index_sort": "1"},
        {"title": "Space Knight Book 3", "author": "A", "series": "Space Knight", "series_index_sort": "3"},
    ]

    def vol(n):
        it = _item(f"sk{n}", "Space Knight", tracks=10)
        it["media"]["metadata"].update({"seriesName": f"Space Knight #{n}", "series": [{"name": "Space Knight", "sequence": str(n)}]})
        return it
    books, stats = build_books_block(rows, [vol(1), vol(3)], [])
    assert "space-knight" in books
    assert "space-knight-book-3" in books, "second volume was claimed by the exact pass"
    assert stats["exact"] == 1 and stats["fuzzy"] == 1


def test_a_lone_missing_item_still_gets_an_entry():
    """A husk still answers the shelf's title search; dropping it would hide a book the shelf lists."""
    books, _ = build_books_block(
        _rows("Gone Book"), [_item("husk", "Gone Book", tracks=5, missing=True)], []
    )
    assert "gone-book" in books


def test_ebook_only_library_entry_is_marked_ebook():
    books, _ = build_books_block(
        _rows("Unsouled"),
        [],
        [_item("b", "Unsouled", tracks=0)],
    )
    assert books["unsouled"]["m"] == "ebook"


def test_two_catalog_rows_cannot_claim_one_abs_item():
    """
    ⚠️ Without the claim set in the fuzzy pass, every book in a series collapses
    onto volume 1 — they all score alike on author and series name.
    """
    books, _ = build_books_block(
        _rows("Cradle Book 1", "Cradle Book 2"),
        [_item("only-one", "Cradle Book 1", tracks=5)],
        [],
    )
    ids_used = [v["t"] for v in books.values()]
    assert len(ids_used) == len(set(ids_used))


def test_an_item_with_no_title_is_skipped_rather_than_keyed_empty():
    books, _ = build_books_block(_rows("Unsouled"), [_item("a", "", tracks=5)], [])
    assert "" not in books


# ---------------------------------------------------------------------------
# the file that actually ships
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not SHELF_MAP.exists(), reason="shelf_book_map.json not generated here")
def test_shipped_map_contains_no_uuids():
    """
    🔴 THE REGRESSION FENCE. A map full of ABS item ids looks fine, deploys
    fine, and produces links that work until the next `02-abs-hardlinks.sh`
    run — at which point every one of them 404s. Measured 2026-08-21: that is
    exactly what happened to the previous generation of ids.
    """
    raw = SHELF_MAP.read_text(encoding="utf-8")
    found = UUID_RE.findall(raw)
    # The library ids are legitimately uuids and live at the top level; book
    # entries must contribute none.
    payload = json.loads(raw)
    allowed = {payload.get("libraryId"), payload.get("ebookLibraryId")} - {None}
    offenders = sorted({u for u in found if u not in allowed})
    assert not offenders, (
        f"{len(offenders)} ABS item id(s) in shelf_book_map.json — "
        f"item ids rot on every library rebuild; store the ABS title instead. "
        f"First few: {offenders[:3]}"
    )


@pytest.mark.skipif(not SHELF_MAP.exists(), reason="shelf_book_map.json not generated here")
def test_shipped_map_has_the_current_shape_and_a_build_stamp():
    payload = json.loads(SHELF_MAP.read_text(encoding="utf-8"))
    assert isinstance(payload.get("books"), dict) and payload["books"]
    # ⚠️ Staleness must be visible — that was the recorded alternative to
    # search links, and it is worth having even now that we use both.
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", payload["generatedAt"])
    assert payload.get("libraryId")
    for slug, entry in payload["books"].items():
        assert slug == book_id_from_title(slug), f"key {slug!r} is not a normalised slug"
        assert entry["t"], f"{slug} has no ABS title to search for"
        assert entry["m"] in ("audio", "ebook", "both")
