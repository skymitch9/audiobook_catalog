"""Regression tests for the 2026-08-16 pipeline safety/usability fixes in
`scripts/sync_to_drive.py`:

1. `--rebuild-only` — a tag-only fix on an already-uploaded book is invisible
   to STEP 2 ("new files to upload?"), so the normal pipeline exits at
   "Nothing to upload" before STEP 5 ever rebuilds the catalog. This flag
   runs STEP 5 (catalog rebuild) through STEP 6 (commit+push) directly,
   skipping sort/detect/upload entirely.
2. `--autostash` on `_auto_commit_and_push()`'s `git pull --rebase` — without
   it, any uncommitted file elsewhere in the tree makes the rebase fail, the
   code just warns and "attempts push anyway", and the catalog commit made
   above stays local-only while the run looks successful.

These exercise the CLI wiring and the exact subprocess argv used for the
pull — not the full rebuild (that needs the real library/Drive/git and is
covered by manual verification in docs/DONE.md), and not git plumbing itself.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from scripts import sync_to_drive as sync


# ---------------------------------------------------------------------------
# --rebuild-only CLI wiring
# ---------------------------------------------------------------------------


def _run_main_with_argv(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["sync_to_drive.py"] + argv)


def test_rebuild_only_conflicts_with_sort_only(monkeypatch, capsys):
    _run_main_with_argv(monkeypatch, ["--rebuild-only", "--sort-only"])
    with pytest.raises(SystemExit) as exc:
        sync.main()
    assert exc.value.code == 1
    assert "Cannot use --rebuild-only" in capsys.readouterr().out


def test_rebuild_only_conflicts_with_upload_only(monkeypatch, capsys):
    _run_main_with_argv(monkeypatch, ["--rebuild-only", "--upload-only"])
    with pytest.raises(SystemExit) as exc:
        sync.main()
    assert exc.value.code == 1
    assert "Cannot use --rebuild-only" in capsys.readouterr().out


def test_rebuild_only_conflicts_with_dry_run(monkeypatch, capsys):
    """app.main has no dry-run mode of its own, so the combination must fail
    loudly rather than silently ignoring one of the two flags."""
    _run_main_with_argv(monkeypatch, ["--rebuild-only", "--dry-run"])
    with pytest.raises(SystemExit) as exc:
        sync.main()
    assert exc.value.code == 1
    assert "no dry-run mode" in capsys.readouterr().out


def test_rebuild_only_alone_calls_run_rebuild_only_not_run_pipeline(monkeypatch):
    """--rebuild-only must route to the rebuild-only path, never touching
    sort/detect/upload's run_pipeline()."""
    _run_main_with_argv(monkeypatch, ["--rebuild-only"])
    calls = []
    monkeypatch.setattr(sync, "run_rebuild_only", lambda **k: calls.append(("rebuild_only", k)))
    monkeypatch.setattr(sync, "run_pipeline", lambda **k: calls.append(("pipeline", k)))
    sync.main()
    assert len(calls) == 1
    assert calls[0][0] == "rebuild_only"


def test_normal_invocation_still_calls_run_pipeline(monkeypatch):
    """Sanity check that wiring --rebuild-only did not disturb the default path."""
    _run_main_with_argv(monkeypatch, [])
    calls = []
    monkeypatch.setattr(sync, "run_rebuild_only", lambda **k: calls.append(("rebuild_only", k)))
    monkeypatch.setattr(sync, "run_pipeline", lambda **k: calls.append(("pipeline", k)))
    sync.main()
    assert len(calls) == 1
    assert calls[0][0] == "pipeline"


# ---------------------------------------------------------------------------
# --autostash on the auto-commit's pull
# ---------------------------------------------------------------------------


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_auto_commit_pull_uses_autostash(monkeypatch):
    """The exact argv matters: this is what stops an uncommitted file
    elsewhere in the tree from silently stranding a catalog commit."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["git", "status"]:
            return _FakeCompleted(stdout=" M site/catalog.csv\n")
        if cmd[:2] == ["git", "add"]:
            return _FakeCompleted()
        if cmd[:2] == ["git", "commit"]:
            return _FakeCompleted()
        if cmd[:2] == ["git", "pull"]:
            return _FakeCompleted()
        if cmd[:2] == ["git", "push"]:
            return _FakeCompleted()
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    sync._auto_commit_and_push()

    pull_calls = [c for c in calls if c[:2] == ["git", "pull"]]
    assert len(pull_calls) == 1
    assert pull_calls[0] == ["git", "pull", "--rebase", "--autostash"]


def test_auto_commit_git_add_allowlist_unchanged(monkeypatch):
    """The fix must not touch what gets staged — that allowlist is what
    makes it safe to run beside other uncommitted work in the repo."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["git", "status"]:
            return _FakeCompleted(stdout=" M site/catalog.csv\n")
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)
    sync._auto_commit_and_push()

    add_calls = [c for c in calls if c[:2] == ["git", "add"]]
    assert len(add_calls) == 1
    assert add_calls[0] == [
        "git", "add", "site/catalog.csv", "site/index.html", "site/ebooks.html",
        "site/covers_manifest.json", "site/covers-base.js",
        "site/stats.html", "site/chapters.json", "site/content_warnings.json",
        "site/additions_log.json", "site/ebooks_status.json", "author_drive_map.json",
    ]
    # site/ebooks.html joined _ALLOWLIST in 23761c3 ("ship site/ebooks.html
    # like index.html"); this assertion had not been updated and was failing on
    # main until the 2026-08-24 sanctity pass. Both this list and the
    # auto-promote.yml `allow=` regex must carry it — enforced now by
    # tests/test_allowlist_promote_parity.py.
    # ⚠️ site/ebooks.json is DELIBERATELY absent since 2026-08-17. The manifest
    # is gitignored (owner directive: "I don't want people scraping my books"
    # — this repo is PUBLIC, so a tracked manifest is world-readable at a raw
    # URL whatever the deployment serves). Naming an ignored path here would
    # make `git add` exit 1, the same noise site/covers/ used to make. If a
    # future session re-adds it, this assertion is the tripwire.
    assert "site/ebooks.json" not in add_calls[0]
    # ⚠️ site/audio_manifest.json is DELIBERATELY absent too (audio-player
    # phase 0b, STEP 5.9). Same reason, larger surface: it is the record of
    # which audiobook FILES are in the private estate-audio bucket, keyed on
    # file paths — a list of the household's books by name, 630 GB of them.
    # It is gitignored, so naming it here would make `git add` exit 1.
    # ⚠️ site/chapters.json IS here and stays here — chapter TITLES are public
    # by owner decision 2026-08-17 ("fine as is"), and the phase-0a start_sec
    # backfill ships through this very line. The asymmetry is on purpose.
    assert "site/audio_manifest.json" not in add_calls[0]
    assert "site/chapters.json" in add_calls[0]


def test_auto_commit_still_warns_and_attempts_push_on_genuine_rebase_conflict(monkeypatch, capsys):
    """--autostash removes the uncommitted-file false-positive, but a REAL
    rebase failure (e.g. an actual merge conflict) must still warn and fall
    through to attempting the push, unchanged from before."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["git", "status"]:
            return _FakeCompleted(stdout=" M site/catalog.csv\n")
        if cmd[:2] == ["git", "pull"]:
            return _FakeCompleted(returncode=1, stderr="CONFLICT (content): Merge conflict")
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)
    sync._auto_commit_and_push()

    out = capsys.readouterr().out
    assert "Pull --rebase --autostash failed" in out
    assert "Attempting push anyway" in out
    push_calls = [c for c in calls if c[:2] == ["git", "push"]]
    assert len(push_calls) == 1
