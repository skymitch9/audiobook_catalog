"""
The author-portrait picker — scripts/set_author_images.py.

⚠️ WHY THESE EXIST. `docs/TODO.md` flags this function specifically: *"The
picker is a function that produces a PERSISTED choice, so it is one canonical
implementation, and changing it later is a migration, not an edit."* 356 author
portraits on the live shelf are the output of these rules. A change here is not
a refactor — it re-rolls all of them.

The stability tests are the point. The first draft of the script called
`random.choice` on every run and its docstring promised the portrait "rotates
nightly", which is exactly the failure TODO.md predicted in advance: *"a shelf
whose art changes every night reads as a bug."*
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from set_author_images import (  # noqa: E402
    cover_url_from_href,
    pick_cover_for_author,
)


def book(title, series="", index="", cover=None):
    return {
        "title": title,
        "series": series,
        "series_index_sort": str(index) if index != "" else "",
        "cover_href": cover if cover is not None else f"covers/A/{title}.jpg",
    }


# ---------------------------------------------------------------------------
# rule 1 & 2 — series first, lowest index we hold
# ---------------------------------------------------------------------------
def test_prefers_book_one_of_a_series():
    got = pick_cover_for_author("Will Wight", [
        book("Soulsmith", "Cradle", 2),
        book("Unsouled", "Cradle", 1),
        book("Blackflame", "Cradle", 3),
    ])
    assert "Unsouled" in got


def test_takes_the_lowest_index_held_when_book_one_is_absent():
    """⚠️ A #3 with #1 and #2 missing still wins — the point is a recognisable
    entry point, not an arbitrary volume."""
    got = pick_cover_for_author("Some Author", [
        book("Volume Five", "Series", 5),
        book("Volume Three", "Series", 3),
    ])
    assert "Volume%20Three" in got


def test_a_series_book_beats_a_standalone():
    got = pick_cover_for_author("Mixed Author", [
        book("A Standalone"),
        book("Series Opener", "Series", 1),
    ])
    assert "Series%20Opener" in got


def test_a_series_entry_with_an_unparseable_index_is_treated_as_standalone():
    """It has no position, so it cannot answer "lowest number we hold"."""
    got = pick_cover_for_author("Odd Author", [
        book("Numbered", "Series", 2),
        book("Unnumbered", "Series", "n/a"),
    ])
    assert "Numbered" in got


def test_fractional_indexes_sort_numerically_not_as_strings():
    """⚠️ "10" < "9" as strings. A novella at 1.5 must not outrank book 1."""
    got = pick_cover_for_author("Author", [
        book("Book Ten", "S", 10),
        book("Book Nine", "S", 9),
    ])
    assert "Book%20Nine" in got

    got2 = pick_cover_for_author("Author", [
        book("Novella", "S", 1.5),
        book("Book One", "S", 1),
    ])
    assert "Book%20One" in got2


# ---------------------------------------------------------------------------
# rule 3 — the standalone-only author, named rather than left to chance
# ---------------------------------------------------------------------------
def test_a_standalone_only_author_still_gets_a_cover():
    got = pick_cover_for_author("Standalone Sam", [book("One"), book("Two")])
    assert got  # not empty — falling through all three rules is not an option


def test_an_author_with_no_covers_gets_nothing_rather_than_a_broken_url():
    assert pick_cover_for_author("Empty", [book("T", cover="")]) == ""
    assert pick_cover_for_author("Empty", []) == ""


# ---------------------------------------------------------------------------
# ⚠️ STABILITY — the whole reason this file exists
# ---------------------------------------------------------------------------
def test_the_pick_is_identical_across_runs():
    """
    ⚠️ THE REGRESSION FENCE. The first draft re-rolled `random.choice` every
    run. Calling twice must give one answer, or 356 portraits churn nightly.
    """
    books = [book(f"Book {i}") for i in range(20)]
    first = pick_cover_for_author("Standalone Sam", books)
    for _ in range(25):
        assert pick_cover_for_author("Standalone Sam", books) == first


def test_the_pick_does_not_depend_on_the_input_order():
    """A catalogue re-sort must not silently re-roll every portrait."""
    books = [book(f"Book {i}") for i in range(20)]
    assert (pick_cover_for_author("Sam", books)
            == pick_cover_for_author("Sam", list(reversed(books))))


def test_different_authors_get_different_picks():
    """Seeded per author, so the choice is arbitrary rather than always first."""
    books = [book(f"Book {i}") for i in range(30)]
    picks = {pick_cover_for_author(f"Author {n}", books) for n in range(12)}
    assert len(picks) > 1, "every author chose the same book — the seed is not being used"


def test_a_series_tie_is_broken_by_title_not_by_luck():
    """Two series at #1 (an author with two series) must not swap between runs."""
    books = [
        book("Zebra Book", "Series Z", 1),
        book("Alpha Book", "Series A", 1),
    ]
    first = pick_cover_for_author("Two Series", books)
    assert "Alpha%20Book" in first
    assert pick_cover_for_author("Two Series", list(reversed(books))) == first


# ---------------------------------------------------------------------------
# cover URLs — ABS fetches these ITSELF, server-side
# ---------------------------------------------------------------------------
def test_cover_url_strips_the_covers_prefix_and_encodes_the_rest():
    url = cover_url_from_href("covers/A. American/Going Home.jpg")
    assert "/covers/" not in url
    assert "A.%20American" in url
    assert url.startswith("https://")
    assert " " not in url


def test_cover_url_is_empty_for_an_empty_href():
    assert cover_url_from_href("") == ""
    assert cover_url_from_href(None) == ""


@pytest.mark.parametrize("ch,enc", [("#", "%23"), ("?", "%3F"), ("&", "%26")])
def test_cover_url_encodes_characters_that_would_truncate_it(ch, enc):
    """⚠️ ABS fetches this URL server-side; a bare # or ? silently truncates it
    and the author quietly keeps the silhouette."""
    assert enc in cover_url_from_href(f"covers/A/Title{ch}x.jpg")
