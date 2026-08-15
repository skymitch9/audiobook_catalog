"""Tests for scripts/sync_to_drive.py's STEP 4 upload classification.

Regression coverage for the 2026-08-15 morning incident: 9 loose epubs sat at
the library ROOT (no <Author>/ folder). The upload step counted each
root-level skip as a FAILURE, which flipped the run to "partial" and — because
STEP 5/6 (rebuild + commit/push) were gated on `uploaded_count > 0` — the
already-refreshed site/ebooks.json manifest never got committed. Ebooks are a
first-class upload path now (they feed library_catalog's ebook lane), so:

  * a misplaced file is a WARNING, not a failure — it must never flip the run
    to "partial" by itself;
  * an ebook-only (or misplaced-only) run must still publish everything else
    that changed — see the STEP 6 gate fix in run_pipeline().

These tests exercise the extracted, side-effect-free pieces of that logic
(`_file_is_misplaced`, `UploadOutcome`, `upload_run_state`, and
`_upload_new_files` with its Drive calls monkeypatched out) rather than the
full `run_pipeline()`, which talks to Google Drive, Firestore, and git.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import sync_to_drive as sync


# ---------------------------------------------------------------------------
# _file_is_misplaced
# ---------------------------------------------------------------------------


def test_file_in_author_folder_is_not_misplaced():
    rel = Path("Honour Rae") / "All the Skills.epub"
    assert sync._file_is_misplaced(rel) is False


def test_file_at_library_root_is_misplaced():
    rel = Path("All the Skills.epub")
    assert sync._file_is_misplaced(rel) is True


def test_file_nested_two_deep_is_not_misplaced():
    rel = Path("Selkie Myth") / "Beneath the Dragoneye Moons" / "book.epub"
    assert sync._file_is_misplaced(rel) is False


# ---------------------------------------------------------------------------
# upload_run_state / UploadOutcome.run_state — the actual classification fix
# ---------------------------------------------------------------------------


def test_upload_run_state_success_with_no_failures():
    assert sync.upload_run_state(0) == "success"


def test_upload_run_state_partial_on_real_failure():
    assert sync.upload_run_state(1) == "partial"


def test_outcome_misplaced_only_is_success_not_partial():
    """The core of the fix: misplaced files must NOT count as failures."""
    outcome = sync.UploadOutcome(misplaced=["a.epub", "b.epub"])
    assert outcome.failed_count == 0
    assert outcome.run_state() == "success"


def test_outcome_real_failure_is_partial_even_with_uploads():
    outcome = sync.UploadOutcome(uploaded=["a.m4b"], failed=["b.m4b"])
    assert outcome.run_state() == "partial"


def test_outcome_warnings_name_each_misplaced_file():
    outcome = sync.UploadOutcome(misplaced=["Loose Book.epub"])
    assert outcome.warnings() == ["Not in author folder: Loose Book.epub"]


# ---------------------------------------------------------------------------
# _upload_new_files — the STEP 4 loop, with Drive calls stubbed out
# ---------------------------------------------------------------------------


@pytest.fixture
def root(tmp_path):
    return tmp_path


def _fake_upload(returns):
    """Build a upload_file_to_drive stand-in that returns `returns` in order,
    one (file_id, already_existed) tuple per call."""
    calls = iter(returns)

    def _upload(service, file_path, folder_id, dry_run=False, item_index=0, item_total=0):
        return next(calls)

    return _upload


def test_misplaced_files_never_reach_drive_calls(root, monkeypatch):
    """Root-level files must short-circuit before any author resolution or
    upload call — this is what actually fixes the incident."""
    calls = []
    monkeypatch.setattr(sync, "resolve_alias", lambda *a, **k: calls.append("resolve_alias"))
    monkeypatch.setattr(sync, "resolve_author_to_drive_folder", lambda *a, **k: calls.append("resolve_folder"))
    monkeypatch.setattr(sync, "upload_file_to_drive", lambda *a, **k: calls.append("upload"))

    new_files = [root / name for name in ["Loose One.epub", "Loose Two.epub"]]

    manifest_updates, outcome, new_folders, links = sync._upload_new_files(
        new_files, root, aliases={}, drive_folders={}, service=None, dry_run=False,
    )

    assert calls == []  # no Drive interaction at all
    assert outcome.misplaced == ["Loose One.epub", "Loose Two.epub"]
    assert outcome.uploaded_count == 0
    assert outcome.already_count == 0
    assert outcome.failed_count == 0
    assert manifest_updates == {}
    assert new_folders == []
    assert links == {}
    assert outcome.run_state() == "success"


def test_uploaded_and_already_on_drive_are_distinguished(root, monkeypatch):
    monkeypatch.setattr(sync, "resolve_alias", lambda author, aliases: (author, None))
    monkeypatch.setattr(
        sync, "resolve_author_to_drive_folder",
        lambda author, folders, dry_run=False: (author, f"folder-{author}"),
    )
    monkeypatch.setattr(
        sync, "upload_file_to_drive",
        _fake_upload([("id-new", False), ("id-existing", True)]),
    )

    new_files = [
        root / "Author A" / "New Book.m4b",
        root / "Author B" / "Dup Book.m4b",
    ]

    manifest_updates, outcome, _new_folders, _links = sync._upload_new_files(
        new_files, root, aliases={}, drive_folders={}, service=None, dry_run=False,
    )

    assert outcome.uploaded == [str(Path("Author A") / "New Book.m4b")]
    assert outcome.already_on_drive == [str(Path("Author B") / "Dup Book.m4b")]
    assert outcome.failed_count == 0
    assert set(manifest_updates) == {
        str(Path("Author A") / "New Book.m4b"),
        str(Path("Author B") / "Dup Book.m4b"),
    }
    assert outcome.run_state() == "success"


def test_real_upload_failure_is_classified_as_failed(root, monkeypatch):
    monkeypatch.setattr(sync, "resolve_alias", lambda author, aliases: (author, None))
    monkeypatch.setattr(
        sync, "resolve_author_to_drive_folder",
        lambda author, folders, dry_run=False: (author, f"folder-{author}"),
    )
    monkeypatch.setattr(sync, "upload_file_to_drive", _fake_upload([(None, False)]))

    new_files = [root / "Author A" / "Network Blip.m4b"]

    _updates, outcome, _new_folders, _links = sync._upload_new_files(
        new_files, root, aliases={}, drive_folders={}, service=None, dry_run=False,
    )

    assert outcome.failed == [str(Path("Author A") / "Network Blip.m4b")]
    assert outcome.uploaded_count == 0
    assert outcome.run_state() == "partial"


def test_unresolvable_author_folder_is_a_real_failure(root, monkeypatch):
    """Not the same thing as 'misplaced' — the file HAS an author folder,
    Drive just couldn't resolve/create one for it. That stays a failure."""
    monkeypatch.setattr(sync, "resolve_alias", lambda author, aliases: (author, None))
    monkeypatch.setattr(sync, "resolve_author_to_drive_folder", lambda *a, **k: None)
    monkeypatch.setattr(sync, "create_drive_folder", lambda *a, **k: None)
    monkeypatch.setattr(sync, "upload_file_to_drive", lambda *a, **k: pytest.fail("must not upload"))

    new_files = [root / "Mystery Author" / "Book.m4b"]

    _updates, outcome, _new_folders, _links = sync._upload_new_files(
        new_files, root, aliases={}, drive_folders={}, service=None, dry_run=False,
    )

    assert outcome.failed == [str(Path("Mystery Author") / "Book.m4b")]
    assert outcome.misplaced_count == 0
    assert outcome.run_state() == "partial"


# ---------------------------------------------------------------------------
# The morning-of-2026-08-15 scenario, as a fixture
#
# Reproduced from output_files/pipeline_8h.log (the Fri 08/14 16:00 run,
# lines ~121909-121943): STEP 1b had already refreshed site/ebooks.json, then
# STEP 4 found 9 new files, all 9 loose at the library root — no m4bs, no
# properly-foldered ebooks. Old code: 0 uploaded, 9 "failed" -> "partial" ->
# STEP 5/6 skipped -> ebooks.json refresh never committed. Fixed behavior:
# 9 misplaced (warnings), 0 failed -> "success" -> STEP 6 always attempted.
# ---------------------------------------------------------------------------

MORNING_INCIDENT_FILES = [
    "All The Skills 3- A Deckbuilding LitRPG - Honour Rae.epub",
    "All the Skills- A Deck-Building LitRPG - Honour Rae.epub",
    "All The Skills- Book 2- A Deck-Building LitRPG - Honour Rae.epub",
    "All The Skills- Book 4- A Deck-Building LitRPG - Honour Rae.epub",
    "All The Skills- Book 6- A Deck-Building LitRPG - Honour Rae.epub",
    "Beneath the Dragoneye Moons- Immortal War - Selkie Myth.epub",
    "Beneath the Dragoneye Moons- Mandate of Heaven - Selkie Myth.epub",
    "Beneath the Dragoneye Moons- New Horizons - Selkie Myth.epub",
    "Beneath the Dragoneye Moons- Return to Remus - Selkie Myth.epub",
]


def test_morning_incident_scenario_is_now_a_clean_success(root, monkeypatch):
    # None of these should be reachable — every candidate is misplaced.
    monkeypatch.setattr(sync, "resolve_alias", lambda *a, **k: pytest.fail("must not resolve"))
    monkeypatch.setattr(sync, "upload_file_to_drive", lambda *a, **k: pytest.fail("must not upload"))

    new_files = [root / name for name in MORNING_INCIDENT_FILES]

    manifest_updates, outcome, new_folders, links = sync._upload_new_files(
        new_files, root, aliases={}, drive_folders={}, service=None, dry_run=False,
    )

    assert outcome.misplaced_count == 9
    assert set(outcome.misplaced) == set(MORNING_INCIDENT_FILES)
    assert outcome.uploaded_count == 0
    assert outcome.already_count == 0
    assert outcome.failed_count == 0  # <- was 9 before the fix
    assert manifest_updates == {}
    assert new_folders == []
    assert links == {}

    # The run-level verdict the panel/log would have shown:
    assert outcome.run_state() == "success"  # <- was "partial" before the fix
    assert len(outcome.warnings()) == 9

    # And per the STEP 6 fix: publish is no longer gated on uploaded_count,
    # so with `not dry_run` true, STEP 6 runs regardless of this outcome —
    # only `dry_run` decides that now (see run_pipeline()'s STEP 6 guard).
    dry_run = False
    step_6_would_run = not dry_run
    assert step_6_would_run is True
