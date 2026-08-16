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
Two independent signals, either one clears it:

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
     the actual liveness signal. If the holder's pid is no longer running,
     the lock is reclaimed immediately regardless of its age — this covers
     the common crash case (an unhandled exception that bypassed the
     release, a kill from Task Manager, a reboot) instantly, with no
     waiting.
  2. **Age ceiling — STALE_LOCK_HOURS = 4.** Covers a holder that is still
     alive but wedged (e.g. blocked on a stdin prompt during an interactive
     run left unattended, or a network call stuck in a retry loop with no
     timeout of its own). 4 hours was chosen so a wedge self-heals well
     inside one 8-hour schedule cycle: 4h stale-reclaim + the 2h
     scheduled-defer window (see app/core/pipeline_schedule.py) = 6h,
     leaving a 2-hour margin before the *next* 8-hourly slot — one wedged
     run costs at most one abandoned/deferred slot, never two in a row. It
     is also comfortably longer than any real run observed in
     output_files/pipeline_8h.log (STEP 4 upload elapsed times are seconds
     to low minutes even for large batches; full runs finish in well under
     an hour), so a live, honestly-working run is never reclaimed out from
     under itself.

A lock file that exists but can't be parsed (e.g. a crash mid-write left a
truncated file) is treated as present-but-unreadable: staleness then falls
back to the file's own mtime age, using the same STALE_LOCK_HOURS ceiling.
"""

from __future__ import annotations

import ctypes
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.config import OUTPUT_DIR

LOCK_PATH: Path = OUTPUT_DIR / "pipeline.lock"
STALE_LOCK_HOURS: float = 4.0

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

    def age_hours(self) -> float:
        try:
            started = datetime.fromisoformat(self.started_at)
        except Exception:
            return float("inf")  # unparseable timestamp -> treat as ancient
        return (datetime.now(timezone.utc) - started).total_seconds() / 3600.0

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


def _is_stale(holder: LockHolder | None) -> bool:
    """True when the lock may be safely reclaimed. See module docstring for
    the two independent signals."""
    if holder is None:
        return _file_age_hours() >= STALE_LOCK_HOURS
    if not pid_alive(holder.pid):
        return True
    return holder.age_hours() >= STALE_LOCK_HOURS


def current_holder() -> LockHolder | None:
    """Best-effort read of who holds the lock right now (for status/UI
    reporting). Returns None if the lock is free or unreadable."""
    if not LOCK_PATH.exists():
        return None
    return _read_holder()


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
            if not _is_stale(holder):
                raise PipelineLockHeld(
                    holder
                    or LockHolder(
                        pid=-1,
                        host="?",
                        trigger="?",
                        started_at=datetime.fromtimestamp(LOCK_PATH.stat().st_mtime, tz=timezone.utc).isoformat(),
                    )
                )
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
