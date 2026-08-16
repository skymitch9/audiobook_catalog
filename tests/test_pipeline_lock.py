"""Tests for app/core/pipeline_lock.py — the single-flight lock (docs/info/
ROLES.md §1c/§1d, "two pipeline runs must never overlap").

Every test redirects pipeline_lock.LOCK_PATH into a pytest tmp_path so
nothing here ever touches the real output_files/pipeline.lock (which a real
scheduled run on this machine might genuinely be holding).

The last two tests exercise the lock for REAL across two OS processes (see
tests/_pipeline_lock_holder.py) rather than mocking pid-liveness — that is
the part of this module (Windows OpenProcess/GetExitCodeProcess) mocks
cannot honestly verify.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core import pipeline_lock as pl

HOLDER_SCRIPT = Path(__file__).resolve().parent / "_pipeline_lock_holder.py"


@pytest.fixture(autouse=True)
def isolated_lock_path(tmp_path, monkeypatch):
    """Every test gets its own lock file, never the real one."""
    monkeypatch.setattr(pl, "LOCK_PATH", tmp_path / "pipeline.lock")
    yield


# ---------------------------------------------------------------------------
# Basic acquire / release
# ---------------------------------------------------------------------------


def test_acquire_creates_lock_file_with_holder_info():
    lock = pl.acquire("manual")
    try:
        assert pl.LOCK_PATH.exists()
        raw = json.loads(pl.LOCK_PATH.read_text(encoding="utf-8"))
        assert raw["pid"] == os.getpid()
        assert raw["trigger"] == "manual"
        assert "started_at" in raw
    finally:
        lock.release()
    assert not pl.LOCK_PATH.exists()


def test_release_is_idempotent():
    lock = pl.acquire("manual")
    lock.release()
    lock.release()  # must not raise
    assert not pl.LOCK_PATH.exists()


def test_context_manager_releases_on_exception():
    with pytest.raises(ValueError):
        with pl.acquire("manual"):
            raise ValueError("boom")
    assert not pl.LOCK_PATH.exists()


# ---------------------------------------------------------------------------
# A held (non-stale) lock refuses a second acquire — in-process
# ---------------------------------------------------------------------------


def test_second_acquire_refused_while_first_holds():
    first = pl.acquire("scheduled")
    try:
        with pytest.raises(pl.PipelineLockHeld) as exc:
            pl.acquire("manual")
        holder = exc.value.holder
        assert holder.pid == os.getpid()  # this process is genuinely still alive
        assert holder.trigger == "scheduled"
    finally:
        first.release()

    # Now that it's released, a fresh acquire must succeed.
    second = pl.acquire("manual")
    second.release()


def test_holder_describe_names_pid_host_trigger_and_age():
    lock = pl.acquire("manual-rebuild")
    try:
        holder = pl.current_holder()
        desc = holder.describe()
        assert f"pid {os.getpid()}" in desc
        assert "trigger=manual-rebuild" in desc
        assert "started" in desc
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Stale-lock recovery — dead pid (in-process, fabricated lock file)
# ---------------------------------------------------------------------------


def test_dead_pid_reclaimed_regardless_of_age():
    """A pid that plainly doesn't exist is reclaimed instantly, even though
    the recorded started_at is recent — the pid-alive signal alone is
    enough; it does not need to wait for the age ceiling too."""
    _write_raw_lock(pid=999_999_999, started_at=_iso_now())
    lock = pl.acquire("manual")  # must not raise
    lock.release()


def test_stale_lock_older_than_ceiling_is_reclaimed_even_if_pid_is_alive():
    """Isolates the SECOND signal (age ceiling) from the first (pid-alive):
    use this test process's own (genuinely alive) pid, but back-date
    started_at past STALE_LOCK_HOURS."""
    old = datetime.now(timezone.utc) - timedelta(hours=pl.STALE_LOCK_HOURS + 0.5)
    _write_raw_lock(pid=os.getpid(), started_at=old.isoformat())
    lock = pl.acquire("manual")  # must not raise, must reclaim on age alone
    lock.release()


def test_lock_within_stale_ceiling_and_alive_pid_is_not_reclaimed():
    """Sanity check for the ceiling boundary: comfortably inside
    STALE_LOCK_HOURS with a live pid must still refuse."""
    recent = datetime.now(timezone.utc) - timedelta(hours=pl.STALE_LOCK_HOURS - 1)
    _write_raw_lock(pid=os.getpid(), started_at=recent.isoformat())
    with pytest.raises(pl.PipelineLockHeld):
        pl.acquire("manual")


def test_corrupt_lock_file_falls_back_to_mtime_age():
    """An unparseable lock file (e.g. a crash mid-write) must not deadlock
    the pipeline forever — staleness falls back to the file's own mtime."""
    pl.LOCK_PATH.write_text("{not valid json", encoding="utf-8")
    # Freshly written -> not yet stale by mtime -> refused.
    with pytest.raises(pl.PipelineLockHeld):
        pl.acquire("manual")
    # Back-date the file's mtime past the ceiling -> reclaimed.
    old_ts = time.time() - (pl.STALE_LOCK_HOURS + 1) * 3600
    os.utime(pl.LOCK_PATH, (old_ts, old_ts))
    lock = pl.acquire("manual")
    lock.release()


def _write_raw_lock(pid: int, started_at: str, trigger: str = "manual", host: str = "ghost") -> None:
    pl.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    pl.LOCK_PATH.write_text(
        json.dumps({"pid": pid, "host": host, "trigger": trigger, "started_at": started_at}),
        encoding="utf-8",
    )


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# REAL cross-process exercises (no mocks) — see tests/_pipeline_lock_holder.py
# ---------------------------------------------------------------------------


def test_real_second_process_refused_while_real_holder_is_running():
    """What it says: spawn a genuinely separate OS process that holds the
    lock for a few seconds, then confirm a real (not mocked) acquire() call
    in THIS process is refused and correctly reports the holder's real pid,
    then succeeds once the holder process actually exits."""
    proc = subprocess.Popen(
        [sys.executable, str(HOLDER_SCRIPT), str(pl.LOCK_PATH), "hold", "5"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        held_pid = _wait_for_line(proc, prefix="HELD ", timeout=15)
        assert held_pid != os.getpid()
        assert pl.pid_alive(held_pid)

        with pytest.raises(pl.PipelineLockHeld) as exc:
            pl.acquire("manual")
        assert exc.value.holder.pid == held_pid

        rc = proc.wait(timeout=15)
        assert rc == 0
        assert not pl.LOCK_PATH.exists()  # the holder released cleanly

        lock = pl.acquire("manual")  # now free — must succeed
        lock.release()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_real_crashed_holder_pid_is_reclaimed_instantly():
    """Spawn a process that acquires the lock and then hard-exits via
    os._exit() WITHOUT releasing (simulating a crash). Once it has actually
    exited, a real ctypes pid-alive check against that (real, now-dead) pid
    must report it dead, and acquire() must reclaim the lock immediately —
    no waiting for the age ceiling."""
    proc = subprocess.Popen(
        [sys.executable, str(HOLDER_SCRIPT), str(pl.LOCK_PATH), "crash"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    crashed_pid = _wait_for_line(proc, prefix="HELD ", timeout=15)
    rc = proc.wait(timeout=15)  # ensure it has genuinely exited before checking
    assert rc == 0

    assert pl.LOCK_PATH.exists()  # left behind, exactly like a real crash
    assert not pl.pid_alive(crashed_pid), (
        "a truly-exited process must read as dead, not just eventually — "
        "this is the OpenProcess+GetExitCodeProcess check, not a mock"
    )

    lock = pl.acquire("manual")  # must reclaim instantly, no 4h wait
    holder_pid_now = json.loads(pl.LOCK_PATH.read_text())["pid"]
    assert holder_pid_now == os.getpid()
    lock.release()


def _wait_for_line(proc: subprocess.Popen, prefix: str, timeout: float) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if line.startswith(prefix):
            return int(line[len(prefix) :].strip())
        if proc.poll() is not None:
            break
    raise AssertionError(f"holder process never printed a line starting with {prefix!r}")
