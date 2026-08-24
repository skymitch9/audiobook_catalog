"""Regression tests for the 2026-08-24 pipeline sanctity fixes in
`scripts/sync_to_drive.py` (audit:
`docs/info/openaudible-pipeline-sanctity-2026-08-24.md`).

Each test pins the NEW behaviour and would have FAILED against the old code:

* F1 — a failed ``git push`` must move the run state (was: WARN + return, panel
  stayed green while the commit was local-only and prod fell behind).
* F2 — an ambiguous fuzzy author match must NOT call ``input()`` in a headless
  run (was: ``EOFError`` aborted the whole upload batch, or a hang held the
  lock).
* F3 — a corrupt/truncated manifest must degrade to EMPTY, and writes must be
  atomic so a crash mid-write cannot truncate the file (was: bare ``json.load``
  crashed every future run; plain ``open`` + ``dump`` could truncate).

These exercise the real functions with subprocess/stdin/filesystem seams
stubbed — not git plumbing, a live console, or a real Drive.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from scripts import sync_to_drive as sync


class _Fake:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# F1 — a failed push marks the run failed/degraded, a success reports success
# ---------------------------------------------------------------------------


def _git_fake(*, status_out=" M site/catalog.csv\n", commit_rc=0, push_rc=0,
              unpushed_seq=("0",)):
    """Build a fake ``subprocess.run`` for the git calls _auto_commit_and_push
    makes. ``unpushed_seq`` feeds successive ``git rev-list --count`` reads."""
    box = {"i": 0}

    def fake_run(cmd, **kwargs):
        key = cmd[:2]
        if key == ["git", "status"]:
            return _Fake(stdout=status_out)
        if key == ["git", "add"]:
            return _Fake()
        if key == ["git", "commit"]:
            return _Fake(returncode=commit_rc,
                         stderr="commit boom" if commit_rc else "")
        if key == ["git", "pull"]:
            return _Fake()
        if key == ["git", "push"]:
            return _Fake(returncode=push_rc,
                         stderr="network boom" if push_rc else "")
        if key == ["git", "rev-list"]:
            i = min(box["i"], len(unpushed_seq) - 1)
            box["i"] += 1
            return _Fake(stdout=unpushed_seq[i] + "\n")
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    return fake_run


def test_push_failure_returns_false(monkeypatch):
    """OLD: a failed push printed a WARN and returned None → the caller could
    not tell, and finish_run stayed 'success'. NEW: returns False."""
    monkeypatch.setattr(subprocess, "run", _git_fake(push_rc=1))
    assert sync._auto_commit_and_push() is False


def test_push_success_returns_true(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _git_fake(push_rc=0))
    assert sync._auto_commit_and_push() is True


def test_commit_failure_returns_false(monkeypatch):
    """A commit that fails after there WERE changes never published them."""
    monkeypatch.setattr(subprocess, "run", _git_fake(commit_rc=1))
    assert sync._auto_commit_and_push() is False


def test_no_changes_and_nothing_stranded_returns_true(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        _git_fake(status_out="", unpushed_seq=("0",)),
    )
    assert sync._auto_commit_and_push() is True


def test_no_changes_but_stranded_commit_is_retried(monkeypatch):
    """The idle self-heal: nothing new to commit, but a prior run stranded a
    commit locally. Retry pushes it; rev-list then reads 0 → True."""
    fake = _git_fake(status_out="", unpushed_seq=("2", "0"))
    calls = []
    orig = fake

    def spy(cmd, **kwargs):
        calls.append(cmd[:2])
        return orig(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    assert sync._auto_commit_and_push() is True
    assert ["git", "push"] in calls  # it actually retried the push


def test_stranded_commit_that_will_not_push_returns_false(monkeypatch):
    """Retry attempted, but the commit is still ahead of origin afterwards."""
    monkeypatch.setattr(
        subprocess, "run",
        _git_fake(status_out="", push_rc=1, unpushed_seq=("2", "2")),
    )
    assert sync._auto_commit_and_push() is False


def test_publish_step_reports_partial_on_push_failure(monkeypatch):
    """End-to-end F1 wiring: the standalone 'publish' step must land the run
    card as 'partial' (not green) when the push fails.

    OLD: _run_step_body always called finish_run('success'). NEW: it forwards
    the handler's returned state, and _step_publish returns 'partial'."""
    monkeypatch.setattr(sync, "_auto_commit_and_push", lambda: False)
    monkeypatch.setattr(sync, "_push_estate_index", lambda *a, **k: None)

    import app.tools.fetch_content_warnings as fcw
    monkeypatch.setattr(fcw, "fulfill_requests", lambda *a, **k: None)

    finished = {}
    monkeypatch.setattr(sync.pstatus, "start_step_run", lambda *a, **k: "rid")
    monkeypatch.setattr(sync.pstatus, "status_note", lambda *a, **k: "")
    monkeypatch.setattr(sync.pstatus, "finish_run",
                        lambda state="success", error=None: finished.update(
                            state=state, error=error))

    sync._run_step_body("publish", "manual-step")
    assert finished["state"] == "partial"


def test_publish_step_reports_success_on_push_ok(monkeypatch):
    monkeypatch.setattr(sync, "_auto_commit_and_push", lambda: True)
    monkeypatch.setattr(sync, "_push_estate_index", lambda *a, **k: None)
    import app.tools.fetch_content_warnings as fcw
    monkeypatch.setattr(fcw, "fulfill_requests", lambda *a, **k: None)

    finished = {}
    monkeypatch.setattr(sync.pstatus, "start_step_run", lambda *a, **k: "rid")
    monkeypatch.setattr(sync.pstatus, "status_note", lambda *a, **k: "")
    monkeypatch.setattr(sync.pstatus, "finish_run",
                        lambda state="success", error=None: finished.update(
                            state=state, error=error))

    sync._run_step_body("publish", "manual-step")
    assert finished["state"] == "success"


# ---------------------------------------------------------------------------
# F2 — no blocking input() in a headless run
# ---------------------------------------------------------------------------


def test_ambiguous_author_does_not_prompt_when_headless(monkeypatch):
    """OLD: a fuzzy match in the 80-91 band called input(), which raises
    EOFError in a headless run and aborts the whole batch. NEW: when stdin is
    not a TTY, it skips the prompt and returns None (→ new folder, safe path).

    Under pytest stdin is already not a TTY; we also make input() explode so
    the test fails loudly if the guard is ever removed."""
    def boom(*a, **k):
        raise AssertionError("input() must not be called in a headless run")

    monkeypatch.setattr("builtins.input", boom)

    # thefuzz's token_sort_ratio: force a mid-band (80-91) score and no
    # Claude key so we reach step 6 deterministically.
    monkeypatch.setattr(sync, "CLAUDE_API_KEY", None)
    from thefuzz import fuzz
    monkeypatch.setattr(fuzz, "token_sort_ratio", lambda a, b: 85)

    drive_folders = {"Robert Jordamn": "folder-xyz"}  # deliberate typo → fuzzy
    result = sync.resolve_author_to_drive_folder("Robert Jordan", drive_folders)
    assert result is None  # falls through to "create a new folder"


def test_ambiguous_author_prompts_when_interactive(monkeypatch):
    """Symmetry check: with a TTY attached, the confirm prompt IS used."""
    monkeypatch.setattr(sync.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    monkeypatch.setattr(sync, "CLAUDE_API_KEY", None)
    from thefuzz import fuzz
    monkeypatch.setattr(fuzz, "token_sort_ratio", lambda a, b: 85)

    drive_folders = {"Robert Jordamn": "folder-xyz"}
    result = sync.resolve_author_to_drive_folder("Robert Jordan", drive_folders)
    assert result == ("Robert Jordamn", "folder-xyz")


# ---------------------------------------------------------------------------
# F3 — corrupt manifest degrades to empty; writes are atomic
# ---------------------------------------------------------------------------


def test_load_manifest_tolerates_corrupt_file(monkeypatch, tmp_path):
    """OLD: bare json.load raised JSONDecodeError → every future run halted.
    NEW: an unparseable manifest loads as {} with a WARN."""
    bad = tmp_path / "upload_manifest.json"
    bad.write_text('{"a/b.m4b": {"drive_file_id": "x"', encoding="utf-8")  # truncated
    monkeypatch.setattr(sync, "MANIFEST_PATH", bad)
    assert sync.load_manifest() == {}


def test_load_manifest_missing_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(sync, "MANIFEST_PATH", tmp_path / "nope.json")
    assert sync.load_manifest() == {}


def test_load_manifest_wrong_shape_is_empty(monkeypatch, tmp_path):
    p = tmp_path / "upload_manifest.json"
    p.write_text('["not", "a", "dict"]', encoding="utf-8")
    monkeypatch.setattr(sync, "MANIFEST_PATH", p)
    assert sync.load_manifest() == {}


def test_save_manifest_roundtrips(monkeypatch, tmp_path):
    p = tmp_path / "upload_manifest.json"
    monkeypatch.setattr(sync, "MANIFEST_PATH", p)
    data = {"Author/Book.m4b": {"drive_file_id": "abc", "uploaded_at": "t"}}
    sync.save_manifest(data)
    assert json.loads(p.read_text(encoding="utf-8")) == data


def test_atomic_write_leaves_old_file_intact_on_crash(monkeypatch, tmp_path):
    """OLD: plain open('w') truncates the target before writing → a crash
    mid-dump leaves a truncated, unparseable file. NEW: the temp+os.replace
    swap means a crash during the write leaves the PREVIOUS file untouched."""
    p = tmp_path / "upload_manifest.json"
    good = {"kept": {"drive_file_id": "old"}}
    p.write_text(json.dumps(good), encoding="utf-8")

    # Make json.dump blow up partway through the write.
    def exploding_dump(*a, **k):
        raise RuntimeError("simulated crash mid-write")

    monkeypatch.setattr(sync.json, "dump", exploding_dump)
    with pytest.raises(RuntimeError):
        sync._atomic_write_json(p, {"new": {"drive_file_id": "new"}})

    # The original file must still be fully readable and unchanged.
    assert json.loads(p.read_text(encoding="utf-8")) == good
    # And no stray .tmp files left behind.
    assert list(tmp_path.glob("*.tmp")) == []
