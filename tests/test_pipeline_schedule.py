"""Tests for app/core/pipeline_schedule.py — the "DEFER, DON'T SKIP" state
machine for the 8-hourly scheduled trigger (docs/info/ROLES.md §1d, owner's
exact words: *"make sure if an 8 hour run is stopped while a pipeline is
running that 8 hour run can be delayed up to 2 hours without breaking the
next auto run. that way we don't skip the auto runs."*).

Every test:
  - redirects DEFER_MARKER_PATH into a pytest tmp_path (never the real
    output_files/pipeline_defer.json),
  - stubs `pipeline_schedule.pstatus` with a call-recording fake — the real
    module talks to Firestore when credentials are configured on the
    machine, and a test must never write to the LIVE pipeline_status the
    site actually reads (this bit a real run during development: an
    unstubbed real-CLI exercise briefly overwrote the live status doc, and
    had to be restored by hand from pipeline_runs history),
  - never actually sleeps for real minutes: `time.sleep` is stubbed to a
    fast no-op recorder, and the "abandon" tests shrink DEFER_WINDOW_MIN
    instead of waiting out the real 2 hours.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from app.core import pipeline_lock as pl
from app.core import pipeline_schedule as sched


class _FakePStatus:
    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name):
        def record(*a, **k):
            self.calls.append((name, a, k))

        return record

    def calls_named(self, name):
        return [c for c in self.calls if c[0] == name]


class _FakeLock:
    """Stand-in for a PipelineLock: supports both direct .release() and the
    `with ... as lock:` protocol, same as the real one."""

    def __init__(self):
        self.released = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()

    def release(self):
        self.released = True


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setattr(sched, "DEFER_MARKER_PATH", tmp_path / "pipeline_defer.json")
    monkeypatch.setattr(pl, "LOCK_PATH", tmp_path / "pipeline.lock")
    fake_status = _FakePStatus()
    monkeypatch.setattr(sched, "pstatus", fake_status)
    sleep_calls: list[float] = []
    monkeypatch.setattr(sched.time, "sleep", lambda s: sleep_calls.append(s))
    yield fake_status, sleep_calls


def _holder(pid=424242, trigger="manual"):
    return pl.LockHolder(pid=pid, host="other-host", trigger=trigger, started_at=_iso_now())


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Constants match the owner's exact numbers
# ---------------------------------------------------------------------------


def test_defer_window_is_owner_specified_120_minutes():
    assert sched.DEFER_WINDOW_MIN == 120.0


def test_retry_interval_is_inside_owners_10_to_15_minute_band():
    assert 10.0 <= sched.RETRY_INTERVAL_MIN <= 15.0


def test_worst_case_delay_fits_inside_one_8h_schedule_cycle():
    """This is WHY a deferral never drifts the 8h grid: the worst-case time
    to resolve one blocked slot (stale-lock reclaim ceiling + the full 2h
    defer window) must stay comfortably under 8 hours, so this process
    always finishes — run or abandon — long before the next scheduled
    trigger fires. Nothing here ever needs to touch Task Scheduler."""
    worst_case_hours = pl.STALE_LOCK_HOURS + (sched.DEFER_WINDOW_MIN / 60.0)
    assert worst_case_hours < 8.0


# ---------------------------------------------------------------------------
# Lock free -> runs immediately, no deferral at all
# ---------------------------------------------------------------------------


def test_lock_free_runs_immediately(monkeypatch, isolated_env):
    fake_status, sleep_calls = isolated_env
    monkeypatch.setattr(sched, "acquire", lambda trigger: _FakeLock())
    ran = []
    result = sched.run_with_defer(lambda: ran.append(1))
    assert result == "ran"
    assert ran == [1]
    assert sleep_calls == []
    assert not sched.DEFER_MARKER_PATH.exists()


# ---------------------------------------------------------------------------
# Lock held, then clears within the window -> defers, then runs exactly once
# ---------------------------------------------------------------------------


def test_lock_clears_within_window_runs_once(monkeypatch, isolated_env):
    fake_status, sleep_calls = isolated_env
    attempts = {"n": 0}

    def fake_acquire(trigger):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise pl.PipelineLockHeld(_holder())
        return _FakeLock()

    monkeypatch.setattr(sched, "acquire", fake_acquire)
    ran = []
    result = sched.run_with_defer(lambda: ran.append(1))

    assert result == "ran"
    assert ran == [1]  # exactly once
    assert not sched.DEFER_MARKER_PATH.exists()  # cleared after success
    # Retried at the owner's 10-15 min cadence (in seconds).
    assert sleep_calls
    for s in sleep_calls:
        assert 0 < s <= sched.RETRY_INTERVAL_MIN * 60
    # Live heartbeats were published while waiting.
    assert len(fake_status.calls_named("deferring")) >= 2


# ---------------------------------------------------------------------------
# 2h window elapses still blocked -> abandon, loud, never silent
# ---------------------------------------------------------------------------


def test_defer_window_elapsed_abandons_and_reports_loudly(monkeypatch, isolated_env):
    fake_status, sleep_calls = isolated_env
    monkeypatch.setattr(sched, "DEFER_WINDOW_MIN", 0.0)  # elapsed the instant it's checked
    monkeypatch.setattr(sched, "acquire", lambda trigger: (_ for _ in ()).throw(pl.PipelineLockHeld(_holder())))

    result = sched.run_with_defer(lambda: pytest.fail("run_fn must never be called"))

    assert result == "abandoned"
    assert not sched.DEFER_MARKER_PATH.exists()
    abandoned_calls = fake_status.calls_named("deferral_abandoned")
    assert len(abandoned_calls) == 1
    _, args, kwargs = abandoned_calls[0]
    assert args[0] == "scheduled"


# ---------------------------------------------------------------------------
# Deferrals must not stack: a second invocation collapses into an existing,
# still-live deferral rather than starting its own countdown.
# ---------------------------------------------------------------------------


def test_second_invocation_collapses_into_existing_live_deferral(monkeypatch, isolated_env):
    fake_status, sleep_calls = isolated_env
    since = _iso_now()
    sched._write_marker(sched.DeferralMarker(deferring_since=since, pid=os.getpid(), host="h", attempts=3))

    monkeypatch.setattr(sched, "acquire", lambda trigger: (_ for _ in ()).throw(pl.PipelineLockHeld(_holder())))

    result = sched.run_with_defer(lambda: pytest.fail("run_fn must never be called"))

    assert result == "collapsed"
    assert sleep_calls == []  # never entered its own wait loop
    # The marker is untouched — still owned by the original deferral.
    marker = sched._read_marker()
    assert marker.deferring_since == since
    assert marker.attempts == 3


def test_two_scheduled_invocations_never_both_run(monkeypatch, isolated_env):
    """End-to-end version of the non-stacking rule: simulate slot A already
    deferring (live marker), then have slot B fire. Only slot A's owner may
    ever run — slot B must come back "collapsed", not "ran"."""
    fake_status, sleep_calls = isolated_env
    since = _iso_now()
    sched._write_marker(sched.DeferralMarker(deferring_since=since, pid=os.getpid(), host="h", attempts=1))
    monkeypatch.setattr(sched, "acquire", lambda trigger: (_ for _ in ()).throw(pl.PipelineLockHeld(_holder())))

    run_count = {"n": 0}
    result_b = sched.run_with_defer(lambda: run_count.__setitem__("n", run_count["n"] + 1))

    assert result_b == "collapsed"
    assert run_count["n"] == 0


# ---------------------------------------------------------------------------
# A deferral owner that crashes mid-wait is recovered honestly, not silently
# dropped, and (when its window hasn't run out) resumes its ORIGINAL clock.
# ---------------------------------------------------------------------------


def test_orphaned_marker_with_elapsed_window_is_recovered_and_logged(monkeypatch, isolated_env):
    fake_status, sleep_calls = isolated_env
    long_ago = (datetime.now(timezone.utc) - timedelta(minutes=sched.DEFER_WINDOW_MIN + 5)).isoformat()
    sched._write_marker(sched.DeferralMarker(deferring_since=long_ago, pid=999_999_999, host="dead", attempts=4))

    attempts = {"n": 0}

    def fake_acquire(trigger):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise pl.PipelineLockHeld(_holder())
        return _FakeLock()

    monkeypatch.setattr(sched, "acquire", fake_acquire)
    ran = []
    result = sched.run_with_defer(lambda: ran.append(1))

    assert result == "ran"
    assert ran == [1]
    recovered = [c for c in fake_status.calls_named("deferral_abandoned") if c[2].get("recovered")]
    assert len(recovered) == 1, "the orphaned slot's overdue abandonment must be surfaced, never silently dropped"


def test_orphaned_marker_within_window_resumes_original_clock(monkeypatch, isolated_env):
    original_since = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    sched._write_marker(sched.DeferralMarker(deferring_since=original_since, pid=999_999_999, host="dead", attempts=2))

    fake_status, sleep_calls = isolated_env
    attempts = {"n": 0}

    def fake_acquire(trigger):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise pl.PipelineLockHeld(_holder())
        return _FakeLock()

    monkeypatch.setattr(sched, "acquire", fake_acquire)
    ran = []
    result = sched.run_with_defer(lambda: ran.append(1))

    assert result == "ran"
    # No "recovered" abandonment — that slot's window hadn't run out.
    assert not [c for c in fake_status.calls_named("deferral_abandoned") if c[2].get("recovered")]
    # The heartbeat published while retrying must carry the ORIGINAL clock,
    # not a freshly-reset one — this is what caps total wait at 2h even
    # across a crash of the deferring coordinator itself.
    deferring_calls = fake_status.calls_named("deferring")
    assert any(c[1][2] == original_since for c in deferring_calls), [c[1] for c in deferring_calls]
