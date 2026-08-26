"""F5 (2026-08-24 sanctity audit, fixed 2026-08-26): STEP 1's `sort_books()`
must sort NEW ARRIVALS only, and REPORT — never relocate — an already-filed
book whose ``©ART`` tag disagrees with the folder it lives in.

The defect it closes. ``OPENAUDIBLE_BOOKS_DIR`` and ``ROOT_DIR`` are the same
path, and `sort_books` rglobs the source, so every one of ~1,080 filed books
was re-evaluated against its tag on every 8-hourly run. One un-aliased spelling
was enough to MOVE a book that had been correctly filed for months. Drive dedup
is PER-FOLDER (`check_file_exists_on_drive` looks inside the resolved author
folder and nowhere else), so after the relocation STEP 4 resolves a different
Drive folder, finds no copy of the book there, and uploads it again — the
library now holds two, and nothing in the run says so. The move printed one
`[MOVE]` line in a log nobody reads.

What these tests pin:
  * a filed book with a divergent tag STAYS PUT and is named in the report;
  * a new arrival is still sorted (the fix must not stop the sorter sorting);
  * ``--resort-all`` restores the whole-library move for a deliberate,
    attended run;
  * the alias map is honoured on the tag side, so the shelf spelling that
    already exists does not read as a mismatch;
  * the pure helpers behave at the edges (root-level file, foreign root,
    casefold).

The library is a real tmp tree; only ``get_author_name`` (which would need a
tagged m4b) is stubbed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import sync_to_drive as sync


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A tmp ROOT_DIR wired in as BOTH the library root and the OpenAudible
    export dir — which is exactly how the real machine is configured, and the
    reason the whole library was in scope in the first place."""
    root = tmp_path / "books"
    root.mkdir()
    monkeypatch.setattr(sync, "OPENAUDIBLE_BOOKS_DIR", root)
    monkeypatch.setattr(sync, "CONTAINER_BOOKS_DIR", root)

    import app.config as cfg
    monkeypatch.setattr(cfg, "ROOT_DIR", root)

    # No alias entries unless a test adds them.
    import app.author_names as an
    monkeypatch.setattr(an, "load_shelf_aliases", lambda: {})
    return root


def _tags(monkeypatch, mapping: dict[str, str]):
    """Stub the ©ART read: {filename: author}."""
    import app.author_names as an
    monkeypatch.setattr(an, "get_author_name", lambda p: mapping.get(Path(p).name))


def _book(root: Path, *parts: str) -> Path:
    p = root.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"m4b")
    return p


# ---------------------------------------------------------------------------
# The pure helpers
# ---------------------------------------------------------------------------


def test_filed_author_folder_recognises_the_three_cases(tmp_path):
    root = tmp_path / "books"
    assert sync.filed_author_folder(root / "Robert Jordan" / "Eye.m4b", root) == "Robert Jordan"
    # Directly in the library root — a loose arrival, never "filed".
    assert sync.filed_author_folder(root / "Eye.m4b", root) is None
    # From another source root entirely (the Docker container's books dir).
    assert sync.filed_author_folder(tmp_path / "container" / "Eye.m4b", root) is None
    # Nested deeper still counts as filed under its top folder.
    assert sync.filed_author_folder(
        root / "Robert Jordan" / "Wheel of Time" / "Eye.m4b", root
    ) == "Robert Jordan"


def test_filed_author_folder_survives_an_unnormalised_root(tmp_path):
    """⚠️ THE ONE THAT WOULD RELOCATE THE WHOLE LIBRARY.

    OPENAUDIBLE_BOOKS_DIR is raw ``Path(os.getenv("ROOT_DIR"))``; app.config's
    ROOT_DIR is the same value ``.resolve()``-d. They agree today. If a future
    $ROOT_DIR carried a trailing slash, a relative segment or different case,
    ``Path.relative_to`` would raise on EVERY file — every filed book would
    read as a new arrival and the sorter would move the library. relpath
    absolutises and normcases both sides, so it cannot.

    ⚠️ Measured 2026-08-26, so the claim is right-sized: ``WindowsPath
    .relative_to`` ALREADY tolerated the trailing separator and the case
    variant (they would bite on POSIX, which is case-sensitive). The one that
    genuinely broke it is an unresolved ``..`` segment — and ``$ROOT_DIR`` is
    free text in a ``.env``."""
    root = tmp_path / "books"
    (root / "Robert Jordan").mkdir(parents=True)
    book = root / "Robert Jordan" / "Eye.m4b"

    for variant in (
        Path(str(root) + "\\"),                     # trailing separator
        Path(str(root) + "/"),
        root / ".",                                 # a relative segment
        root / "Robert Jordan" / "..",              # ⚠️ the one that bit
        Path(str(root).upper()) if root.drive else root,   # case (Windows)
    ):
        assert sync.filed_author_folder(book, variant) == "Robert Jordan", variant


def test_filed_author_folder_is_none_for_a_sibling_directory(tmp_path):
    """The `..` case must stay None — a normcase comparison must not make
    everything look filed either."""
    root = tmp_path / "books"
    assert sync.filed_author_folder(tmp_path / "elsewhere" / "X.m4b", root) is None
    assert sync.filed_author_folder(root, root) is None  # the root itself


def test_tag_folder_mismatch_is_casefold_only():
    assert sync.tag_folder_mismatch("Robert Jordan", "Robert Jordamn") is True
    assert sync.tag_folder_mismatch("Robert Jordan", "robert jordan") is False
    assert sync.tag_folder_mismatch("Robert Jordan", "Robert Jordan") is False


# ---------------------------------------------------------------------------
# The behaviour that matters
# ---------------------------------------------------------------------------


def test_filed_book_with_a_divergent_tag_stays_put_and_is_reported(library, monkeypatch):
    """THE regression. Before the fix this book moved to 'Robert Jordamn/',
    which then re-uploaded it to Drive as a duplicate."""
    book = _book(library, "Robert Jordan", "The Eye of the World.m4b")
    _tags(monkeypatch, {"The Eye of the World.m4b": "Robert Jordamn"})

    mismatches: list[str] = []
    moved = sync.sort_books(dry_run=False, mismatch_out=mismatches)

    assert moved == [], "an already-filed book must not be relocated by a scheduled run"
    assert book.exists(), "the file must still be exactly where it was"
    assert not (library / "Robert Jordamn").exists(), "no second author folder"

    assert len(mismatches) == 1
    line = mismatches[0]
    assert "The Eye of the World.m4b" in line
    assert "Robert Jordamn" in line   # what the tag says
    assert "Robert Jordan" in line    # where it actually lives
    assert "left in place" in line


def test_a_new_arrival_is_still_sorted(library, monkeypatch):
    """The fix must not stop the sorter sorting — a loose file in the library
    root is what STEP 1 exists for."""
    arrival = _book(library, "Brand New Book.m4b")
    _tags(monkeypatch, {"Brand New Book.m4b": "Robert Jordan"})

    mismatches: list[str] = []
    moved = sync.sort_books(dry_run=False, mismatch_out=mismatches)

    dest = library / "Robert Jordan" / "Brand New Book.m4b"
    assert moved == [dest]
    assert dest.exists() and not arrival.exists()
    assert mismatches == []


def test_resort_all_moves_the_filed_book(library, monkeypatch):
    """The escape hatch: a human who has decided the tag is right gets the old
    whole-library behaviour back for exactly one attended run."""
    book = _book(library, "Robert Jordan", "The Eye of the World.m4b")
    _tags(monkeypatch, {"The Eye of the World.m4b": "Robert Jordamn"})

    mismatches: list[str] = []
    moved = sync.sort_books(dry_run=False, resort_all=True, mismatch_out=mismatches)

    dest = library / "Robert Jordamn" / "The Eye of the World.m4b"
    assert moved == [dest]
    assert dest.exists() and not book.exists()
    assert mismatches == [], "--resort-all moves; it does not also report"


def test_resort_all_dry_run_moves_nothing(library, monkeypatch):
    """--resort-all is a bulk move and the docs say dry-run it first, so the
    dry run had better actually be dry."""
    book = _book(library, "Robert Jordan", "The Eye of the World.m4b")
    _tags(monkeypatch, {"The Eye of the World.m4b": "Robert Jordamn"})

    moved = sync.sort_books(dry_run=True, resort_all=True)

    assert moved == [library / "Robert Jordamn" / "The Eye of the World.m4b"]
    assert book.exists(), "dry-run must not touch the disk"
    assert not (library / "Robert Jordamn").exists()


def test_the_shelf_alias_is_applied_to_the_tag_side(library, monkeypatch):
    """The comment sort_books has carried since 2026-08-09: a book tagged
    'Alex Toxic' that lives in 'Nadya Lee/' is CORRECT, not a mismatch. The
    alias resolves the tag, and the resolved name matches the folder."""
    _book(library, "Nadya Lee", "Some Book.m4b")
    _tags(monkeypatch, {"Some Book.m4b": "Alex Toxic"})

    import app.author_names as an
    monkeypatch.setattr(an, "load_shelf_aliases", lambda: {"alex toxic": "Nadya Lee"})

    mismatches: list[str] = []
    moved = sync.sort_books(dry_run=False, mismatch_out=mismatches)

    assert moved == []
    assert mismatches == [], "an aliased spelling is the map working, not a divergence"


def test_a_book_filed_under_an_alias_source_name_is_reported(library, monkeypatch):
    """The other direction, and it SHOULD report: the map says this author's
    shelf is 'Nadya Lee', and the book is still sitting in 'Alex Toxic/'. It is
    still not moved — reporting it is the point."""
    book = _book(library, "Alex Toxic", "Some Book.m4b")
    _tags(monkeypatch, {"Some Book.m4b": "Alex Toxic"})

    import app.author_names as an
    monkeypatch.setattr(an, "load_shelf_aliases", lambda: {"alex toxic": "Nadya Lee"})

    mismatches: list[str] = []
    moved = sync.sort_books(dry_run=False, mismatch_out=mismatches)

    assert moved == []
    assert book.exists()
    assert len(mismatches) == 1
    assert "via alias from 'Alex Toxic'" in mismatches[0]


def test_a_filed_book_whose_tag_agrees_is_silent(library, monkeypatch):
    """The overwhelmingly common case — ~1,080 books — must produce no moves
    and no noise, or the report is useless."""
    _book(library, "Robert Jordan", "The Eye of the World.m4b")
    _book(library, "Robert Jordan", "The Great Hunt.m4b")
    _tags(monkeypatch, {
        "The Eye of the World.m4b": "Robert Jordan",
        "The Great Hunt.m4b": "Robert Jordan",
    })

    mismatches: list[str] = []
    assert sync.sort_books(dry_run=False, mismatch_out=mismatches) == []
    assert mismatches == []


def test_a_file_with_no_author_tag_is_skipped_not_reported(library, monkeypatch):
    """No tag is a pre-existing '[SKIP] No author metadata' case, and it is not
    a tag/folder DIVERGENCE — mixing the two would bury the real signal."""
    _book(library, "Robert Jordan", "Untagged.m4b")
    _tags(monkeypatch, {})  # get_author_name returns None

    mismatches: list[str] = []
    assert sync.sort_books(dry_run=False, mismatch_out=mismatches) == []
    assert mismatches == []


def test_mismatch_out_is_optional(library, monkeypatch):
    """Callers that only want the moved list (the old signature) must keep
    working — the report is opt-in."""
    _book(library, "Robert Jordan", "Book.m4b")
    _tags(monkeypatch, {"Book.m4b": "Someone Else"})
    assert sync.sort_books(dry_run=False) == []
