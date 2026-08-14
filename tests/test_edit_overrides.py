"""Tests for the corrections EDITOR (app/core/overrides_store.py + app/tools/edit_overrides.py).

tests/test_catalog_overrides.py pins how corrections are READ. These pin how
they are WRITTEN, and the point of nearly every one of them is the same: the
editor must be incapable of producing a file that test_catalog_overrides.py
would reject, or an entry that validates but never fires.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core import book_lookup as bl
from app.core import catalog_overrides as co
from app.core import overrides_store as store
from app.tools import edit_overrides as cli

REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_JSON = REPO_ROOT / "scripts" / "catalog_overrides.json"


@pytest.fixture(autouse=True)
def _restore_default_overrides():
    yield
    co.reload_overrides()


@pytest.fixture()
def sandbox(tmp_path: Path) -> Path:
    """A minimal but valid corrections file, away from the real one."""
    path = tmp_path / "catalog_overrides.json"
    path.write_text(
        json.dumps(
            {
                "canonical_series": {"completionist chronicles": "The Completionist Chronicles"},
                "overrides": [
                    {
                        "match": {"author": "Dakota Krout", "title": "Implode"},
                        "set": {"series": "The Completionist Chronicles"},
                        "added": "2026-08-11",
                        "evidence": {
                            "series": "album tag held it",
                            "tags_read": {"\xa9alb": "The Completionist Chronicles"},
                            "filename_said": "Implode",
                            "sources": ["https://example.invalid/cc"],
                        },
                    }
                ],
                "_unresolved": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def _entry(**kw):
    kw.setdefault("match", {"author": "A", "title": "T"})
    kw.setdefault("sets", {"series": "S"})
    kw.setdefault("why", {"series": "because the album tag says so"})
    return store.build_entry(**kw)


# --------------------------------------------------------------------------- #
# The live file passes the editor's own validator
# --------------------------------------------------------------------------- #


def test_the_real_corrections_file_has_no_errors():
    """The validator and the test suite must agree about the shipped file."""
    problems = [p for p in store.validate(store.load(LIVE_JSON)) if p.startswith("ERROR")]
    assert problems == []


def test_round_trip_does_not_reformat_the_real_file():
    """A one-field edit must not show up as a 1000-line diff."""
    raw = LIVE_JSON.read_text(encoding="utf-8")
    assert store.dumps(json.loads(raw)) == raw


# --------------------------------------------------------------------------- #
# Evidence and keying cannot be skipped
# --------------------------------------------------------------------------- #


def test_a_corrected_field_without_a_reason_is_refused():
    with pytest.raises(store.OverridesError, match="evidence"):
        store.build_entry(match={"author": "A", "title": "T"}, sets={"series": "S", "year": "2019"}, why={"series": "x"})


def test_blank_reason_is_not_a_reason():
    with pytest.raises(store.OverridesError):
        store.build_entry(match={"author": "A", "title": "T"}, sets={"series": "S"}, why={"series": "   "})


def test_a_field_the_layer_cannot_correct_is_refused():
    with pytest.raises(store.OverridesError, match="not correctable"):
        store.build_entry(match={"author": "A", "title": "T"}, sets={"desc": "no"}, why={"desc": "no"})


def test_a_filename_alone_is_not_a_key():
    with pytest.raises(store.OverridesError, match="ASIN"):
        store.build_match(file="something.m4b")
    with pytest.raises(store.OverridesError):
        store.build_match(title="Only A Title")


def test_an_asin_is_used_alone_because_every_match_field_must_match():
    """Adding the title back to an ASIN key breaks it on the first retitle."""
    assert store.build_match(asin="B01GEWQL0K", title="T", author="A", file="f.m4b") == {"asin": "B01GEWQL0K"}


def test_file_is_kept_only_as_a_tiebreaker_next_to_a_real_key():
    assert store.build_match(title="Twin", author="A", file="twin-b.m4b") == {
        "author": "A",
        "title": "Twin",
        "file": "twin-b.m4b",
    }


def test_the_entry_carries_a_human_label_so_an_asin_key_stays_readable():
    entry = _entry(match={"asin": "B01"}, book="Some Book - Some Author")
    assert entry["book"] == "Some Book - Some Author"
    assert "title" not in entry["match"], "the label must not leak back into the key"
    assert store.describe(entry) == "Some Book - Some Author"


def test_a_built_entry_satisfies_the_reader_side_tests():
    entry = _entry(sets={"series": "S", "series_index": ""}, why={"series": "a", "series_index": "b"})
    assert entry["added"]
    for field in entry["set"]:
        assert entry["evidence"].get(field)
    assert entry["set"]["series_index"] == "", "an unknown volume is recorded blank, never guessed"


# --------------------------------------------------------------------------- #
# save() is the gate
# --------------------------------------------------------------------------- #


def test_save_refuses_to_write_an_invalid_file(sandbox):
    data = store.load(sandbox)
    data["overrides"].append({"match": {"file": "x.m4b"}, "set": {"series": "S"}, "added": "2026-01-01", "evidence": {"series": "y"}})
    before = sandbox.read_text(encoding="utf-8")
    with pytest.raises(store.OverridesError):
        store.save(data, sandbox)
    assert sandbox.read_text(encoding="utf-8") == before, "a refused write must leave the file untouched"


def test_save_refuses_an_entry_whose_evidence_does_not_cover_its_set(sandbox):
    data = store.load(sandbox)
    data["overrides"][0]["set"]["year"] = "2019"
    with pytest.raises(store.OverridesError, match="year"):
        store.save(data, sandbox)


def test_load_refuses_a_malformed_file_instead_of_silently_emptying_it(tmp_path):
    """catalog_overrides treats broken JSON as 'no corrections'. The editor must not:
    loading it as empty and saving would delete every correction in the library."""
    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    with pytest.raises(store.OverridesError):
        store.load(broken)


def test_save_keeps_the_files_existing_line_endings(sandbox):
    """git here runs autocrlf=true; rewriting CRLF as LF is a diff-less 'modified' file."""
    crlf = sandbox.read_text(encoding="utf-8").replace("\n", "\r\n")
    sandbox.write_bytes(crlf.encode("utf-8"))
    store.save(store.load(sandbox), sandbox)
    assert b"\r\n" in sandbox.read_bytes()

    sandbox.write_bytes(store.dumps(store.load(sandbox)).encode("utf-8"))
    store.save(store.load(sandbox), sandbox)
    assert b"\r\n" not in sandbox.read_bytes()


def test_validate_catches_a_duplicate_match_block(sandbox):
    data = store.load(sandbox)
    data["overrides"].append(json.loads(json.dumps(data["overrides"][0])))
    assert any("can never fire" in p for p in store.validate(data))


def test_validate_catches_an_unresolved_record_keyed_on_the_wrong_field(sandbox):
    """_unresolved keys on 'item'. tests/test_catalog_overrides.py reads u["item"]."""
    data = store.load(sandbox)
    data["_unresolved"].append({"subject": "Invent Short Story", "question": "7.5 or a dupe?"})
    assert any("'item'" in p for p in store.validate(data))


def test_validate_catches_a_non_lowercase_canonical_series_key(sandbox):
    data = store.load(sandbox)
    data["canonical_series"]["Lions Quest"] = "Lion's Quest"
    assert any("lowercased" in p for p in store.validate(data))


# --------------------------------------------------------------------------- #
# Amending an entry
# --------------------------------------------------------------------------- #


def test_amending_one_field_keeps_the_other_corrections_and_their_evidence(sandbox):
    data = store.load(sandbox)
    action = store.upsert(
        data,
        _entry(
            match={"author": "Dakota Krout", "title": "Implode"},
            sets={"series_index": "8"},
            why={"series_index": "trkn=8"},
            sources=["https://example.invalid/goodreads"],
        ),
    )
    store.save(data, sandbox)
    entry = store.load(sandbox)["overrides"][0]
    assert action == "updated"
    assert entry["set"] == {"series": "The Completionist Chronicles", "series_index": "8"}
    assert entry["evidence"]["series"] == "album tag held it", "the earlier reason must survive"
    assert entry["evidence"]["sources"] == [
        "https://example.invalid/cc",
        "https://example.invalid/goodreads",
    ], "sources accumulate; research is never dropped by a later edit"
    assert entry["added"] == "2026-08-11" and entry["updated"], "the original date is kept, the amendment dated"


def test_evidence_for_a_dropped_field_goes_with_it(sandbox):
    data = store.load(sandbox)
    store.upsert(data, _entry(match={"author": "Dakota Krout", "title": "Implode"}, sets={"year": "2022"}, why={"year": "©day"}), merge=False)
    store.save(data, sandbox)
    entry = store.load(sandbox)["overrides"][0]
    assert entry["set"] == {"year": "2022"}
    assert "series" not in entry["evidence"]


def test_removing_an_entry(sandbox):
    data = store.load(sandbox)
    store.remove(data, 0)
    data["overrides"].append(_entry())  # the file may not be left empty-but-valid by accident
    store.save(data, sandbox)
    assert len(store.load(sandbox)["overrides"]) == 1


# --------------------------------------------------------------------------- #
# simulate(): the check that an entry actually fires
# --------------------------------------------------------------------------- #


def test_simulate_runs_the_real_layer_and_puts_it_back(sandbox):
    data = store.load(sandbox)
    out = store.simulate(data, {"title": "Implode", "author": "Dakota Krout", "series": "", "series_index": ""})
    assert out["series"] == "The Completionist Chronicles"
    # The live layer must be exactly as it was, or the next build in this
    # process would use the sandbox.
    assert co.apply_overrides({"title": "Tenacity", "author": "Dakota Krout"})["series_index"] == "9"


def test_simulate_shows_an_entry_keyed_on_the_CORRECTED_title_never_firing(sandbox):
    """The failure mode the editor exists to prevent.

    An entry retitles a book; a second entry is then keyed on the NEW title,
    which is what the catalog and the site show. The layer matches on
    pre-correction values, so the second entry can never match anything.
    """
    data = store.load(sandbox)
    data["overrides"] = [
        {
            "match": {"author": "A", "title": "raw tag title"},
            "set": {"title": "Published Title"},
            "added": "2026-01-01",
            "evidence": {"title": "the tag is a mess"},
        },
        {
            "match": {"author": "A", "title": "Published Title"},
            "set": {"series_index": "3"},
            "added": "2026-01-01",
            "evidence": {"series_index": "wrong key, right intention"},
        },
    ]
    out = store.simulate(data, {"title": "raw tag title", "author": "A", "series": "", "series_index": ""})
    assert out["title"] == "Published Title"
    assert out["series_index"] == "", "keyed on the published title, so it never fires"


def test_canonical_series_also_folds_the_canonical_spelling_onto_itself(sandbox):
    data = store.load(sandbox)
    store.set_canonical_series(data, "Lions Quest", "Lion's Quest")
    assert data["canonical_series"]["lions quest"] == "Lion's Quest"
    assert data["canonical_series"]["lion's quest"] == "Lion's Quest"


def test_add_unresolved_keys_on_item_and_replaces_by_item(sandbox):
    data = store.load(sandbox)
    store.add_unresolved(data, item="Invent Short Story.epub", question="7.5 or a dupe of 7?")
    store.add_unresolved(data, item="invent short story.epub", question="asked again")
    store.save(data, sandbox)
    unresolved = store.load(sandbox)["_unresolved"]
    assert len(unresolved) == 1
    assert unresolved[0]["item"] == "invent short story.epub"
    assert unresolved[0]["status"].startswith("UNRESOLVED")


# --------------------------------------------------------------------------- #
# The CLI shell
# --------------------------------------------------------------------------- #


def test_cli_edit_writes_a_verified_entry(sandbox, tmp_path, monkeypatch, capsys):
    """End to end, with a fake book so no real m4b is opened."""
    book = bl.Book(
        row={"title": "Tag Title", "author": "A. Author", "narrator": "", "year": "", "genre": "", "series": "", "series_index_display": "", "cover_href": ""},
        path=tmp_path / "Tag Title.m4b",
        uncorrected={"title": "Tag Title", "author": "A. Author", "narrator": "", "year": "", "genre": "", "series": "", "series_index": ""},
        asin=None,
        tags_read={"\xa9nam": "Tag Title", "SRNM": "absent"},
    )
    monkeypatch.setattr(cli, "_resolve", lambda *a, **k: book)
    monkeypatch.setattr(cli, "_duplicate_titles", lambda b: 1)

    rc = cli.main(
        [
            "--overrides", str(sandbox), "edit", "tag title",
            "--set", "series=Some Series",
            "--set", "series_index=-",
            "--why", "series=the album tag holds it",
            "--why", "series_index=no source gives a volume, so it stays blank",
            "--yes",
        ]
    )
    assert rc == 0
    assert "verified" in capsys.readouterr().out

    written = [e for e in store.load(sandbox)["overrides"] if e["match"].get("title") == "Tag Title"]
    assert len(written) == 1
    assert written[0]["set"] == {"series": "Some Series", "series_index": ""}
    assert written[0]["evidence"]["tags_read"]["SRNM"] == "absent"


# --------------------------------------------------------------------------- #
# Phase A2: the key-move warning (edit-audit-design.md sec 3.4/6)
# --------------------------------------------------------------------------- #


def _plain_book(title="Tag Title", author="A. Author", **extra):
    row = {
        "title": title, "author": author, "narrator": "", "year": "", "genre": "",
        "series": "", "series_index_display": "", "cover_href": "",
    }
    row.update(extra)
    return bl.Book(row=row, path=None, uncorrected=None, asin=None, tags_read={})


def test_key_move_is_none_when_title_and_author_are_untouched():
    assert cli._key_move(_plain_book(), {"series": "S"}) is None


def test_key_move_is_none_when_the_new_value_folds_to_the_same_key():
    """'THE Tag Title' vs 'Tag Title' normalise to the identical key - not a move."""
    assert cli._key_move(_plain_book(), {"title": "THE Tag Title"}) is None


def test_key_move_detects_a_title_change(monkeypatch):
    # Every test here mocks the network read - see review_join module tests
    # for count_reviews_for_book_id's own behaviour (including a real
    # failure->None degrade). This test only pins _key_move's OWN logic.
    monkeypatch.setattr(cli.review_join, "count_reviews_for_book_id", lambda *a, **k: None)
    move = cli._key_move(_plain_book(), {"title": "A Completely Different Title"})
    assert move is not None
    old_key, new_key, count = move
    # normalise_title strips a leading article - "A. Author" -> "a author" ->
    # the standalone "a" reads as the indefinite article and is stripped too.
    # Faithful to titles.ts::normaliseTitle, applied identically to authors.
    assert old_key == "tag title|author"
    assert new_key == "completely different title|author"
    assert count is None, "a failed/unmocked read must degrade to 'unknowable', never raise"


def test_key_move_detects_an_author_change(monkeypatch):
    monkeypatch.setattr(cli.review_join, "count_reviews_for_book_id", lambda *a, **k: None)
    move = cli._key_move(_plain_book(), {"author": "A Totally Different Person"})
    assert move is not None
    old_key, new_key, _ = move
    assert old_key.split("|")[1] == "author"
    assert new_key.split("|")[1] == "totally different person"


def test_cli_refuses_a_key_moving_edit_without_the_confirm_flag(sandbox, monkeypatch, capsys):
    book = bl.Book(
        row={"title": "Tag Title", "author": "A. Author", "narrator": "", "year": "", "genre": "", "series": "", "series_index_display": "", "cover_href": ""},
        path=None, uncorrected=None, asin=None, tags_read={},
    )
    monkeypatch.setattr(cli, "_resolve", lambda *a, **k: book)
    monkeypatch.setattr(cli, "_duplicate_titles", lambda b: 1)
    monkeypatch.setattr(cli.review_join, "count_reviews_for_book_id", lambda *a, **k: 3)
    before = sandbox.read_text(encoding="utf-8")

    rc = cli.main(
        [
            "--overrides", str(sandbox), "edit", "tag title",
            "--set", "title=A Whole New Title",
            "--why", "title=the tag was garbled",
            "--yes",
        ]
    )
    assert rc == 4
    out = capsys.readouterr().out
    assert "KEY-MOVE WARNING" in out
    assert "old key" in out and "new key" in out
    assert "3" in out, "the (mocked) review count must be shown"
    assert "--confirm-key-move" in out
    assert sandbox.read_text(encoding="utf-8") == before, "a refused key move must leave the file untouched"


def test_cli_proceeds_past_a_key_move_with_the_confirm_flag(sandbox, monkeypatch, capsys):
    book = bl.Book(
        row={"title": "Tag Title", "author": "A. Author", "narrator": "", "year": "", "genre": "", "series": "", "series_index_display": "", "cover_href": ""},
        path=None, uncorrected=None, asin=None, tags_read={},
    )
    monkeypatch.setattr(cli, "_resolve", lambda *a, **k: book)
    monkeypatch.setattr(cli, "_duplicate_titles", lambda b: 1)
    monkeypatch.setattr(cli.review_join, "count_reviews_for_book_id", lambda *a, **k: 0)

    rc = cli.main(
        [
            "--overrides", str(sandbox), "edit", "tag title",
            "--set", "title=A Whole New Title",
            "--why", "title=the tag was garbled",
            "--confirm-key-move",
            "--yes",
        ]
    )
    assert rc == 0
    written = [e for e in store.load(sandbox)["overrides"] if e["match"].get("title") == "Tag Title"]
    assert len(written) == 1
    assert written[0]["set"]["title"] == "A Whole New Title"
    assert "KEY-MOVE WARNING" in capsys.readouterr().out


def test_cli_never_warns_when_no_key_move_is_happening(sandbox, tmp_path, monkeypatch, capsys):
    """Editing narrator/series/etc. must never hit the network at all."""
    book = bl.Book(
        row={"title": "Tag Title", "author": "A. Author", "narrator": "", "year": "", "genre": "", "series": "", "series_index_display": "", "cover_href": ""},
        path=tmp_path / "Tag Title.m4b",
        uncorrected={"title": "Tag Title", "author": "A. Author", "narrator": "", "year": "", "genre": "", "series": "", "series_index": ""},
        asin=None,
        tags_read={},
    )
    monkeypatch.setattr(cli, "_resolve", lambda *a, **k: book)
    monkeypatch.setattr(cli, "_duplicate_titles", lambda b: 1)

    def _boom(*a, **k):
        raise AssertionError("must not be called when title/author are untouched")

    monkeypatch.setattr(cli.review_join, "count_reviews_for_book_id", _boom)

    rc = cli.main(
        [
            "--overrides", str(sandbox), "edit", "tag title",
            "--set", "narrator=New Narrator",
            "--why", "narrator=Audible page credits them",
            "--yes",
        ]
    )
    assert rc == 0
    assert "KEY-MOVE" not in capsys.readouterr().out


def test_cli_refuses_a_set_with_no_why(sandbox, tmp_path, monkeypatch):
    book = bl.Book(row={"title": "T", "author": "A", "cover_href": ""}, path=None, uncorrected=None)
    monkeypatch.setattr(cli, "_resolve", lambda *a, **k: book)
    monkeypatch.setattr(cli, "_duplicate_titles", lambda b: 1)
    before = sandbox.read_text(encoding="utf-8")
    rc = cli.main(["--overrides", str(sandbox), "edit", "t", "--set", "genre=Fantasy", "--yes"])
    assert rc == 2
    assert sandbox.read_text(encoding="utf-8") == before


def test_cli_refuses_to_write_an_entry_that_could_never_fire(sandbox, tmp_path, monkeypatch, capsys):
    """A shadowing entry earlier in the list wins, so the new one is dead weight.

    find_override() returns the FIRST match. Without the pre-write simulation
    this writes cleanly, validates, and does nothing to the catalog forever.
    """
    data = store.load(sandbox)
    data["overrides"].insert(
        0,
        {
            "match": {"author": "A. Author", "title": "Tag Title"},
            "set": {"genre": "Whatever The First Entry Says"},
            "added": "2026-01-01",
            "evidence": {"genre": "first come, first served"},
        },
    )
    sandbox.write_text(store.dumps(data), encoding="utf-8")

    book = bl.Book(
        row={"title": "Tag Title", "author": "A. Author", "cover_href": ""},
        path=tmp_path / "Tag Title.m4b",
        uncorrected={"title": "Tag Title", "author": "A. Author", "series": "", "series_index": ""},
        asin="B0EXAMPLE1",  # a different key, so it lands as a SECOND entry
        tags_read={"CDEK": "B0EXAMPLE1"},
    )
    monkeypatch.setattr(cli, "_resolve", lambda *a, **k: book)
    before = sandbox.read_text(encoding="utf-8")

    rc = cli.main(["--overrides", str(sandbox), "edit", "x", "--set", "genre=Fantasy", "--why", "genre=Audible says Fantasy", "--yes"])
    assert rc == 3
    assert "FAILED" in capsys.readouterr().out
    assert sandbox.read_text(encoding="utf-8") == before


def test_first_credited_author_reduces_a_joined_string():
    assert cli._first_credited_author("Author A, Author B") == "Author A"
    assert cli._first_credited_author("Solo Author") == "Solo Author"
    assert cli._first_credited_author(None) is None
    assert cli._first_credited_author("") == ""


def test_multi_author_entry_keys_on_a_single_credited_name(sandbox, tmp_path, monkeypatch):
    """Regression (tag-repair-plan.md section 8): _build_entry used to store the
    full comma-joined author string in match.author. _author_matches() and
    entries_for() both check whether match.author equals ONE of the
    comma-separated names in the book's real (multi-author) field, so a joined
    match.author can never equal any single segment - the entry validated,
    looked correct, and silently never fired. Caught only by counting a sweep
    plan (29 vs 31), not by _verify(), because the title-only fields still came
    out correct.
    """
    book = bl.Book(
        row={
            "title": "Shared Byline",
            "author": "Author A, Author B",
            "narrator": "",
            "year": "",
            "genre": "",
            "series": "",
            "series_index_display": "",
            "cover_href": "",
        },
        path=tmp_path / "Shared Byline.m4b",
        uncorrected={
            "title": "Shared Byline",
            "author": "Author A, Author B",
            "narrator": "",
            "year": "",
            "genre": "",
            "series": "",
            "series_index": "",
        },
        asin=None,
        tags_read={"\xa9nam": "Shared Byline", "SRNM": "absent"},
    )
    monkeypatch.setattr(cli, "_resolve", lambda *a, **k: book)
    monkeypatch.setattr(cli, "_duplicate_titles", lambda b: 1)

    rc = cli.main(
        [
            "--overrides", str(sandbox), "edit", "shared byline",
            "--set", "series=Some Series",
            "--why", "series=album tag holds it",
            "--yes",
        ]
    )
    assert rc == 0

    written = [e for e in store.load(sandbox)["overrides"] if e["match"].get("title") == "Shared Byline"]
    assert len(written) == 1
    entry = written[0]
    assert entry["match"]["author"] == "Author A", "keyed on a single credited name, not the joined string"
    assert entry["book"] == "Shared Byline - Author A, Author B", "the LABEL keeps the full byline; only the key is reduced"

    # And it must actually fire once the build sees the real, multi-author row -
    # this is the check _verify() could not have caught (co.py, not book_lookup.py).
    assert co._author_matches(entry["match"]["author"], "Author A, Author B") is True
    out = store.simulate(
        store.load(sandbox),
        {"title": "Shared Byline", "author": "Author A, Author B", "series": "", "series_index": ""},
    )
    assert out["series"] == "Some Series"


def test_interactive_asks_for_a_reason_until_it_gets_one(monkeypatch, capsys):
    """Enter = leave alone, '-' = force blank, and a blank reason is re-asked."""
    book = bl.Book(
        row={"title": "T", "author": "A", "narrator": "Wrong Narrator", "year": "", "genre": "", "series": "S", "series_index_display": "1", "cover_href": ""},
        uncorrected={"title": "T", "author": "A", "narrator": "Wrong Narrator", "year": "", "genre": "", "series": "S", "series_index": "1"},
    )
    answers = iter(
        [
            "",                    # title      - leave alone
            "",                    # author
            "Right Narrator",      # narrator   - change
            "",                    # year
            "",                    # genre
            "",                    # series
            "-",                   # series_index - force blank
            "",                    # why narrator: refused, asked again
            "the Audible page credits Right Narrator",
            "no source gives a volume",  # why series_index
        ]
    )
    monkeypatch.setattr(cli, "_ask", lambda prompt, default="": next(answers))
    sets, why = cli._collect_interactive(book, current_set={})
    assert sets == {"narrator": "Right Narrator", "series_index": ""}
    assert why["narrator"].startswith("the Audible page")
    assert "Required" in capsys.readouterr().out


def test_interactive_skips_a_value_retyped_unchanged(monkeypatch):
    book = bl.Book(
        row={"title": "T", "author": "A", "narrator": "N", "year": "", "genre": "", "series": "S", "series_index_display": "1", "cover_href": ""},
        uncorrected={"title": "T", "author": "A", "narrator": "N", "year": "", "genre": "", "series": "S", "series_index": "1"},
    )
    answers = iter(["", "", "N", "", "", "", "", ""])
    monkeypatch.setattr(cli, "_ask", lambda prompt, default="": next(answers))
    assert cli._collect_interactive(book, current_set={}) == ({}, {})


def test_cli_validate_reports_errors_with_a_nonzero_exit(sandbox):
    data = store.load(sandbox)
    data["overrides"][0]["evidence"] = {}
    sandbox.write_text(store.dumps(data), encoding="utf-8")
    assert cli.main(["--overrides", str(sandbox), "validate"]) == 2


def test_cli_validate_is_clean_on_the_real_file():
    assert cli.main(["--overrides", str(LIVE_JSON), "validate"]) == 0


# --------------------------------------------------------------------------- #
# book_lookup
# --------------------------------------------------------------------------- #


def test_locate_file_uses_cover_href_not_the_title(tmp_path):
    """The title is the field most likely to be wrong on a book that needs fixing."""
    (tmp_path / "A. Author").mkdir()
    real = tmp_path / "A. Author" / "Some File Stem.m4b"
    real.write_bytes(b"")
    row = {"title": "A Completely Different Published Title", "author": "A. Author", "cover_href": "covers/A. Author/Some File Stem.jpg"}
    assert bl.locate_file(row, root=tmp_path) == real


def test_locate_file_falls_back_to_the_author_folder(tmp_path):
    (tmp_path / "A. Author").mkdir()
    real = tmp_path / "A. Author" / "Some Title - Book 2.m4b"
    real.write_bytes(b"")
    row = {"title": "Some Title", "author": "A. Author", "cover_href": ""}
    assert bl.locate_file(row, root=tmp_path) == real


def test_locate_file_returns_none_rather_than_a_wrong_guess(tmp_path):
    row = {"title": "Nothing Here", "author": "Nobody", "cover_href": "covers/Nobody/Nothing Here.jpg"}
    assert bl.locate_file(row, root=tmp_path) is None


def test_search_requires_every_term(tmp_path):
    rows = [
        {"title": "Thunderplump", "author": "Dakota Krout", "series": "The Completionist Chronicles"},
        {"title": "Ritualist", "author": "Dakota Krout", "series": "The Completionist Chronicles"},
    ]
    assert [r["title"] for r in bl.search(rows, "krout thunder")] == ["Thunderplump"]
    assert len(bl.search(rows, "krout")) == 2
    assert bl.search(rows, "nothing matches this") == []


def test_tags_summary_uses_the_bare_vendor_atoms(tmp_path):
    """'----:com.apple.iTunes:SRNM' is always absent and reads as an untagged file."""
    summary = bl.tags_summary({"\xa9nam": ["A Title"], "SRNM": ["A Series"], "trkn": [(8, 0)]})
    assert summary["\xa9nam"] == "A Title"
    assert summary["SRNM"] == "A Series"
    assert summary["trkn"] == "(8, 0)"
    assert summary["CDEK"] == "absent"


def test_derive_correctable_fields_is_what_the_layer_matches_on():
    """The editor keys entries on this, so it must be the same function the build uses."""
    import app.metadata as md

    body = (REPO_ROOT / "app" / "metadata.py").read_text(encoding="utf-8").split("def extract_metadata")[1]
    assert "derive_correctable_fields(" in body, "extract_metadata must use the shared helper, not its own copy"
    derived = md.derive_correctable_fields({"\xa9nam": ["T"], "\xa9ART": ["A"], "SRNM": ["S"], "SRSQ": ["3"]})
    assert derived == {"title": "T", "author": "A", "narrator": None, "year": "", "genre": "", "series": "S", "series_index": "3"}
