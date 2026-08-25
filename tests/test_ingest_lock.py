"""Tests for the ingest single-flight lock in app/tools/ingest_books.py (_Lock).

The lock guards the 00:00-08:00 ingestion window so the 30-minute scheduled
task never starts a second Whisper run on top of a live one. It used to be
time-only (mtime vs LOCK_STALE_HOURS = 12 h): a hard crash / kill / power loss
mid-window stranded the whole window for up to 12 h. This suite pins the fix
(docs/info/pipeline-sanctity-2026-08-24.md, finding #1): a provably-dead holder
pid is reclaimed IMMEDIATELY, a live pid is never stolen, a recycled/foreign pid
is not trusted for liveness, and the 12 h age ceiling still backstops a
wedged-but-alive holder or an unreadable lock file.

The liveness primitive itself (pid_alive: Windows OpenProcess +
GetExitCodeProcess) is the canonical one from app/core/pipeline_lock.py, which
tests/test_pipeline_lock.py already exercises across REAL OS processes; this
file reuses it and adds one real cross-process crash test to prove the
integration end to end rather than only through fabricated pids.

Every test redirects _Lock's path into a pytest tmp_path so nothing here ever
touches the real output_files/ingest_books.lock (which a real scheduled run on
this machine might genuinely be holding).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.tools import ingest_books as ib


@pytest.fixture
def lock_path(tmp_path):
    return tmp_path / "ingest_books.lock"


def _write_lock(path: Path, *, pid: int, host: str | None = None,
                at: str = "2026-08-24T00:00:00-07:00") -> None:
    if host is None:
        host = ib._this_host()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "host": host, "at": at}), encoding="utf-8")


def _backdate(path: Path, hours: float) -> None:
    ts = time.time() - hours * 3600
    os.utime(path, (ts, ts))


DEAD_PID = 999_999_999  # nothing this large is a live pid on this machine


# ---------------------------------------------------------------------------
# Basic acquire / release
# ---------------------------------------------------------------------------


def test_fresh_acquire_writes_pid_host_and_releases(lock_path):
    with ib._Lock(lock_path) as lk:
        assert lk.acquired
        assert lock_path.exists()
        raw = json.loads(lock_path.read_text(encoding="utf-8"))
        assert raw["pid"] == os.getpid()
        assert raw["host"] == ib._this_host()
        assert "at" in raw
    # released on exit
    assert not lock_path.exists()


def test_live_holder_is_not_stolen(lock_path):
    """A recent lock held by a genuinely-alive pid (this process) must refuse a
    second acquire — the whole reason the lock exists."""
    _write_lock(lock_path, pid=os.getpid())  # this pid is alive, mtime = now
    with ib._Lock(lock_path) as lk:
        assert not lk.acquired
    # The live holder's lock file must be left completely intact.
    assert lock_path.exists()
    assert json.loads(lock_path.read_text())["pid"] == os.getpid()


# ---------------------------------------------------------------------------
# Signal 1 — PID liveness (the fix)
# ---------------------------------------------------------------------------


def test_dead_pid_reclaimed_immediately_even_when_lock_is_fresh(lock_path):
    """The core fix: a fabricated lock naming a pid that plainly does not exist
    is reclaimed at once, even though its mtime is only seconds old and nowhere
    near the 12 h ceiling. Under the old time-only lock this would have blocked
    for up to 12 h."""
    _write_lock(lock_path, pid=DEAD_PID)          # dead pid, this host, mtime = now
    assert ib._lock_age_hours(lock_path) < 1      # fresh: age alone would NOT clear it
    with ib._Lock(lock_path) as lk:
        assert lk.acquired                        # reclaimed on the dead-pid signal
        assert json.loads(lock_path.read_text())["pid"] == os.getpid()


# ---------------------------------------------------------------------------
# Signal 2 — age ceiling backstop
# ---------------------------------------------------------------------------


def test_ttl_backstop_reclaims_a_wedged_but_alive_holder(lock_path):
    """Isolates the age ceiling from the pid signal: use this process's own
    (genuinely alive) pid so liveness says 'held', then back-date the file past
    LOCK_STALE_HOURS. The wedged-but-alive holder must still be reclaimed on age
    alone."""
    _write_lock(lock_path, pid=os.getpid())
    _backdate(lock_path, ib.LOCK_STALE_HOURS + 0.5)
    with ib._Lock(lock_path) as lk:
        assert lk.acquired


def test_within_ceiling_and_alive_pid_is_refused(lock_path):
    """Boundary sanity: comfortably inside LOCK_STALE_HOURS with a live pid must
    still refuse."""
    _write_lock(lock_path, pid=os.getpid())
    _backdate(lock_path, ib.LOCK_STALE_HOURS - 1)
    with ib._Lock(lock_path) as lk:
        assert not lk.acquired


# ---------------------------------------------------------------------------
# Recycled / foreign pid guard — never steal a pid we cannot trust
# ---------------------------------------------------------------------------


def test_foreign_host_dead_pid_is_not_trusted_for_liveness(lock_path):
    """A lock recorded on a DIFFERENT host names a pid that is meaningless here:
    even though that pid number is dead on THIS machine, it must NOT be reclaimed
    on the liveness signal (it could be a live run on the other host, or a
    recycled pid). It falls through to the age ceiling only."""
    _write_lock(lock_path, pid=DEAD_PID, host="some-other-machine")
    # Fresh -> the ceiling has not elapsed -> must be refused, not stolen.
    with ib._Lock(lock_path) as lk:
        assert not lk.acquired
    # Once the age ceiling elapses, the backstop still frees it.
    _backdate(lock_path, ib.LOCK_STALE_HOURS + 1)
    with ib._Lock(lock_path) as lk:
        assert lk.acquired


def test_recycled_live_pid_within_ttl_is_left_alone(lock_path):
    """Fail-safe direction: a pid that reads ALIVE on this host (here, our own
    pid standing in for a recycled pid now owned by an unrelated process) is
    never stolen while inside the ceiling — uncertainty must not steal a
    possibly-live lock."""
    _write_lock(lock_path, pid=os.getpid())
    _backdate(lock_path, 1.0)  # recent, well inside the ceiling
    with ib._Lock(lock_path) as lk:
        assert not lk.acquired


# ---------------------------------------------------------------------------
# Corrupt lock file — mtime fallback
# ---------------------------------------------------------------------------


def test_corrupt_lock_falls_back_to_mtime(lock_path):
    """A truncated/unparseable lock (crash mid-write) must not deadlock the
    pipeline: with no readable pid, staleness falls back to the mtime ceiling."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("{not valid json", encoding="utf-8")
    with ib._Lock(lock_path) as lk:   # fresh corrupt file -> refused
        assert not lk.acquired
    _backdate(lock_path, ib.LOCK_STALE_HOURS + 1)
    with ib._Lock(lock_path) as lk:   # past the ceiling -> reclaimed
        assert lk.acquired


# ---------------------------------------------------------------------------
# REAL cross-process crash — a genuinely dead pid, no mocks
# ---------------------------------------------------------------------------


_HOLDER_SRC = """
import json, os, sys
p = sys.argv[1]
host = os.getenv("COMPUTERNAME") or os.getenv("HOSTNAME") or "unknown"
with open(p, "w", encoding="utf-8") as f:
    f.write(json.dumps({"pid": os.getpid(), "host": host, "at": "2026-08-24T00:00:00-07:00"}))
print("HELD", os.getpid(), flush=True)
os._exit(0)  # hard exit, no lock cleanup — simulates a crash
"""


def test_real_crashed_holder_pid_is_reclaimed_instantly(lock_path):
    """Spawn a real, separate OS process that writes the ingest lock with its
    own pid+host and then hard-exits WITHOUT releasing. After it has genuinely
    exited, the lock file is fresh (mtime = now) so only the pid-liveness signal
    can clear it — proving the fix end to end against a real dead pid, not a
    fabricated one."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SRC, str(lock_path)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    out = proc.stdout.readline()
    held_pid = int(out.split()[1])
    assert proc.wait(timeout=15) == 0        # genuinely exited
    assert held_pid != os.getpid()
    assert lock_path.exists()                # left behind, exactly like a crash
    assert ib._lock_age_hours(lock_path) < 1  # fresh: age alone would not clear it
    assert not ib.pid_alive(held_pid)        # real ctypes check, real dead pid

    with ib._Lock(lock_path) as lk:
        assert lk.acquired                   # reclaimed instantly on the dead pid
        assert json.loads(lock_path.read_text())["pid"] == os.getpid()
