"""Tests for the prepared tag sweep (app/tools/audit_series_tags.py).

⚠️  These tests NEVER touch the real library. Every m4b here is generated into a
pytest tmp_path by scripts/generate_test_book.py. The tool itself has never been
run against C:/Users/nbasl/OpenAudible/books and must not be until the owner
asks for it.

What is pinned:
  * the safety rails — dry run by default, no write without --commit, no value
    ever taken from a filename, conflicts reported rather than resolved;
  * the backup/verify/restore round trip, on real files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mutagen.mp4 import MP4

from app.tools import audit_series_tags as ast

K_SRNM = "SRNM"
K_SRSQ = "SRSQ"
K_ALB = "\xa9alb"
K_TRKN = "trkn"
K_NAM = "\xa9nam"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def make_book(tmp_path: Path, name: str, *, title, series=None, index=None, album=None, track=None) -> Path:
    """A real, tiny, tagged m4b in tmp_path. Nothing outside tmp_path is touched."""
    from scripts.generate_test_book import generate_test_book

    out = tmp_path / name
    generate_test_book(title=title, author="Test Author", series=series, series_index=index, output=out)

    audio = MP4(str(out))
    if audio.tags is None:
        audio.add_tags()
    for atom in (K_SRNM, K_SRSQ):
        if (series is None and atom == K_SRNM) or (index is None and atom == K_SRSQ):
            audio.tags.pop(atom, None)
    if album is not None:
        audio.tags[K_ALB] = [album]
    if track is not None:
        audio.tags[K_TRKN] = [(track, 0)]
    audio.save()
    return out


def scan(path: Path) -> ast.Scan:
    return ast.scan_file(path)


# --------------------------------------------------------------------------- #
# Filename parsing is evidence only
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "stem,series,index",
    [
        (
            "Dakota Krout - [The Completionist Chronicles - 11] - Thunderplump (Luke Daniels)",
            "The Completionist Chronicles",
            "11",
        ),
        ("Ritualist - Completionist Chronicles, Book 1", "Completionist Chronicles", "1"),
        ("Anima - A Divine Dungeon Series (Artorian's Archives, Book 6)", "Artorian's Archives", "6"),
        ("Implode", None, None),
        ("Lord January", None, None),
    ],
)
def test_parse_filename(stem, series, index):
    assert ast.parse_filename(stem) == (series, index)


def test_a_filename_value_is_never_written(tmp_path):
    """The whole point: the tags were right and the filenames were not."""
    book = make_book(
        tmp_path,
        "Mystery - Some Series, Book 4.m4b",
        title="Mystery",
        series=None,
        index=None,
        album=None,
        track=None,
    )
    s = scan(book)
    assert s.file_series == "Some Series" and s.file_index == "4", "the filename evidence is unambiguous"
    assert "SERIES_BLANK" in s.issues
    # No album, no trkn, no override -> nothing to write, despite a confident filename.
    assert ast.propose(s) is None


def test_tag_wins_when_filename_disagrees_and_the_clash_is_reported(tmp_path):
    """The Untapped case: filename says 11, trkn says 12."""
    book = make_book(
        tmp_path,
        "Untapped - The Completionist Chronicles, Book 11.m4b",
        title="Untapped Test",
        series=None,
        index=None,
        album="Completionist Chronicles",
        track=12,
    )
    s = scan(book)
    assert "FILENAME_INDEX_DIFFERS" in s.issues
    proposal = ast.propose(s)
    assert proposal["writes"][K_SRSQ] == "12", "the tag must win, not the filename"
    assert "filename" in proposal["sources"]["_note"]


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #


def test_blank_series_recoverable_from_album(tmp_path):
    book = make_book(tmp_path, "x.m4b", title="Tenacity Test", series=None, index=None, album="Some Series", track=9)
    s = scan(book)
    assert "SERIES_ONLY_IN_ALBUM" in s.issues
    assert "INDEX_ONLY_IN_TRACK" in s.issues


def test_series_field_holding_the_title_is_detected_and_blocked(tmp_path):
    """The Uncapped defect. Detected, and never auto-repaired."""
    book = make_book(tmp_path, "y.m4b", title="Uncapped Test", series=None, index=None, album="Uncapped Test", track=14)
    s = scan(book)
    assert "SERIES_IS_TITLE" in s.issues
    proposal = ast.propose(s)
    assert K_SRNM not in proposal["writes"], "must not copy the title into the series tag"
    assert proposal["blocked_by"] == ["SERIES_IS_TITLE"], "a blocked proposal is never executed by repair()"
    assert proposal["curated"] is False


def test_a_curated_correction_is_not_blocked_by_the_defect_it_documents(tmp_path, monkeypatch):
    """
    scripts/catalog_overrides.json IS the human decision, made once with citations.
    The real Uncapped is exactly this: album tag holds the title, and the curated
    entry resolves it. Blocking that would make the safest sweep unable to fix
    the worst defect.
    """
    book = make_book(tmp_path, "unc.m4b", title="Uncapped", series=None, index=None, album="Uncapped", track=14)
    s = scan(book)
    monkeypatch.setattr(s, "artist", "Dakota Krout")
    s.override_series, s.override_index = "The Completionist Chronicles", "14"
    proposal = ast.propose(s, from_overrides_only=True)
    assert proposal["curated"] is True
    assert proposal["blocked_by"] == []
    assert proposal["writes"] == {K_SRNM: "The Completionist Chronicles", K_SRSQ: "14"}


def test_two_tag_sources_disagreeing_is_a_decision_not_a_repair(tmp_path):
    book = make_book(tmp_path, "z.m4b", title="Conflicted", series="A Series", index="3", album="A Series", track=7)
    s = scan(book)
    assert "INDEX_CONFLICT" in s.issues
    assert ast.propose(s) is None, "SRSQ=3 vs trkn=7 must be reported, never silently picked"


def test_non_canonical_spelling_is_flagged_and_normalised(tmp_path):
    book = make_book(
        tmp_path, "w.m4b", title="Some Book", series="Completionist Chronicles Series", index="4", album=None, track=None
    )
    s = scan(book)
    assert "SERIES_SPELLING" in s.issues
    proposal = ast.propose(s)
    assert proposal["writes"][K_SRNM] == "The Completionist Chronicles"


def test_correctly_tagged_file_has_no_issues_and_no_proposal(tmp_path):
    book = make_book(tmp_path, "ok.m4b", title="Fine Book", series="A Perfectly Fine Series", index="2")
    s = scan(book)
    assert s.issues == []
    assert ast.propose(s) is None


def test_duplicate_volumes_are_reported_not_resolved(tmp_path):
    a = make_book(tmp_path, "a.m4b", title="Book A", series="Dup Series", index="5")
    b = make_book(tmp_path, "b.m4b", title="Book B", series="Dup Series", index="5")
    scans = [scan(a), scan(b)]
    cross = ast.cross_check(scans)
    assert cross["duplicate_volumes"]["Dup Series"]["5"]
    for s in scans:
        assert "DUPLICATE_VOLUME" in s.issues
        proposal = ast.propose(s)
        assert proposal is None or proposal["blocked_by"] == ["DUPLICATE_VOLUME"]


def test_series_gaps_are_reported(tmp_path):
    books = [make_book(tmp_path, f"g{i}.m4b", title=f"G{i}", series="Gap Series", index=str(i)) for i in (1, 2, 5)]
    cross = ast.cross_check([scan(b) for b in books])
    assert cross["series_gaps"]["Gap Series"] == [3, 4]


def test_library_spelling_variants_are_collected(tmp_path):
    a = make_book(tmp_path, "v1.m4b", title="V1", series="Completionist Chronicles", index="1")
    b = make_book(tmp_path, "v2.m4b", title="V2", series="The Completionist Chronicles Series", index="2")
    cross = ast.cross_check([scan(a), scan(b)])
    variants = cross["series_spelling_variants"]["The Completionist Chronicles"]
    assert len(variants) > 1


# --------------------------------------------------------------------------- #
# The corrections file drives the safest sweep
# --------------------------------------------------------------------------- #


def test_from_overrides_only_writes_nothing_without_a_curated_entry(tmp_path):
    book = make_book(tmp_path, "u.m4b", title="Unknown Book", series=None, index=None, album="Some Series", track=3)
    s = scan(book)
    assert ast.propose(s) is not None, "the permissive mode would write this"
    assert ast.propose(s, from_overrides_only=True) is None, "the safe mode must not"


# --------------------------------------------------------------------------- #
# Backup, write, verify, restore
# --------------------------------------------------------------------------- #


def test_backup_records_present_and_absent_atoms(tmp_path):
    book = make_book(tmp_path, "s.m4b", title="Snap", series=None, index=None, album="Alb", track=4)
    rec = ast._atom_snapshot(book)
    assert rec["atoms"][K_ALB] == ["Alb"]
    assert K_SRNM in rec["absent"] and K_SRSQ in rec["absent"]
    assert rec["size"] > 0


def test_backup_is_written_and_verified(tmp_path):
    book = make_book(tmp_path, "s.m4b", title="Snap", series="S", index="1")
    run_dir = tmp_path / "run"
    backup = ast.write_backup([ast._atom_snapshot(book)], run_dir)
    assert backup.is_file()
    lines = [json.loads(x) for x in backup.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(lines) == 1 and lines[0]["path"] == str(book)


@pytest.mark.parametrize("safe_copy", [False, True])
def test_repair_then_restore_round_trip(tmp_path, safe_copy):
    """The rollback story, end to end, on real files."""
    book = make_book(tmp_path, "r.m4b", title="Round Trip", series=None, index=None, album="Real Series", track=6)
    before = MP4(str(book)).tags
    assert K_SRNM not in before and K_SRSQ not in before

    s = scan(book)
    s.proposal = ast.propose(s)
    run_dir = tmp_path / "run"
    assert ast.repair([s], run_dir, safe_copy=safe_copy) == 0

    after = MP4(str(book)).tags
    assert after[K_SRNM] == ["Real Series"]
    assert after[K_SRSQ] == ["6"]

    assert ast.restore_run(run_dir, safe_copy=safe_copy) == 0
    restored = MP4(str(book)).tags
    assert K_SRNM not in restored, "restore must delete atoms that were absent before"
    assert K_SRSQ not in restored
    assert restored[K_ALB] == ["Real Series"]
    assert restored[K_TRKN] == [(6, 0)]


def test_restore_puts_back_a_previous_value_not_just_a_deletion(tmp_path):
    book = make_book(tmp_path, "p.m4b", title="Prev", series="Completionist Chronicles Series", index="4")
    s = scan(book)
    s.proposal = ast.propose(s)
    run_dir = tmp_path / "run"
    ast.repair([s], run_dir, safe_copy=False)
    assert MP4(str(book)).tags[K_SRNM] == ["The Completionist Chronicles"]

    ast.restore_run(run_dir)
    assert MP4(str(book)).tags[K_SRNM] == ["Completionist Chronicles Series"]


def test_repair_skips_blocked_files(tmp_path):
    book = make_book(tmp_path, "b.m4b", title="Blocked", series="A", index="3", album="A", track=9)
    s = scan(book)
    s.proposal = ast.propose(s)
    run_dir = tmp_path / "run"
    ast.repair([s], run_dir, safe_copy=False)
    assert not (run_dir / "backup.jsonl").exists(), "a blocked file must not even trigger a backup"
    assert MP4(str(book)).tags[K_SRSQ] == ["3"], "the file must be untouched"


def test_restore_without_a_backup_fails_loudly(tmp_path):
    assert ast.restore_run(tmp_path / "nope") == 1


# --------------------------------------------------------------------------- #
# CLI safety rails
# --------------------------------------------------------------------------- #


def test_commit_is_not_the_default():
    args = ast.build_parser().parse_args([])
    assert args.commit is False
    assert args.plan is False
    assert args.restore is None


def test_commit_must_be_explicit():
    assert ast.build_parser().parse_args(["--commit"]).commit is True
    assert ast.build_parser().parse_args(["--plan"]).commit is False


def test_only_srnm_and_srsq_are_ever_writable():
    """Titles, authors and dates on purchased media stay untouched."""
    assert ast.WRITABLE_ATOMS == ("SRNM", "SRSQ")
    assert set(ast.WRITABLE_ATOMS) <= set(ast.BACKED_UP_ATOMS), "every writable atom must be backed up"


def test_needs_decision_codes_are_never_auto_repaired():
    assert ast.NEEDS_DECISION == {"INDEX_CONFLICT", "DUPLICATE_VOLUME", "SERIES_IS_TITLE"}
    assert ast.NEEDS_DECISION <= set(ast.ISSUES)


def test_importing_the_module_scans_nothing(tmp_path, monkeypatch):
    """Importing must be inert — no walk of 422 folders as a side effect."""
    import importlib

    called = []
    monkeypatch.setattr(ast, "collect_files", lambda *a, **k: called.append(1) or [])
    importlib.reload(ast)
    assert called == []
