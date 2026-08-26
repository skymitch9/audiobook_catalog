# The six measured filename shapes from docs/TODO.md's 2026-08-25 table, as
# tests. Each one is a book that read `transcription failed` on the GABI
# Knowledge page while its .m4b sat on disk the whole time — the ingester was
# guessing a filename from a title.
#
# Fixture directories with the REAL filenames, the same style as
# tests/test_drive_pull.py: the point is the join, so the files are empty and
# nothing is transcribed.
from pathlib import Path

import pytest

from app.core.m4b_resolver import (
    AmbiguousBookFile,
    BookFileNotFound,
    fold_title,
    numbers_agree,
    resolve_book_file,
)


def _m4b(root: Path, name: str, sub: str = "") -> Path:
    d = root / sub if sub else root
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.m4b"
    path.write_bytes(b"")
    return path


def _resolve(root: Path, title: str, rows=None):
    return resolve_book_file(title, rows=rows or [], root=root)


# ---------------------------------------------------------------------------
# Tier 0 — the catalog's OWN index. The primary; a filename is never guessed
# when cover_href already carries the address.
# ---------------------------------------------------------------------------

def test_index_resolves_through_cover_href_not_the_title(tmp_path):
    # cover_href is "covers/<path relative to ROOT_DIR>/<file stem>.<ext>"
    # (app/metadata.py:_save_cover_for_file). The title shares NOTHING with the
    # filename here, which is the whole point of using the index.
    want = _m4b(tmp_path, "Wings of Justice - City of Light, Book 1", "Michael-Scott Earle")
    rows = [{
        "title": "City of Light: Wings of Justice",
        "author": "Michael-Scott Earle",
        "cover_href": "covers/Michael-Scott Earle/Wings of Justice - City of Light, Book 1.png",
    }]
    assert _resolve(tmp_path, "City of Light: Wings of Justice", rows) == want


def test_index_beats_a_filename_that_would_also_fold_the_same_way(tmp_path):
    # Two files fold identically; only the index can say which row is which.
    right = _m4b(tmp_path, "Everything - Full Murderhobo, Book 3", "Dakota Krout")
    _m4b(tmp_path, "Everything", "Someone Else")
    rows = [{
        "title": "Everything",
        "author": "Dakota Krout",
        "cover_href": "covers/Dakota Krout/Everything - Full Murderhobo, Book 3.png",
    }]
    assert _resolve(tmp_path, "Everything", rows) == right


def test_a_cover_href_pointing_at_nothing_falls_through_to_the_folds(tmp_path):
    # The file was renamed since the last catalog build. The index tier must
    # not swallow the lookup — it has no answer, so the fold tiers get a turn.
    want = _m4b(tmp_path, "Phoebe Berman's Gonna Lose It", "Brooke Averick")
    rows = [{
        "title": "Phoebe Berman's Gonna Lose It: A Novel",
        "author": "Brooke Averick",
        "cover_href": "covers/Brooke Averick/Some Old Name.jpg",
    }]
    assert _resolve(tmp_path, "Phoebe Berman's Gonna Lose It: A Novel", rows) == want


def test_a_dot_in_the_stem_does_not_eat_the_filename(tmp_path):
    # ⚠️ REGRESSION, measured 2026-08-26 on the live catalog. `locate_file`
    # used `base.with_suffix(ext)`, and pathlib reads an author's initials as
    # an extension: `J.L.Mullins - [Binding - 3] - Binding (Tess Irondale)`
    # became `J.L.m4b`. The row's .m4b was on disk the whole time; the index
    # answered None and the ingester fell back to guessing from the bare title
    # "Binding" — one of the 12 books logged as `transcription failed`.
    want = _m4b(tmp_path, "J.L.Mullins - [Binding - 3] - Binding (Tess Irondale)", "J.l.mullins")
    rows = [{
        "title": "Binding",
        "author": "J.l.mullins",
        "cover_href": "covers/J.l.mullins/J.L.Mullins - [Binding - 3] - Binding (Tess Irondale).png",
    }]
    assert _resolve(tmp_path, "Binding", rows) == want


def test_locate_file_s_own_title_guess_is_not_trusted(tmp_path):
    # ⚠️ book_lookup.locate_file's fallback limb accepts any stem in the
    # author's folder that startswith(title[:40]) — that is the guess this
    # module exists to replace. With no usable cover_href, tier 0 must decline
    # rather than hand back `Everything Is Fine`, and the bare-title guard then
    # refuses containment too.
    _m4b(tmp_path, "Everything Is Fine", "Dakota Krout")
    rows = [{"title": "Everything", "author": "Dakota Krout", "cover_href": ""}]
    with pytest.raises(BookFileNotFound):
        _resolve(tmp_path, "Everything", rows)


# ---------------------------------------------------------------------------
# The six measured shapes, resolved WITHOUT the index (filename tiers only) —
# the fallback has to stand on its own, because a book added since the last
# catalog build has no row yet.
# ---------------------------------------------------------------------------

def test_shape_1_author_series_prefix_and_narrator_suffix(tmp_path):
    want = _m4b(
        tmp_path,
        "Michael-Scott Earle - [Space Knight - 9] - Space Knight Book 9 "
        "(Alex Perone and Marissa Parness)",
    )
    assert _resolve(tmp_path, "Space Knight Book 9") == want


def test_shape_2_colon_became_a_dash_and_the_series_tail_was_dropped(tmp_path):
    want = _m4b(tmp_path, "Demonic Devourer- Book 2")
    _m4b(tmp_path, "Demonic Devourer- Book 3")
    _m4b(tmp_path, "Demonic Devourer - Demonic Devourer, Book 1")
    got = _resolve(tmp_path, "Demonic Devourer: Book 2: Demonic Devourer Series")
    assert got == want


def test_shape_3_marketing_subtitle_dropped(tmp_path):
    want = _m4b(tmp_path, "Phoebe Berman's Gonna Lose It")
    assert _resolve(tmp_path, "Phoebe Berman's Gonna Lose It: A Novel") == want


def test_shape_4_bare_title_meets_a_file_carrying_a_series_tail(tmp_path):
    want = _m4b(tmp_path, "Everything - Full Murderhobo, Book 3")
    assert _resolve(tmp_path, "Everything") == want


def test_shape_4_bare_title_matches_by_the_fold_not_by_substring(tmp_path):
    # ⚠️ THE GUARD. `Everything Is Fine` contains `Everything` word for word.
    # The fold makes the Murderhobo file EQUAL to the wanted title (its series
    # tail is stripped from the FILE), so tier 3 settles it and tier 4 is never
    # reached. A resolver that matched on substring would have two candidates
    # and no way to choose.
    want = _m4b(tmp_path, "Everything - Full Murderhobo, Book 3")
    _m4b(tmp_path, "Everything Is Fine")
    assert _resolve(tmp_path, "Everything") == want


def test_shape_5_halves_swapped(tmp_path):
    want = _m4b(tmp_path, "Wings of Justice - City of Light, Book 1")
    assert _resolve(tmp_path, "City of Light: Wings of Justice") == want


def test_shape_6_a_volume_never_resolves_to_its_false_twins(tmp_path):
    # ⚠️ The Space Knight lesson: a bare volume tail is IDENTITY, not
    # boilerplate. Book 5 is not on disk; neither the series-level file nor
    # book 2 may stand in for it. Transcribing the wrong book reports itself
    # as success, so a wrong answer here is worse than no answer.
    _m4b(tmp_path, "Space Knight")
    _m4b(tmp_path, "Space Knight, Book 2")
    with pytest.raises(BookFileNotFound):
        _resolve(tmp_path, "Space Knight Book 5")


def test_the_whole_shelf_at_once(tmp_path):
    # All six shapes in ONE directory, so no test passes only because its
    # fixture was conveniently empty.
    files = {
        "Space Knight Book 9":
            "Michael-Scott Earle - [Space Knight - 9] - Space Knight Book 9 "
            "(Alex Perone and Marissa Parness)",
        "Demonic Devourer: Book 2: Demonic Devourer Series": "Demonic Devourer- Book 2",
        "Phoebe Berman's Gonna Lose It: A Novel": "Phoebe Berman's Gonna Lose It",
        "Everything": "Everything - Full Murderhobo, Book 3",
        "City of Light: Wings of Justice": "Wings of Justice - City of Light, Book 1",
    }
    for extra in ("Space Knight", "Space Knight, Book 2", "Everything Is Fine",
                  "Demonic Devourer- Book 3"):
        _m4b(tmp_path, extra)
    wanted = {title: _m4b(tmp_path, stem) for title, stem in files.items()}
    for title, path in wanted.items():
        assert _resolve(tmp_path, title) == path, title
    with pytest.raises(BookFileNotFound):
        _resolve(tmp_path, "Space Knight Book 5")


# ---------------------------------------------------------------------------
# Ambiguity — refuse with names, never pick
# ---------------------------------------------------------------------------

def test_two_files_folding_the_same_way_are_refused_by_name(tmp_path):
    # Both series tails are stripped by the fold, so both files answer to
    # `Everything` and nothing in either name says which one the row meant.
    _m4b(tmp_path, "Everything - Full Murderhobo, Book 3", "Dakota Krout")
    _m4b(tmp_path, "Everything - Another Series, Book 3", "Someone Else")
    with pytest.raises(AmbiguousBookFile) as exc:
        _resolve(tmp_path, "Everything")
    message = str(exc.value)
    assert "refusing to guess" in message
    assert message.count(".m4b") == 2, "both candidates must be named"


def test_two_index_rows_pointing_at_different_files_are_refused(tmp_path):
    a = _m4b(tmp_path, "Binding", "Author A")
    b = _m4b(tmp_path, "Binding", "Author B")
    rows = [
        {"title": "Binding", "author": "Author A", "cover_href": "covers/Author A/Binding.jpg"},
        {"title": "Binding", "author": "Author B", "cover_href": "covers/Author B/Binding.jpg"},
    ]
    with pytest.raises(AmbiguousBookFile):
        _resolve(tmp_path, "Binding", rows)
    assert a.exists() and b.exists()


def test_ambiguity_is_a_file_not_found_error_so_old_callers_still_catch_it(tmp_path):
    _m4b(tmp_path, "Legion")
    _m4b(tmp_path, "Legion", "other")
    with pytest.raises(FileNotFoundError):
        _resolve(tmp_path, "Legion - The Many, Book 1")


# ---------------------------------------------------------------------------
# The rules themselves
# ---------------------------------------------------------------------------

def test_numbers_agree_is_the_containment_gate():
    # Ported rule: a containment match may differ in words, never in numbers.
    assert numbers_agree(fold_title("Space Knight Book 9"), fold_title("Space Knight Book 9"))
    assert not numbers_agree(fold_title("Space Knight Book 5"), fold_title("Space Knight"))
    assert not numbers_agree(fold_title("Demonic Devourer: Book 2"),
                             fold_title("Demonic Devourer- Book 3"))


def test_the_fold_is_the_repo_s_existing_one():
    # clean_audiobook_title then normalise_title — no third normaliser exists.
    assert fold_title("Everything - Full Murderhobo, Book 3") == "everything"
    assert fold_title("Phoebe Berman's Gonna Lose It: A Novel") == "phoebe berman s gonna lose it"
    assert fold_title("Wings of Justice - City of Light, Book 1") == "wings of justice"


def test_nothing_on_disk_says_not_found_with_the_root(tmp_path):
    _m4b(tmp_path, "Some Other Book")
    with pytest.raises(BookFileNotFound, match="no .m4b under"):
        _resolve(tmp_path, "This Book Does Not Exist - Nowhere, Book 99")
