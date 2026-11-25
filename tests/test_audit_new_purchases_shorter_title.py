"""A SHORTER catalog title inside a purchase's title is not proof of ownership.

Found 2026-11-25: "Portal to Nova Roma: Empire" (book 5, bought 2026-11-24)
never downloaded because the catalog's "Portal to Nova Roma" (book 1) sat
inside its title as whole words. Over all 1,139 purchases the same rule hid
eight books, every one of them a shorter catalog title by another author or at
another series position. See app/tools/audit_new_purchases.py
``_shorter_catalog_title_is_this_book``.
"""
from app.tools.audit_new_purchases import missing_purchases, norm


def _catalog(*rows):
    """(title, author, series_index) rows -> the dict load_catalog_titles() returns."""
    out = {}
    for title, author, index in rows:
        out.setdefault(norm(title), []).append({"author": author, "index": index})
    return out


def _missing(purchases, catalog):
    books = [dict(p, purchase_date=f"2026-11-{i + 1:02d}") for i, p in enumerate(purchases)]
    return [t for _, t in missing_purchases(books, catalog, top=100)]


NOVA_1 = ("Portal to Nova Roma", "J.R. Mathews", 1.0)


def test_book_5_is_not_owned_because_book_1_is_a_prefix_of_its_title():
    book5 = {"title": "Portal to Nova Roma: Empire", "author": "J.R. Mathews", "series_sequence": "5"}
    assert _missing([book5], _catalog(NOVA_1)) == ["Portal to Nova Roma: Empire"]


def test_a_one_word_catalog_title_by_another_author_does_not_claim_the_purchase():
    cat = _catalog(("You", "Caroline Kepnes", 1.0), ("Mage", "J.L.Mullins", 2.0), ("Prey", "Michael Crichton", None))
    buys = [
        {"title": "Fall into You", "author": "Caroline Frank", "series_sequence": "1"},
        {"title": "Mage Tank", "author": "Cornman", "series_sequence": "1"},
        {"title": "Prey for Me", "author": "Allie Oleander", "series_sequence": ""},
    ]
    assert sorted(_missing(buys, cat)) == ["Fall into You", "Mage Tank", "Prey for Me"]


def test_a_decorated_edition_by_the_same_author_with_no_series_is_still_owned():
    cat = _catalog(("Where the Crawdads Sing", "Delia Owens", None))
    buy = {"title": "Where the Crawdads Sing: Reese's Book Club", "author": "Delia Owens", "series_sequence": ""}
    assert _missing([buy], cat) == []


def test_same_author_same_position_is_owned_even_with_a_longer_purchase_title():
    cat = _catalog(("Bog Standard Isekai: Scarred", "Jonathan Brooks", 2.0))
    buy = {"title": "Bog Standard Isekai: Scarred - A LitRPG Isekai Adventure",
           "author": "Jonathan Brooks", "series_sequence": "2"}
    assert _missing([buy], cat) == []


def test_any_one_agreeing_row_with_the_title_is_enough():
    # Two catalog rows share the short title; the second is the purchased book.
    cat = _catalog(("Empire", "Someone Else", 1.0), ("Empire", "J.R. Mathews", 5.0))
    buy = {"title": "Empire: Portal to Nova Roma", "author": "J.R. Mathews", "series_sequence": "5"}
    assert _missing([buy], cat) == []


def test_a_box_set_range_is_not_a_position_so_only_the_author_decides():
    cat = _catalog(("Titan Mage", "Edie Skye", 1.0))
    buy = {"title": "Titan Mage Box Set", "author": "Edie Skye", "series_sequence": "1-5"}
    assert _missing([buy], cat) == []


def test_a_LONGER_catalog_title_is_untouched_by_the_guard():
    # The catalog adds "Book 1, Unabridged"; a different author string must not veto it.
    cat = _catalog(("Awakening Horde: Shieldwall Academy, Book 1 (Unabridged)", "", 1.0))
    buy = {"title": "Awakening Horde", "author": "Ryan Kirk", "series_sequence": "1"}
    assert _missing([buy], cat) == []


def test_a_plain_set_catalog_keeps_the_old_whole_word_rule():
    # Older callers pass a set of titles with no metadata: nothing to check, old verdict.
    book5 = {"title": "Portal to Nova Roma: Empire", "author": "J.R. Mathews", "series_sequence": "5"}
    assert _missing([book5], {norm("Portal to Nova Roma")}) == []
