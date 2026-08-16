"""Tests for the fine-grained manual pipeline step controls in
scripts/sync_to_drive.py (owner ask 2026-08-16, catalog-platform /status
Operations section): STEP_INFO, run_step()'s lock wiring, --step CLI
wiring, and each _step_*() body with its Drive/git/filesystem calls
monkeypatched out.

Same isolation idiom as tests/test_pipeline_single_flight_wiring.py: real
lock file redirected into tmp_path, pstatus always stubbed so nothing here
ever touches the live Firestore project.
"""

from __future__ import annotations

import sys

import pytest

from app.core import pipeline_lock as pl
from scripts import sync_to_drive as sync


class _FakePStatus:
    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name):
        def record(*a, **k):
            self.calls.append((name, a, k))

        return record

    def calls_named(self, name):
        return [c for c in self.calls if c[0] == name]


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "LOCK_PATH", tmp_path / "pipeline.lock")
    fake_status = _FakePStatus()
    monkeypatch.setattr(sync, "pstatus", fake_status)
    yield fake_status


# ---------------------------------------------------------------------------
# STEP_INFO — the classification catalog-platform's admin.js/ops.ts must
# mirror. Pinned here so a change is a deliberate, visible diff.
# ---------------------------------------------------------------------------


def test_step_info_has_exactly_the_seven_pipeline_status_steps():
    from app import pipeline_status as pstatus_real

    assert set(sync.STEP_INFO.keys()) == {k for k, _label in pstatus_real.STEPS}
    assert sync.STEP_CHOICES == tuple(sync.STEP_INFO.keys())


def test_step_info_classification_matches_the_owner_brief():
    kinds = {k: v["kind"] for k, v in sync.STEP_INFO.items()}
    assert kinds == {
        "audit": "read-only",
        "sort": "mutating",
        "detect": "read-only",
        "folders": "mutating",
        "upload": "mutating",
        "catalog": "publishing",
        "publish": "publishing",
    }


# ---------------------------------------------------------------------------
# run_step(): lock wiring — the actual safety guarantee.
# ---------------------------------------------------------------------------


def test_run_step_rejects_unknown_step():
    with pytest.raises(ValueError, match="unknown pipeline step"):
        sync.run_step("not-a-real-step")


def test_run_step_runs_body_under_a_real_lock(monkeypatch):
    seen = {}

    def fake_body(step, trigger):
        seen["held"] = pl.LOCK_PATH.exists()
        seen["step"] = step
        seen["trigger"] = trigger

    monkeypatch.setattr(sync, "_run_step_body", fake_body)
    sync.run_step("audit", trigger="manual-step:audit")

    assert seen == {"held": True, "step": "audit", "trigger": "manual-step:audit"}
    assert not pl.LOCK_PATH.exists()  # released afterward


def test_run_step_blocked_reports_and_raises_without_running_body(monkeypatch, isolated_env):
    fake_status = isolated_env
    held = pl.acquire("scheduled")  # someone else already holds it
    try:
        calls = []
        monkeypatch.setattr(sync, "_run_step_body", lambda step, trigger: calls.append(step))

        with pytest.raises(pl.PipelineLockHeld):
            sync.run_step("upload", trigger="manual-step:upload")

        assert calls == []
        blocked = fake_status.calls_named("blocked_run")
        assert len(blocked) == 1
        assert blocked[0][1][0] == "manual-step:upload"
    finally:
        held.release()


def test_run_step_never_defers_even_while_scheduled_run_holds_lock(monkeypatch):
    """A manual step must fail immediately, exactly like run_pipeline()'s
    non-scheduled path and run_rebuild_only() — never wait up to 2h like the
    scheduled trigger's defer state machine."""
    held = pl.acquire("scheduled")
    try:
        with pytest.raises(pl.PipelineLockHeld):
            sync.run_step("detect")
    finally:
        held.release()


def test_run_step_releases_lock_even_when_body_raises(monkeypatch):
    monkeypatch.setattr(sync, "_run_step_body", lambda step, trigger: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        sync.run_step("sort")
    assert not pl.LOCK_PATH.exists()


def test_run_step_two_steps_cannot_overlap():
    """The core promise: taking the SAME lock as every other run means two
    manual steps (or a step and anything else) can never run concurrently."""
    held = pl.acquire("manual-step:sort")
    try:
        with pytest.raises(pl.PipelineLockHeld):
            sync.run_step("upload")
    finally:
        held.release()


# ---------------------------------------------------------------------------
# _run_step_body(): success closes the run; failure propagates and never
# calls finish_run("success").
# ---------------------------------------------------------------------------


def test_run_step_body_success_calls_finish_run_success(monkeypatch, isolated_env):
    fake_status = isolated_env
    monkeypatch.setattr(sync, "_STEP_HANDLERS", {**sync._STEP_HANDLERS, "audit": lambda: None})
    sync._run_step_body("audit", "manual-step:audit")

    assert fake_status.calls_named("start_step_run")
    started = fake_status.calls_named("start_step_run")[0]
    assert started[1] == ("audit", "Purchase audit", "manual-step:audit")
    finishes = fake_status.calls_named("finish_run")
    assert finishes and finishes[0][1] == ("success",)


def test_run_step_body_failure_never_calls_finish_run_success(monkeypatch, isolated_env):
    fake_status = isolated_env

    def boom():
        raise RuntimeError("upload failed")

    monkeypatch.setattr(sync, "_STEP_HANDLERS", {**sync._STEP_HANDLERS, "upload": boom})
    with pytest.raises(RuntimeError, match="upload failed"):
        sync._run_step_body("upload", "manual-step:upload")

    assert fake_status.calls_named("finish_run") == []


# ---------------------------------------------------------------------------
# --step CLI wiring
# ---------------------------------------------------------------------------


def _run_main_with_argv(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["sync_to_drive.py"] + argv)


def test_step_cli_routes_to_run_step(monkeypatch):
    _run_main_with_argv(monkeypatch, ["--step", "detect"])
    calls = []
    monkeypatch.setattr(sync, "run_step", lambda step, trigger: calls.append((step, trigger)))
    monkeypatch.setattr(sync, "run_pipeline", lambda **k: pytest.fail("must not run the full pipeline"))
    monkeypatch.setattr(sync, "run_rebuild_only", lambda **k: pytest.fail("must not run rebuild-only"))
    sync.main()
    assert calls == [("detect", "manual-step:detect")]


def test_step_cli_rejects_invalid_choice(monkeypatch, capsys):
    _run_main_with_argv(monkeypatch, ["--step", "bogus"])
    with pytest.raises(SystemExit):
        sync.main()


@pytest.mark.parametrize("flag", ["--sort-only", "--upload-only", "--rebuild-only", "--dry-run"])
def test_step_cli_conflicts_with_other_run_modes(monkeypatch, capsys, flag):
    _run_main_with_argv(monkeypatch, ["--step", "audit", flag])
    with pytest.raises(SystemExit) as exc:
        sync.main()
    assert exc.value.code == 1
    assert "--step cannot be combined" in capsys.readouterr().out


def test_step_cli_blocked_run_exits_nonzero_without_calling_fail_run(monkeypatch):
    _run_main_with_argv(monkeypatch, ["--step", "upload"])
    fail_run_calls = []
    monkeypatch.setattr(sync.pstatus, "fail_run", lambda e: fail_run_calls.append(e))

    def fake_run_step(step, trigger):
        raise pl.PipelineLockHeld(pl.LockHolder(pid=1, host="h", trigger="manual", started_at="2026-01-01T00:00:00+00:00"))

    monkeypatch.setattr(sync, "run_step", fake_run_step)
    with pytest.raises(SystemExit) as exc:
        sync.main()
    assert exc.value.code == 1
    assert fail_run_calls == []


# ---------------------------------------------------------------------------
# Individual step bodies — Drive/git calls monkeypatched out.
# ---------------------------------------------------------------------------


def test_step_audit_calls_run_audit(monkeypatch, isolated_env):
    calls = []
    import app.tools.audit_new_purchases as audit_mod
    monkeypatch.setattr(audit_mod, "run_audit", lambda: calls.append("audited"))
    sync._step_audit()
    assert calls == ["audited"]
    assert isolated_env.calls_named("step_detail")


def test_step_sort_reports_counts(monkeypatch, isolated_env):
    monkeypatch.setattr(sync, "sort_books", lambda dry_run=False: ["a", "b"])
    monkeypatch.setattr(sync, "sort_companion_files", lambda dry_run=False: ["c"])
    sync._step_sort()
    detail = isolated_env.calls_named("step_detail")[0]
    assert detail[1] == ("sort", "2 sorted, 1 companions filed")
    summary = isolated_env.calls_named("set_summary")[0]
    assert summary[2] == {"sorted": 2, "companionsFiled": 1}


def test_step_detect_reports_count(monkeypatch, isolated_env):
    monkeypatch.setattr(sync, "load_manifest", lambda: {})
    monkeypatch.setattr(sync, "detect_new_books", lambda manifest: ["x.m4b", "y.m4b"])
    sync._step_detect()
    detail = isolated_env.calls_named("step_detail")[0]
    assert detail[1] == ("detect", "2 to upload")
    summary = isolated_env.calls_named("set_summary")[0]
    assert summary[2] == {"toUpload": 2}


def test_step_folders_raises_when_drive_auth_fails(monkeypatch):
    from scripts import drive_auth
    monkeypatch.setattr(drive_auth, "build_drive_service", lambda: None)
    with pytest.raises(RuntimeError, match="Google Drive auth failed"):
        sync._step_folders()


def test_step_folders_fetches_and_caches(monkeypatch, isolated_env):
    from scripts import drive_auth
    monkeypatch.setattr(drive_auth, "build_drive_service", lambda: object())
    monkeypatch.setattr(sync, "fetch_all_drive_folders", lambda service: {"Author A": "id1", "Author B": "id2"})
    saved = {}
    monkeypatch.setattr(sync, "save_drive_folders_cache", lambda folders: saved.update(folders))
    sync._step_folders()
    assert saved == {"Author A": "id1", "Author B": "id2"}
    detail = isolated_env.calls_named("step_detail")[0]
    assert detail[1] == ("folders", "2 folders")


def test_step_upload_no_new_files_is_a_clean_no_op(monkeypatch, isolated_env):
    monkeypatch.setattr(sync, "load_manifest", lambda: {})
    monkeypatch.setattr(sync, "detect_new_books", lambda manifest: [])
    sync._step_upload()
    summary_calls = isolated_env.calls_named("set_summary")
    assert any(c[2].get("idle") is True for c in summary_calls)
    # Never even tries to auth against Drive when there's nothing to upload.
    details = [c[1] for c in isolated_env.calls_named("step_detail")]
    assert ("upload", "nothing to upload (0 new files)") in details


def test_step_upload_raises_when_drive_auth_fails(monkeypatch):
    monkeypatch.setattr(sync, "load_manifest", lambda: {})
    monkeypatch.setattr(sync, "detect_new_books", lambda manifest: ["a.m4b"])
    from scripts import drive_auth
    monkeypatch.setattr(drive_auth, "build_drive_service", lambda: None)
    with pytest.raises(RuntimeError, match="Google Drive auth failed"):
        sync._step_upload()


def test_step_upload_success_path(monkeypatch, isolated_env):
    monkeypatch.setattr(sync, "load_manifest", lambda: {})
    monkeypatch.setattr(sync, "detect_new_books", lambda manifest: ["Author A/book.m4b"])
    from scripts import drive_auth
    monkeypatch.setattr(drive_auth, "build_drive_service", lambda: object())
    monkeypatch.setattr(sync, "load_drive_folders_cache", lambda: {"Author A": "id1"})
    monkeypatch.setattr(sync, "load_author_aliases", lambda: {})

    outcome = sync.UploadOutcome(uploaded=["Author A/book.m4b"])
    monkeypatch.setattr(
        sync, "_upload_new_files",
        lambda new_files, root, aliases, folders, service, dry_run=False: ({}, outcome, [], {}),
    )
    saved_manifest = {}
    monkeypatch.setattr(sync, "save_manifest", lambda m: saved_manifest.update(m))
    monkeypatch.setattr(sync, "save_drive_folders_cache", lambda f: None)
    monkeypatch.setattr(sync, "persist_author_links", lambda links: 0)

    sync._step_upload()
    summary = isolated_env.calls_named("set_summary")[-1]
    assert summary[2]["uploaded"] == 1
    assert summary[2]["failed"] == 0


def test_step_upload_raises_on_real_failures(monkeypatch):
    monkeypatch.setattr(sync, "load_manifest", lambda: {})
    monkeypatch.setattr(sync, "detect_new_books", lambda manifest: ["Author A/book.m4b"])
    from scripts import drive_auth
    monkeypatch.setattr(drive_auth, "build_drive_service", lambda: object())
    monkeypatch.setattr(sync, "load_drive_folders_cache", lambda: {"Author A": "id1"})
    monkeypatch.setattr(sync, "load_author_aliases", lambda: {})

    outcome = sync.UploadOutcome(failed=["Author A/book.m4b"])
    monkeypatch.setattr(
        sync, "_upload_new_files",
        lambda new_files, root, aliases, folders, service, dry_run=False: ({}, outcome, [], {}),
    )
    monkeypatch.setattr(sync, "save_manifest", lambda m: None)
    monkeypatch.setattr(sync, "save_drive_folders_cache", lambda f: None)
    monkeypatch.setattr(sync, "persist_author_links", lambda links: 0)

    with pytest.raises(RuntimeError, match="failed to upload"):
        sync._step_upload()


def test_step_publish_calls_auto_commit_and_fulfill_requests(monkeypatch):
    calls = []
    monkeypatch.setattr(sync, "_auto_commit_and_push", lambda: calls.append("committed"))
    import app.tools.fetch_content_warnings as cw_mod
    monkeypatch.setattr(cw_mod, "fulfill_requests", lambda: calls.append("fulfilled"))
    sync._step_publish()
    assert calls == ["committed", "fulfilled"]


def test_step_publish_tolerates_fulfill_requests_failure(monkeypatch):
    monkeypatch.setattr(sync, "_auto_commit_and_push", lambda: None)
    import app.tools.fetch_content_warnings as cw_mod

    def boom():
        raise RuntimeError("nope")

    monkeypatch.setattr(cw_mod, "fulfill_requests", boom)
    sync._step_publish()  # must not raise
