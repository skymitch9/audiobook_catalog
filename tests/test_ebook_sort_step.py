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
  * the three refusals survive being run unattended — never overwrite, never
    touch a filed file, never invent an author;
  * unattended, a FILENAME-derived author may join an existing folder but may
    never create one;
  * the hand-run CLI planner keeps its old, more permissive behaviour;
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


def test_loose_ebook_is_filed_logged_and_returned(library, monkeypatch):
    loose = _file(library, "Unsouled - Will Wight.epub")
    _opf(monkeypatch, {loose.name: "Will Wight"})

    moved, skipped = sync.sort_standalone_ebooks(dry_run=False)

    dest = library / "Will Wight" / loose.name
    assert moved == [dest]
    assert skipped == []
    assert dest.exists() and not loose.exists()

    lines = sync.EBOOK_SORT_LOG_PATH.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["from"] == loose.name
    assert rec["to"] == f"Will Wight/{loose.name}"
    assert rec["author_source"] == "opf"


def test_existing_folder_is_matched_case_insensitively(library, monkeypatch):
    (library / "Sir Bedivere The Mad").mkdir()
    loose = _file(library, "Bunny Girl - Sir Bedivere the Mad.epub")
    _opf(monkeypatch, {loose.name: "Sir Bedivere the Mad"})

    moved, _ = sync.sort_standalone_ebooks(dry_run=False)

    assert moved == [library / "Sir Bedivere The Mad" / loose.name]
    assert [d.name for d in library.iterdir() if d.is_dir()] == ["Sir Bedivere The Mad"]


def test_never_overwrites_a_filed_copy(library, monkeypatch):
    filed = _file(library, "Will Wight", "Reaper - Will Wight.epub", data=b"old")
    loose = _file(library, "Reaper - Will Wight.epub", data=b"new")
    _opf(monkeypatch, {loose.name: "Will Wight"})

    moved, skipped = sync.sort_standalone_ebooks(dry_run=False)

    assert moved == []
    assert len(skipped) == 1 and "already holds a file of this name" in skipped[0]
    assert filed.read_bytes() == b"old"
    assert loose.read_bytes() == b"new"
    assert not sync.EBOOK_SORT_LOG_PATH.exists()


def test_never_touches_a_file_already_in_a_folder(library, monkeypatch):
    filed = _file(library, "Somebody Else", "Unsouled - Will Wight.epub")
    _opf(monkeypatch, {filed.name: "Will Wight"})

    moved, skipped = sync.sort_standalone_ebooks(dry_run=False)

    assert (moved, skipped) == ([], [])
    assert filed.exists()


def test_no_author_is_left_loose_and_named(library, monkeypatch):
    loose = _file(library, "mistborn_adventuregame.pdf")
    _opf(monkeypatch, {})

    moved, skipped = sync.sort_standalone_ebooks(dry_run=False)

    assert moved == []
    assert len(skipped) == 1 and loose.name in skipped[0]
    assert loose.exists()


def test_filename_author_may_join_an_existing_folder(library, monkeypatch):
    (library / "Brandon Sanderson").mkdir()
    loose = _file(library, "Handbook - Brandon Sanderson.pdf")
    _opf(monkeypatch, {})

    moved, skipped = sync.sort_standalone_ebooks(dry_run=False)

    assert moved == [library / "Brandon Sanderson" / loose.name]
    assert skipped == []


def test_filename_author_never_creates_a_folder_unattended(library, monkeypatch):
    """"Title - Something.pdf" is a guess. A guess that mints a folder becomes
    a Drive folder the same run — the pipeline does not make that call."""
    loose = _file(library, "Field Guide - Deluxe Edition.pdf")
    _opf(monkeypatch, {})

    moved, skipped = sync.sort_standalone_ebooks(dry_run=False)

    assert moved == []
    assert len(skipped) == 1 and "filename only" in skipped[0]
    assert loose.exists()
    assert not (library / "Deluxe Edition").exists()


def test_hand_run_planner_still_creates_a_folder_from_a_filename(library, monkeypatch):
    """The CLI is attended — a person reads the plan before --commit — so its
    behaviour is unchanged by the unattended guard."""
    loose = _file(library, "Field Guide - Jane Roe.pdf")
    _opf(monkeypatch, {})

    moves, skipped = sort_ebooks.plan_moves(library, {})

    assert skipped == []
    assert [m["to"] for m in moves] == [f"Jane Roe/{loose.name}"]
    assert moves[0]["new_folder"] == "yes"


def test_dry_run_plans_and_moves_nothing(library, monkeypatch):
    loose = _file(library, "Unsouled - Will Wight.epub")
    _opf(monkeypatch, {loose.name: "Will Wight"})

    moved, _ = sync.sort_standalone_ebooks(dry_run=True)

    assert moved == [library / "Will Wight" / loose.name]
    assert loose.exists()
    assert not (library / "Will Wight").exists()
    assert not sync.EBOOK_SORT_LOG_PATH.exists()


def test_shelf_alias_is_honoured(library, monkeypatch):
    import app.author_names as an
    monkeypatch.setattr(an, "load_shelf_aliases", lambda: {"alex toxic": "Nadya Lee"})
    monkeypatch.setattr(an, "resolve_shelf_author", lambda a, al: al.get(a.lower(), a))
    monkeypatch.setattr(sort_ebooks, "resolve_shelf_author", lambda a, al: al.get(a.lower(), a))
    loose = _file(library, "Book - Alex Toxic.epub")
    _opf(monkeypatch, {loose.name: "Alex Toxic"})

    moved, _ = sync.sort_standalone_ebooks(dry_run=False)

    assert moved == [library / "Nadya Lee" / loose.name]


def test_a_failure_is_a_warning_not_a_stopped_pipeline(library, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(sort_ebooks, "plan_moves", boom)

    assert sync.sort_standalone_ebooks(dry_run=False) == ([], [])
    assert "[WARN] Ebook sort failed" in capsys.readouterr().out
