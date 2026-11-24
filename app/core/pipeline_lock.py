"""Single-flight lock for the audiobook pipeline.

⚠️ Owner order 2026-08-16 (docs/info/ROLES.md §1c/§1d): two pipeline runs must
NEVER overlap. They fight over Drive uploads and the git commit, and an
overlap is exactly how a corrupted catalog commit happens. Nothing enforced
this before this module existed — the 8h Task Scheduler job
(scripts/sync_pipeline_8h.bat), a manual `python scripts/sync_to_drive.py`,
the remote "run now" trigger (app/tools/pipeline_watcher.py, which runs this
same script as a subprocess), and `--rebuild-only` could all start on top of
each other.

Design: a lockfile at output_files/pipeline.lock holding JSON
``{pid, host, trigger, started_at}``. Acquired with an atomic
``O_CREAT | O_EXCL`` open, so two processes racing to create it can never
both "win" — the loser always sees FileExistsError and falls into the
held/stale decision below. Released by deleting the file, always via
``PipelineLock.release()`` called from a ``try/finally`` (or the context-
manager protocol), so both a normal return and an exception release it.

Stale-lock recovery (a lock must never deadlock the pipeline after a crash).
⚠️ LIVENESS, NOT AGE, decides (review fix 2026-11-24). The old rule also
reclaimed any lock older than 4 h even when its pid was alive — but a run on
2026-11-23 spent ~1h45 in ONE step, observers (purchase_audit, fs_watcher) act
on LEFTOVER, and fs_watcher's fired run would then reclaim the lock from the
LIVE process: two sync_to_drive.py runs at once, the one thing this module
exists to prevent. And a dead holder whose pid was reused read HELD for 4 h.

A readable lock is HELD exactly when ALL of these hold (``_classify``, the ONE
implementation acquire() and lock_status() both use):

  1. **PID-alive check.** Windows-safe: ``OpenProcess`` + ``GetExitCodeProcess``
     (compared against STILL_ACTIVE) + ``CloseHandle`` via ctypes — never
     ``os.kill``, because on Windows ``os.kill(pid, 0)`` is NOT a harmless
     existence probe like it is on POSIX; CPython's Windows implementation
     calls ``TerminateProcess`` for any signal value other than the two CTRL
     events, so a "just checking" call could kill an unrelated process that
     happens to reuse the pid. ``OpenProcess`` succeeding is also not
     sufficient by itself — it can still succeed for a pid that has already
     exited, as long as something else (another handle) keeps the kernel
     process object alive a little longer — so ``GetExitCodeProcess`` is
     the actual liveness signal. A dead pid is reclaimed immediately,
     whatever the age (crash, Task Manager kill, reboot).
  2. **It is the SAME process.** The pid's process creation time (Windows
     ``GetProcessTimes``; Linux ``/proc/<pid>/stat``) must be at or before
     the lock's ``started_at`` (+ PID_REUSE_SLACK_SECONDS for clock steps):
     the holder writes started_at after it starts, so a process created
     later is a different program that inherited the pid — LEFTOVER. If the
     creation time cannot be read, this check is skipped (fail toward HELD:
     a wrong HELD defers a run; a wrong LEFTOVER doubles one).
  3. **It is younger than WEDGED_LOCK_HOURS (24).** The safety net, not the
     rule: a verified-live holder past it is reclaimed, LOUDLY (stderr from
     acquire(), and describe_leftover() says a still-running process is
     being reclaimed). Why 24: >10x the longest single step observed
     (~1h45, 2026-11-23) and three full 8 h schedule slots — a run that has
     not finished in a day is wedged, and under the never-kill order the
     only remedy left to a human would be deleting the lock, i.e. this same
     reclaim. Meanwhile each scheduled slot's own defer loop
     (app/core/pipeline_schedule.py) gives up after its 2 h window, so a
     wedged holder costs slots without drifting the 8 h grid.

A lock file that exists but can't be parsed (e.g. a crash mid-write left a
truncated file) has no pid to check: it falls back to the file's own mtime
age against UNREADABLE_LOCK_HOURS (4, the old ceiling, unchanged).
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.config import OUTPUT_DIR

LOCK_PATH: Path = OUTPUT_DIR / "pipeline.lock"
# A verified-live holder older than this is reclaimed, loudly (docstring, 3.).
WEDGED_LOCK_HOURS: float = 24.0
# An UNPARSEABLE lock file (no pid to check) is HELD until its mtime is this old.
UNREADABLE_LOCK_HOURS: float = 4.0
# A process created more than this many seconds AFTER the lock's started_at is
# not the holder (pid reuse). The real holder is created BEFORE it writes
# started_at, so the slack only absorbs wall-clock steps (NTP) between the two.
PID_REUSE_SLACK_SECONDS: float = 60.0

# Defensive cap on the reclaim loop below — in practice this never runs more
# than twice (one stale lock found, one successful create). Anything beyond
# this many spins means something is very wrong (e.g. two processes stuck
# fighting over a lock whose staleness check keeps flip-flopping), so give up
# loudly with a RuntimeError rather than spinning forever.
_MAX_ACQUIRE_SPINS = 20


def _hostname() -> str:
    return os.getenv("COMPUTERNAME") or os.getenv("HOSTNAME") or "unknown"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class LockHolder:
    """Who currently holds (or last held) the lock."""

    pid: int
    host: str
    trigger: str
    started_at: str  # ISO 8601, UTC

    def started_epoch(self) -> float | None:
        try:
            started = datetime.fromisoformat(self.started_at)
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            return started.timestamp()
        except Exception:
            return None

    def age_hours(self) -> float:
        started = self.started_epoch()
        if started is None:
            return float("inf")  # unparseable timestamp -> treat as ancient
        return (time.time() - started) / 3600.0

    def describe(self) -> str:
        """Human-readable line for console output and pipeline_status —
        'print who holds the lock and since when' per the owner's spec."""
        return (
            f"pid {self.pid} on {self.host} (trigger={self.trigger}, "
            f"started {self.started_at}, {self.age_hours():.2f}h ago)"
        )


class PipelineLockHeld(Exception):
    """Raised when another process genuinely (non-stale) holds the lock."""

    def __init__(self, holder: LockHolder):
        self.holder = holder
        super().__init__(f"pipeline lock held by {holder.describe()}")


def _win_pid_alive(pid: int) -> bool:
    """Windows pid-liveness check, done right.

    ``OpenProcess`` alone is NOT sufficient: it can still succeed for a pid
    whose process has already called ExitProcess, as long as the kernel
    process object hasn't been fully destroyed yet (e.g. because something
    else — like our own ``subprocess.Popen`` handle in a test — is still
    holding a handle to it). Checking ``GetExitCodeProcess`` afterwards and
    comparing against STILL_ACTIVE is what actually distinguishes "running"
    from "exited but not yet reaped" — the difference matters here because
    the crash-recovery path depends on this being right the instant a
    holder dies, not eventually.
    """
    import ctypes.wintypes as wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False  # couldn't query — do not assume alive
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    """True if a process with this pid currently exists. Never signals or
    kills it — see the module docstring for why ``os.kill`` is unsafe here
    on Windows."""
    if pid is None or pid <= 0:
        return False
    if os.name == "nt":
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    except OSError:
        return False


def _win_process_created(pid: int) -> float | None:
    """Creation time (epoch seconds) of the process with this pid, via
    OpenProcess + GetProcessTimes. None when it cannot be read."""
    import ctypes.wintypes as wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]  # creation, exit, kernel, user
        if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        created = times[0]
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime  # 100 ns since 1601
        return ticks / 1e7 - 11644473600.0
    finally:
        kernel32.CloseHandle(handle)


def _parse_proc_start(pid_stat: str, proc_stat: str, clk_tck: int) -> float:
    """Epoch start time from /proc/<pid>/stat + /proc/stat text. comm (field
    2) may hold spaces and parens, so split after the LAST ')'; starttime is
    field 22 overall = index 19 of what follows."""
    start_ticks = int(pid_stat.rsplit(")", 1)[1].split()[19])
    btime = next(int(line.split()[1]) for line in proc_stat.splitlines() if line.startswith("btime "))
    return btime + start_ticks / clk_tck


def _linux_process_created(pid: int) -> float | None:
    try:
        return _parse_proc_start(Path(f"/proc/{pid}/stat").read_text(),
                                 Path("/proc/stat").read_text(), os.sysconf("SC_CLK_TCK"))
    except Exception:
        return None


def process_start_time(pid: int) -> float | None:
    """When the process now holding this pid was created (epoch seconds), or
    None if that cannot be determined here. psutil is installed in neither
    py -3.12 nor .venv (checked 2026-11-24), hence ctypes / procfs."""
    if pid is None or pid <= 0:
        return None
    if os.name == "nt":
        try:
            return _win_process_created(pid)
        except Exception:
            return None
    return _linux_process_created(pid)


def _read_holder() -> LockHolder | None:
    """Best-effort read of the lock file. None if missing or unparseable."""
    try:
        raw = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        return LockHolder(
            pid=int(raw["pid"]),
            host=str(raw.get("host", "?")),
            trigger=str(raw.get("trigger", "?")),
            started_at=str(raw["started_at"]),
        )
    except FileNotFoundError:
        return None
    except Exception:
        return None  # corrupt/partial write; caller falls back to file mtime


def _file_age_hours() -> float:
    try:
        return (time.time() - LOCK_PATH.stat().st_mtime) / 3600.0
    except OSError:
        return float("inf")


# _classify() reasons
_HELD_LIVE = "live"
_HELD_UNREADABLE = "unreadable-fresh"
_LEFT_DEAD = "dead"
_LEFT_REUSED = "reused"
_LEFT_WEDGED = "wedged"
_LEFT_UNREADABLE = "unreadable"


def _classify(holder: LockHolder | None) -> tuple[bool, str]:
    """(reclaimable, reason) — THE rule, shared by acquire() and lock_status().
    See the module docstring for each signal and its numbers."""
    if holder is None:
        if _file_age_hours() >= UNREADABLE_LOCK_HOURS:
            return True, _LEFT_UNREADABLE
        return False, _HELD_UNREADABLE
    if not pid_alive(holder.pid):
        return True, _LEFT_DEAD
    created = process_start_time(holder.pid)
    started = holder.started_epoch()
    if created is not None and started is not None and created > started + PID_REUSE_SLACK_SECONDS:
        return True, _LEFT_REUSED
    if holder.age_hours() >= WEDGED_LOCK_HOURS:
        return True, _LEFT_WEDGED
    return False, _HELD_LIVE


def _is_stale(holder: LockHolder | None) -> bool:
    """True when the lock may be safely reclaimed (see _classify)."""
    return _classify(holder)[0]


def current_holder() -> LockHolder | None:
    """Best-effort read of who holds the lock right now (for status/UI
    reporting). Returns None if the lock is free or unreadable."""
    if not LOCK_PATH.exists():
        return None
    return _read_holder()


# ⚠️ "The lock file exists" is NOT "a run is in flight" (incidents 2026-11-21
# and 2026-11-23). A run that is killed — Task Manager, a reboot, a closed
# console — never reaches its `finally`, so its lock stays on disk with a dead
# pid in it. acquire() has always known that (it reclaims via _is_stale()),
# but the OBSERVERS tested LOCK_PATH.exists(): on 2026-11-21 a reboot's
# leftover made every 15-minute purchase-audit tick skip for 7.5 hours, and on
# 2026-11-23 fs_watcher sat "observing" dead pid 65744 and then dropped 19
# audiobooks + 22 ebooks of pending changes as "already ingested". This is the
# ONE answer to "is a run genuinely in flight?" — it applies acquire()'s own
# reclaim rule, so an observer and the next run can never disagree about it.
LOCK_FREE = "free"
LOCK_HELD = "held"          # a run is genuinely in flight — acquire() would refuse
LOCK_LEFTOVER = "leftover"  # file present, but acquire() would reclaim it


def lock_status() -> tuple[str, LockHolder | None]:
    """(state, holder) for the lock right now. state is LOCK_FREE, LOCK_HELD
    or LOCK_LEFTOVER; holder is None when free or when the file is unreadable.

    A live holder is HELD however long it runs (up to the WEDGED_LOCK_HOURS
    safety net); a dead or reused pid is LEFTOVER at once. An unreadable lock
    younger than UNREADABLE_LOCK_HOURS is HELD (a run may be mid-write of its
    own lock file). Same rule as acquire() — both go through _classify().
    Never deletes anything: reclaiming stays acquire()'s job."""
    if not LOCK_PATH.exists():
        return LOCK_FREE, None
    holder = _read_holder()
    if holder is None and not LOCK_PATH.exists():
        return LOCK_FREE, None  # released between the two reads
    if _is_stale(holder):
        return LOCK_LEFTOVER, holder
    return LOCK_HELD, holder


def describe_leftover(holder: LockHolder | None) -> str:
    """The one log line an observer prints for a LOCK_LEFTOVER lock, naming
    which signal made it one — "not running" would be false for a reused pid
    or a wedged-but-live holder."""
    _stale, reason = _classify(holder)
    if holder is None:
        why = f"unreadable and older than the {UNREADABLE_LOCK_HOURS:g}h ceiling"
        who = "unreadable lock file"
    else:
        who = holder.describe()
        if reason == _LEFT_REUSED:
            why = (f"pid {holder.pid} now belongs to a process that started after the lock "
                   "was taken (pid reuse) — the holder is gone")
        elif reason == _LEFT_WEDGED:
            why = (f"⚠️ pid {holder.pid} IS STILL RUNNING but has held the lock past the "
                   f"{WEDGED_LOCK_HOURS:g}h safety net — treated as wedged; investigate it")
        else:
            why = f"pid {holder.pid} is not running"
    return (
        f"pipeline lock is a leftover, not a run in flight ({who}): {why} — "
        "proceeding as if no run is in flight. Leaving the file alone: the next "
        "run's acquire() reclaims it."
    )


class PipelineLock:
    """Context manager for the single-flight lock.

    Usage::

        try:
            lock = acquire(trigger)
        except PipelineLockHeld as held:
            ...  # held.holder describes who has it
        else:
            try:
                do_work()
            finally:
                lock.release()

    or equivalently ``with acquire(trigger): do_work()``.
    """

    def __init__(self, trigger: str):
        self.trigger = trigger
        self._acquired = False

    def __enter__(self) -> "PipelineLock":
        if not self._acquired:
            _do_acquire(self)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            LOCK_PATH.unlink()
        except FileNotFoundError:
            pass
        self._acquired = False


def _do_acquire(lock: PipelineLock) -> None:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "host": _hostname(),
        "trigger": lock.trigger,
        "started_at": _now_iso(),
    }
    data = json.dumps(payload).encode("utf-8")

    for _ in range(_MAX_ACQUIRE_SPINS):
        try:
            fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = _read_holder()
            stale, reason = _classify(holder)
            if not stale:
                raise PipelineLockHeld(
                    holder
                    or LockHolder(
                        pid=-1,
                        host="?",
                        trigger="?",
                        started_at=datetime.fromtimestamp(LOCK_PATH.stat().st_mtime, tz=timezone.utc).isoformat(),
                    )
                )
            if reason == _LEFT_WEDGED:
                print(f"[pipeline_lock] ⚠️ RECLAIMING the lock from a process that is STILL "
                      f"RUNNING: {holder.describe()} — past the {WEDGED_LOCK_HOURS:g}h safety "
                      "net. Investigate that process.", file=sys.stderr, flush=True)
            # Stale: reclaim and retry the exclusive create. If a third
            # process wins the race in between, its fresh (non-stale) lock
            # correctly blocks us on the next loop iteration.
            try:
                LOCK_PATH.unlink()
            except FileNotFoundError:
                pass
            continue
        else:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            lock._acquired = True
            return

    raise RuntimeError(
        f"pipeline_lock: gave up after {_MAX_ACQUIRE_SPINS} attempts to "
        "acquire or reclaim the lock — investigate output_files/pipeline.lock"
    )


def acquire(trigger: str) -> PipelineLock:
    """Acquire the lock or raise PipelineLockHeld. Caller must release()."""
    lock = PipelineLock(trigger)
    _do_acquire(lock)
    return lock
