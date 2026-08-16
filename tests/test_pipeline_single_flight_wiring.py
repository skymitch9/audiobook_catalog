"""Tests for the single-flight lock wiring in scripts/sync_to_drive.py —
that every entry point (the normal run, --sort-only, --upload-only,
--rebuild-only, and the remote-trigger watcher, which runs this same
script) actually takes app/core/pipeline_lock.py's lock, per docs/info/
ROLES.md §1c/§1d.

These stub out `_run_pipeline_body` / `_run_rebuild_only_body` (the actual
sort/upload/git work, renamed out of `run_pipeline`/`run_rebuild_only` so
those two names could become thin lock-owning wrappers) — this file tests
the WIRING (does the lock get taken, does a block get reported and refused,
does --dry-run correctly skip it, does the scheduled trigger defer), not the
pipeline body itself (that's tests/test_upload_classification.py and
tests/test_rebuild_only_and_autostash.py).

`sync.pstatus` is always stubbed — the real one writes to Firestore when
credentials are configured on the machine running these tests, and these
tests must never touch the live pipeline_status the site actually reads.
"""

from __future__ import annotations

import os
import sys

import pytest

from app.core import pipeline_lock as pl
from app.core import pipeline_schedule as sched
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
    monkeypatch.setattr(sched, "DEFER_MARKER_PATH", tmp_path / "pipeline_defer.json")
    fake_status = _FakePStatus()
    monkeypatch.setattr(sync, "pstatus", fake_status)
    monkeypatch.setattr(sched, "pstatus", fake_status)
    yield fake_status


# ---------------------------------------------------------------------------
# run_pipeline(): --dry-run bypasses the lock entirely
# ---------------------------------------------------------------------------


def test_dry_run_never_touches_the_lock(monkeypatch):
    calls = []
    monkeypatch.setattr(sync, "_run_pipeline_body", lambda **k: calls.append(k))
    acquire_calls = []
    monkeypatch.setattr(pl, "acquire", lambda trigger: acquire_calls.append(trigger) or pytest.fail("must not lock"))

    sync.run_pipeline(dry_run=True, trigger="manual")

    assert len(calls) == 1
    assert calls[0]["dry_run"] is True
    assert acquire_calls == []
    assert not pl.LOCK_PATH.exists()


# ---------------------------------------------------------------------------
# run_pipeline(): non-scheduled triggers take the lock directly and fail
# loudly + immediately when it's held — no retry.
# ---------------------------------------------------------------------------


def test_manual_trigger_runs_body_under_a_real_lock(monkeypatch):
    seen_lock_held_during_body = {}

    def fake_body(**k):
        seen_lock_held_during_body["held"] = pl.LOCK_PATH.exists()

    monkeypatch.setattr(sync, "_run_pipeline_body", fake_body)
    sync.run_pipeline(dry_run=False, trigger="manual")

    assert seen_lock_held_during_body["held"] is True
    assert not pl.LOCK_PATH.exists()  # released afterward


def test_manual_trigger_blocked_reports_and_raises_without_running_body(monkeypatch, isolated_env):
    fake_status = isolated_env
    held = pl.acquire("scheduled")  # someone else already holds it
    try:
        calls = []
        monkeypatch.setattr(sync, "_run_pipeline_body", lambda **k: calls.append(k))

        with pytest.raises(pl.PipelineLockHeld) as exc:
            sync.run_pipeline(dry_run=False, trigger="manual")

        assert calls == []  # never ran
        assert exc.value.holder.pid == os.getpid()
        blocked = fake_status.calls_named("blocked_run")
        assert len(blocked) == 1
        assert blocked[0][1][0] == "manual"
    finally:
        held.release()


def test_lock_released_even_when_body_raises(monkeypatch):
    monkeypatch.setattr(sync, "_run_pipeline_body", lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        sync.run_pipeline(dry_run=False, trigger="manual")
    assert not pl.LOCK_PATH.exists()


# ---------------------------------------------------------------------------
# run_pipeline(): trigger="scheduled" routes through the defer state machine
# ---------------------------------------------------------------------------


def test_scheduled_trigger_uses_defer_not_immediate_failure(monkeypatch, isolated_env):
    held = pl.acquire("manual")  # blocked by someone else
    try:
        calls = []
        monkeypatch.setattr(sync, "_run_pipeline_body", lambda **k: calls.append(k))
        # Force the defer loop to resolve fast instead of waiting real minutes.
        monkeypatch.setattr(sched, "DEFER_WINDOW_MIN", 0.0)

        # Must NOT raise PipelineLockHeld — scheduled defers, never fails immediately.
        sync.run_pipeline(dry_run=False, trigger="scheduled")

        assert calls == []  # the window elapsed instantly -> abandoned, body never ran
        assert isolated_env.calls_named("deferral_abandoned")
    finally:
        held.release()


def test_scheduled_trigger_runs_once_lock_clears(monkeypatch, isolated_env):
    attempts = {"n": 0}
    real_acquire = pl.acquire

    def flaky_acquire(trigger):
        attempts["n"] += 1
        if attempts["n"] == 1:
            held = real_acquire("other")
            held.release()  # free it right back up — just needed ONE refusal
            raise pl.PipelineLockHeld(
                pl.LockHolder(
                    pid=999999,
                    host="x",
                    trigger="manual",
                    started_at="2026-01-01T00:00:00+00:00",
                )
            )
        return real_acquire(trigger)

    monkeypatch.setattr(pl, "acquire", flaky_acquire)
    monkeypatch.setattr(sched, "acquire", flaky_acquire)
    monkeypatch.setattr(sched.time, "sleep", lambda s: None)
    calls = []
    monkeypatch.setattr(sync, "_run_pipeline_body", lambda **k: calls.append(k))

    sync.run_pipeline(dry_run=False, trigger="scheduled")

    assert len(calls) == 1


# ---------------------------------------------------------------------------
# run_rebuild_only(): always fails loudly+immediately, even if someone passes
# trigger="scheduled" by mistake — the defer rule is specifically for the
# plain scheduled run, scripts/sync_pipeline_8h.bat never calls --rebuild-only.
# ---------------------------------------------------------------------------


def test_rebuild_only_never_defers_even_with_scheduled_trigger(monkeypatch, isolated_env):
    fake_status = isolated_env
    held = pl.acquire("manual")
    try:
        calls = []
        monkeypatch.setattr(sync, "_run_rebuild_only_body", lambda **k: calls.append(k))

        with pytest.raises(pl.PipelineLockHeld):
            sync.run_rebuild_only(trigger="scheduled")

        assert calls == []
        assert fake_status.calls_named("blocked_run")
    finally:
        held.release()


def test_rebuild_only_runs_under_lock_when_free(monkeypatch):
    seen = {}
    monkeypatch.setattr(sync, "_run_rebuild_only_body", lambda **k: seen.update(held=pl.LOCK_PATH.exists()))
    sync.run_rebuild_only(trigger="manual-rebuild")
    assert seen["held"] is True
    assert not pl.LOCK_PATH.exists()


# ---------------------------------------------------------------------------
# main(): PIPELINE_TRIGGER default is "manual" (not "scheduled") for the
# plain run — "scheduled" is now reserved for scripts/sync_pipeline_8h.bat,
# which sets it explicitly. A blocked run exits nonzero without calling
# pstatus.fail_run() (that would clobber the blocked_run() message).
# ---------------------------------------------------------------------------


def test_main_default_trigger_is_manual_not_scheduled(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["sync_to_drive.py"])
    monkeypatch.delenv("PIPELINE_TRIGGER", raising=False)
    captured = {}
    monkeypatch.setattr(sync, "run_pipeline", lambda **k: captured.update(k))
    monkeypatch.setattr(sync, "run_rebuild_only", lambda **k: None)
    sync.main()
    assert captured["trigger"] == "manual"


def test_main_blocked_run_exits_nonzero_without_calling_fail_run(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["sync_to_drive.py"])
    fail_run_calls = []
    monkeypatch.setattr(sync.pstatus, "fail_run", lambda e: fail_run_calls.append(e))

    def fake_run_pipeline(**k):
        raise pl.PipelineLockHeld(pl.LockHolder(pid=1, host="h", trigger="manual", started_at="2026-01-01T00:00:00+00:00"))

    monkeypatch.setattr(sync, "run_pipeline", fake_run_pipeline)

    with pytest.raises(SystemExit) as exc:
        sync.main()
    assert exc.value.code == 1
    assert fail_run_calls == []


def test_main_genuine_crash_still_calls_fail_run(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["sync_to_drive.py"])
    fail_run_calls = []
    monkeypatch.setattr(sync.pstatus, "fail_run", lambda e: fail_run_calls.append(e))
    monkeypatch.setattr(sync, "run_pipeline", lambda **k: (_ for _ in ()).throw(RuntimeError("real crash")))

    with pytest.raises(RuntimeError):
        sync.main()
    assert len(fail_run_calls) == 1
