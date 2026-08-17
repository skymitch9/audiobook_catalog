# tests/test_ebook_warning_keys.py
"""
The IDENTITY half of the ebook sibling join — `audiobook_title`.

⚠️ What this protects, and why it cannot be caught in a browser. The estate's
reader content notes live in one Firestore collection keyed by
`bookIdFromTitle(title)`, where *title* is **the audiobook catalog's own
spelling**. An ebook's title comes from epub metadata and is a different
spelling of the same book. Key a note on the ebook's spelling and:

  * the note is filed where nobody reads, and
  * none of the notes written elsewhere are found,

both silently, and both looking exactly like "nobody has added one yet". There
is no error to notice, which is why the rule gets tests instead of a comment.
`library_catalog/docs/info/content-warnings.md` §2 is the long version; it
measured 27 of 92 shared books producing a DIFFERENT key from the two titles.

So `scripts/build_ebook_manifest.scan()` publishes `audiobook_title` — the raw
catalog title of the audiobook this ebook sits beside, or `None` — and
`site/ebook-notes.js` keys on it, falling back to the ebook's own title only
when there is no sibling (that IS this catalog's spelling for such a file).

⚠️ It is the SAME join the cover uses (`sibling_catalog_match`), deliberately:
one implementation of "which audiobook is this ebook?", so a cover and a
content note can never disagree. The conservatism tests live next door in
tests/test_ebook_covers.py; these pin the title that travels.
"""

from pathlib import Path

import scripts.build_ebook_manifest as bem
from tests.test_ebook_covers import folder_index


# --------------------------------------------------------------------------- #
# sibling_catalog_match — the title that travels
# --------------------------------------------------------------------------- #
def test_exact_match_returns_the_catalog_spelling_not_the_ebook_one():
    # The divergence this whole feature exists for: same book, two spellings.
    idx = folder_index(
        ("Suzanne Collins", "Sunrise on the Reaping", "sunrise.jpg"),
    )
    match = bem.sibling_catalog_match("sunrise on the reaping!", "Suzanne Collins", idx)
    assert match == ("covers/Suzanne Collins/sunrise.jpg", "Sunrise on the Reaping")


def test_subtitle_extension_hands_back_the_FULL_catalog_title():
    # ⚠️ The load-bearing case. The ebook is "Moonfall"; the audiobook catalog
    # calls it "Moonfall - Beneath the Dragoneye Moons, Book 13", and THAT is
    # the string every warning for this book is keyed by. Returning the ebook's
    # own title here would be the silo the feature exists to prevent.
    idx = folder_index(
        ("Selkie Myrtle", "Moonfall - Beneath the Dragoneye Moons, Book 13", "moonfall.jpg"),
        ("Selkie Myrtle", "Rise from the Ashes - Beneath the Dragoneye Moons, Book 15", "rise.jpg"),
    )
    match = bem.sibling_catalog_match("Moonfall", "Selkie Myrtle", idx)
    assert match is not None
    assert match[1] == "Moonfall - Beneath the Dragoneye Moons, Book 13"
    assert match[1] != "Moonfall"


def test_no_sibling_means_no_audiobook_title():
    idx = folder_index(("X", "Book", "b.jpg"))
    assert bem.sibling_catalog_match("Book", None, idx) is None       # loose in root
    assert bem.sibling_catalog_match("Book", "Y", idx) is None        # unknown folder
    assert bem.sibling_catalog_match("Unrelated", "X", idx) is None   # no title match


def test_an_ambiguous_join_yields_no_title_rather_than_a_guess():
    # Two different books with the same title in one folder. A guessed identity
    # files somebody's note on the wrong book — worse than no note at all.
    idx = folder_index(("X", "Same Title", "one.jpg"), ("X", "Same Title", "two.jpg"))
    assert bem.sibling_catalog_match("Same Title", "X", idx) is None


def test_duplicate_rows_naming_one_cover_are_one_book_and_pick_deterministically():
    idx = folder_index(("X", "Same Title", "one.jpg"), ("X", "Same, Title", "one.jpg"))
    first = bem.sibling_catalog_match("Same Title", "X", idx)
    second = bem.sibling_catalog_match("Same Title", "X", idx)
    assert first is not None and first == second  # never swaps between rebuilds


def test_reverse_extension_gives_no_title_either():
    # The Tamer case: the ebook is "… Book 10", the catalog row is book 1.
    # Matching would key book 10's notes onto book 1.
    idx = folder_index(("Michael-Scott Earle", "Tamer: King of Dinosaurs", "tamer1.jpg"))
    assert bem.sibling_catalog_match("Tamer: King of Dinosaurs Book 10", "Michael-Scott Earle", idx) is None


def test_cover_href_and_catalog_title_come_from_the_SAME_row():
    # One join, two answers — the property that keeps a cover and a content
    # note from ever pointing at different audiobooks.
    idx = folder_index(
        ("X", "Dungeon Crawler Carl", "dcc1.jpg"),
        ("X", "Dungeon Crawler Carl's Christmas Special", "xmas.jpg"),
    )
    match = bem.sibling_catalog_match("Dungeon Crawler Carl", "X", idx)
    assert match == ("covers/X/dcc1.jpg", "Dungeon Crawler Carl")
    assert bem.sibling_cover_href("Dungeon Crawler Carl", "X", idx) == match[0]


# --------------------------------------------------------------------------- #
# scan() — the field actually reaches the manifest
# --------------------------------------------------------------------------- #
def _touch(root: Path, rel: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"not really an epub")


def test_scan_publishes_audiobook_title_for_a_sibling_and_null_without_one(tmp_path):
    root = tmp_path / "books"
    _touch(root, "Selkie Myrtle/Moonfall.epub")   # has an audiobook sibling
    _touch(root, "Loose Book - Some Author.epub")  # ebook-only, loose in root

    idx = folder_index(
        ("Selkie Myrtle", "Moonfall - Beneath the Dragoneye Moons, Book 13", "moonfall.jpg"),
    )
    rows = {e["title"]: e for e in bem.scan(root, catalog_covers=idx, extract=False)}

    assert rows["Moonfall"]["audiobook_title"] == "Moonfall - Beneath the Dragoneye Moons, Book 13"
    # ⚠️ null, not the ebook's own title. The FALLBACK is the consumer's
    # decision (site/ebook-notes.js warningTitleFor), because only the consumer
    # can say it in words — "no audiobook sibling, keyed by its own title".
    # Baking the fallback in here would erase the distinction from the data.
    assert rows["Loose Book"]["audiobook_title"] is None


def test_every_scanned_row_carries_the_key_even_when_it_is_null(tmp_path):
    # A consumer that must branch on `'audiobook_title' in row` is a consumer
    # that will get it wrong once. The key is always present.
    root = tmp_path / "books"
    _touch(root, "A/one.epub")
    _touch(root, "two.pdf")
    for e in bem.scan(root, catalog_covers={}, extract=False):
        assert "audiobook_title" in e
