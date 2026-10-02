"""STEP 1c (2026-10-02): standalone ebooks are filed into author folders by the
pipeline, the way `sort_books()` files audio.

The gap it closes. `sort_books()` filters on AUDIOBOOK_EXTS and
`sort_companion_files()` only files a doc beside the audiobook it belongs to,
while STEP 4 refuses anything that is not in an author folder. So an ebook with
no audiobook sat loose at the library root for ever: published to the gated
shelf, never backed up to Drive. Measured on the day: 82 loose, 0 of them in
the upload manifest.

What these tests pin:
  * a loose ebook with an OPF author is moved, logged, and returned (so the
    caller can put it in `just_moved` and STEP 4 uploads it the same run);
  * the refusals survive being run unattended — never overwrite, never touch a
    filed file, never invent an author;
  * a re-downloaded DUPLICATE (its author folder already holds that filename)
    is set aside outside the library: never deleted, the filed copy untouched,
    a second duplicate never overwrites the first;
  * unattended, a FILENAME-derived author may join an existing folder but may
    never create one;
  * the hand-run CLI planner keeps its old behaviour on both points;
  * `--dry-run` plans and moves nothing;
  * a failure inside the step is a WARN, never a stopped pipeline.

The library is a real tmp tree; only the OPF read (which would need a real
EPUB zip) is stubbed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import sort_ebooks
from scripts import sync_to_drive as sync


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / "books"
    root.mkdir()

    import app.config as cfg
    monkeypatch.setattr(cfg, "ROOT_DIR", root)

    import app.author_names as an
    monkeypatch.setattr(an, "load_shelf_aliases", lambda: {})

    monkeypatch.setattr(sync, "EBOOK_SORT_LOG_PATH", tmp_path / "out" / "ebook_sort_log.jsonl")
    monkeypatch.delenv("EBOOK_DUPLICATES_DIR", raising=False)
    return root


def _opf(monkeypatch, mapping: dict[str, str]):
    """Stub the OPF read: {filename: author}. Anything absent has no OPF author."""
    monkeypatch.setattr(
        sort_ebooks, "get_epub_metadata",
        lambda p: {"title": Path(p).stem, "author": mapping.get(Path(p).name, "")},
    )


def _file(root: Path, *parts: str, data: bytes = b"epub") -> Path:
    p = root.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _log_lines() -> list[dict]:
    return [json.loads(x) for x in sync.EBOOK_SORT_LOG_PATH.read_text(encoding="utf-8").splitlines()]


def test_loose_ebook_is_filed_logged_and_returned(library, monkeypatch):
    loose = _file(library, "Unsouled - Will Wight.epub")
    _opf(monkeypatch, {loose.name: "Will Wight"})

    moved, skipped, set_aside = sync.sort_standalone_ebooks(dry_run=False)

    dest = library / "Will Wight" / loose.name
    assert moved == [dest]
    assert (skipped, set_aside) == ([], [])
    assert dest.exists() and not loose.exists()

    (rec,) = _log_lines()
    assert rec["from"] == loose.name
    assert rec["to"] == f"Will Wight/{loose.name}"
    assert rec["author_source"] == "opf"


def test_existing_folder_is_matched_case_insensitively(library, monkeypatch):
    (library / "Sir Bedivere The Mad").mkdir()
    loose = _file(library, "Bunny Girl - Sir Bedivere the Mad.epub")
    _opf(monkeypatch, {loose.name: "Sir Bedivere the Mad"})

    moved, _, _ = sync.sort_standalone_ebooks(dry_run=False)

    assert moved == [library / "Sir Bedivere The Mad" / loose.name]
    assert [d.name for d in library.iterdir() if d.is_dir()] == ["Sir Bedivere The Mad"]


def test_duplicate_is_set_aside_outside_the_library_and_nothing_is_overwritten(library, monkeypatch):
    filed = _file(library, "Will Wight", "Reaper - Will Wight.epub", data=b"old")
    loose = _file(library, "Reaper - Will Wight.epub", data=b"new")
    _opf(monkeypatch, {loose.name: "Will Wight"})

    moved, skipped, set_aside = sync.sort_standalone_ebooks(dry_run=False)

    aside = library.parent / "ebook_duplicates" / "Will Wight" / loose.name
    assert (moved, skipped) == ([], [])
    assert len(set_aside) == 1 and loose.name in set_aside[0]
    assert filed.read_bytes() == b"old"          # the filed copy is untouched
    assert aside.read_bytes() == b"new"          # the duplicate is kept, not deleted
    assert not loose.exists()                    # and it is out of the library
    assert library not in aside.parents

    (rec,) = _log_lines()
    assert rec["kind"] == "duplicate" and rec["from"] == loose.name


def test_a_second_duplicate_never_overwrites_the_first(library, monkeypatch):
    _file(library, "Will Wight", "Reaper - Will Wight.epub", data=b"old")
    first = _file(library.parent, "ebook_duplicates", "Will Wight", "Reaper - Will Wight.epub", data=b"dup1")
    loose = _file(library, "Reaper - Will Wight.epub", data=b"dup2")
    _opf(monkeypatch, {loose.name: "Will Wight"})

    _, _, set_aside = sync.sort_standalone_ebooks(dry_run=False)

    assert len(set_aside) == 1
    assert first.read_bytes() == b"dup1"
    kept = sorted(p.read_bytes() for p in first.parent.iterdir())
    assert kept == [b"dup1", b"dup2"]


def test_duplicates_dir_can_be_overridden(library, monkeypatch, tmp_path):
    monkeypatch.setenv("EBOOK_DUPLICATES_DIR", str(tmp_path / "elsewhere"))
    _file(library, "Will Wight", "Reaper - Will Wight.epub")
    loose = _file(library, "Reaper - Will Wight.epub")
    _opf(monkeypatch, {loose.name: "Will Wight"})

    sync.sort_standalone_ebooks(dry_run=False)

    assert (tmp_path / "elsewhere" / "Will Wight" / loose.name).exists()


def test_never_touches_a_file_already_in_a_folder(library, monkeypatch):
    filed = _file(library, "Somebody Else", "Unsouled - Will Wight.epub")
    _opf(monkeypatch, {filed.name: "Will Wight"})

    assert sync.sort_standalone_ebooks(dry_run=False) == ([], [], [])
    assert filed.exists()


def test_no_author_is_left_loose_and_named(library, monkeypatch):
    loose = _file(library, "mistborn_adventuregame.pdf")
    _opf(monkeypatch, {})

    moved, skipped, set_aside = sync.sort_standalone_ebooks(dry_run=False)

    assert (moved, set_aside) == ([], [])
    assert len(skipped) == 1 and loose.name in skipped[0]
    assert loose.exists()


def test_filename_author_may_join_an_existing_folder(library, monkeypatch):
    (library / "Brandon Sanderson").mkdir()
    loose = _file(library, "Handbook - Brandon Sanderson.pdf")
    _opf(monkeypatch, {})

    moved, skipped, _ = sync.sort_standalone_ebooks(dry_run=False)

    assert moved == [library / "Brandon Sanderson" / loose.name]
    assert skipped == []


def test_filename_author_never_creates_a_folder_unattended(library, monkeypatch):
    """"Title - Something.pdf" is a guess. A guess that mints a folder becomes
    a Drive folder the same run — the pipeline does not make that call."""
    loose = _file(library, "Field Guide - Deluxe Edition.pdf")
    _opf(monkeypatch, {})

    moved, skipped, _ = sync.sort_standalone_ebooks(dry_run=False)

    assert moved == []
    assert len(skipped) == 1 and "filename only" in skipped[0]
    assert loose.exists()
    assert not (library / "Deluxe Edition").exists()


def test_hand_run_planner_is_unchanged(library, monkeypatch):
    """The CLI is attended — a person reads the plan before --commit — so it
    still creates a folder from a filename and still only REPORTS a collision."""
    new = _file(library, "Field Guide - Jane Roe.pdf")
    _file(library, "Will Wight", "Reaper - Will Wight.epub")
    dupe = _file(library, "Reaper - Will Wight.epub")
    _opf(monkeypatch, {dupe.name: "Will Wight"})

    moves, skipped = sort_ebooks.plan_moves(library, {})

    assert [m["to"] for m in moves] == [f"Jane Roe/{new.name}"]
    assert moves[0]["new_folder"] == "yes"
    assert len(skipped) == 1 and "already holds a file of this name" in skipped[0]
    assert dupe.exists()


def test_dry_run_plans_and_moves_nothing(library, monkeypatch):
    _file(library, "Will Wight", "Reaper - Will Wight.epub")
    dupe = _file(library, "Reaper - Will Wight.epub")
    loose = _file(library, "Unsouled - Will Wight.epub")
    _opf(monkeypatch, {loose.name: "Will Wight", dupe.name: "Will Wight"})

    moved, _, set_aside = sync.sort_standalone_ebooks(dry_run=True)

    assert moved == [library / "Will Wight" / loose.name]
    assert len(set_aside) == 1
    assert loose.exists() and dupe.exists()
    assert not (library.parent / "ebook_duplicates").exists()
    assert not sync.EBOOK_SORT_LOG_PATH.exists()


def test_shelf_alias_is_honoured(library, monkeypatch):
    import app.author_names as an
    monkeypatch.setattr(an, "load_shelf_aliases", lambda: {"alex toxic": "Nadya Lee"})
    monkeypatch.setattr(sort_ebooks, "resolve_shelf_author", lambda a, al: al.get(a.lower(), a))
    loose = _file(library, "Book - Alex Toxic.epub")
    _opf(monkeypatch, {loose.name: "Alex Toxic"})

    moved, _, _ = sync.sort_standalone_ebooks(dry_run=False)

    assert moved == [library / "Nadya Lee" / loose.name]


def test_idle_path_publishes_the_ebook_shelf_before_the_index_push():
    """Owner 2026-10-02 ("1 yes"): a run that uploads nothing still publishes a
    changed ebook manifest. Measured that day: 20 duplicates set aside, local
    manifest 244 -> 224, and the idle run published nothing. Source-text pin,
    in the style of test_index_push.py's idle-path check."""
    src = Path(sync.__file__).read_text(encoding="utf-8")
    idle = src.split("Nothing to upload. All books are synced!")[1].split("finish_run")[0]
    assert "_sync_ebook_shelf()" in idle
    assert idle.index("_sync_ebook_shelf()") < idle.index("_push_estate_index(")


def test_one_ebook_shelf_implementation_and_files_before_manifest():
    src = Path(sync.__file__).read_text(encoding="utf-8")
    body = src.split("def _sync_ebook_shelf()")[1].split("\ndef ")[0]
    assert body.index("upload_ebooks_main") < body.index("publish_ebooks_main")
    # The busy path calls the same helper rather than carrying a second copy.
    busy = src.split("def _run_pipeline_body(")[1].split("\ndef ")[0]
    assert "_sync_ebook_shelf()" in busy
    assert "upload_ebooks_main" not in busy and "publish_ebooks_main" not in busy


def test_a_failure_is_a_warning_not_a_stopped_pipeline(library, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(sort_ebooks, "plan_moves", boom)

    assert sync.sort_standalone_ebooks(dry_run=False) == ([], [], [])
    assert "[WARN] Ebook sort failed" in capsys.readouterr().out
