"""DEFER, DON'T SKIP — the 8-hourly scheduled trigger's retry/defer state
machine.

Owner requirement, docs/info/ROLES.md §1d (exact words): *"make sure if an 8
hour run is stopped while a pipeline is running that 8 hour run can be
delayed up to 2 hours without breaking the next auto run. that way we don't
skip the auto runs."*

Only the TRUE 8-hour Task Scheduler trigger uses this. scripts/
sync_pipeline_8h.bat sets ``PIPELINE_TRIGGER=scheduled`` explicitly, so this
path is unambiguous — it is never reached by a human running the script by
hand, ``--rebuild-only``, or the remote "run now" watcher trigger, all of
which default to trigger="manual"/"manual-rebuild" and fail LOUDLY the
instant the lock is held (see app.core.pipeline_lock + scripts/
sync_to_drive.py's ``_execute_locked``) rather than waiting: a human or an
event-driven request wants an answer now, not a silent up-to-2-hour wait.

The schedule itself lives in Windows Task Scheduler, outside this repo, and
is NOT touched here — the 8h grid keeps firing on its own independent
cadence no matter what this module does. That is *why* the deferral must be
entirely internal to one process's lifetime: as long as this module's own
wait never exceeds the 2h the owner specified, the next 8h trigger is
guaranteed to fire after this one has already resolved (run or abandoned),
so the cadence can never drift.

State machine, driven entirely from inside the ONE process invoked by that
8h trigger:

  1. Try to acquire the pipeline lock (app.core.pipeline_lock.acquire).
       - Free -> clear any leftover deferral marker, run immediately, done.
       - Held -> go to 2.
  2. Look at output_files/pipeline_defer.json (the deferral marker — tracks
     who is currently "spending" this slot's 2-hour grace period).
       - A LIVE marker (its owner pid is still running AND its own window
         hasn't elapsed) belonging to a DIFFERENT process -> another
         invocation is already deferring this slot. Do **not** start a
         second countdown — that is the "deferrals must not stack" rule.
         Log it, report it, return without running or sleeping.
       - No marker, or a STALE one (owner pid dead, or its window already
         elapsed) -> this process becomes the deferral's owner for a fresh
         slot. If the stale marker's window had elapsed without anyone
         logging the abandonment (its owner crashed mid-wait), surface that
         abandonment now — LOUD, never silently dropped — then start this
         process's own fresh window and go to 3.
  3. Retry loop (marker owner only): sleep RETRY_INTERVAL_MIN, try the lock
     again. Every attempt refreshes pipeline_status so /status shows
     "deferred, retrying" with who holds the lock and since when.
       - Lock frees before DEFER_WINDOW_MIN minutes have elapsed since this
         marker's deferring_since -> acquire, clear the marker, run, done.
       - DEFER_WINDOW_MIN minutes elapse still blocked -> clear the marker,
         log the abandonment LOUDLY (console + a real pipeline_runs history
         entry, not just the live card) and return without running. The
         NEXT scheduled slot is unaffected.

DEFER_WINDOW_MIN = 120 — the owner's exact number. Do not change without a
new owner instruction.
RETRY_INTERVAL_MIN = 12 — inside the owner's suggested 10-15 minute band;
120 / 12 = 10 clean attempts across the window.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.config import OUTPUT_DIR
from app.core.pipeline_lock import LockHolder, PipelineLockHeld, acquire, pid_alive

DEFER_MARKER_PATH: Path = OUTPUT_DIR / "pipeline_defer.json"
DEFER_WINDOW_MIN: float = 120.0
RETRY_INTERVAL_MIN: float = 12.0

# Live status for the admin panel — same defensive-import pattern as
# scripts/sync_to_drive.py: a no-op shim when app/ credentials or
# firebase-admin are unavailable, so a status-backend outage can never cost
# a scheduled run.
try:
    from app import pipeline_status as pstatus
except Exception:  # pragma: no cover - defensive

    class _NoStatus:
        def __getattr__(self, _name):
            return lambda *a, **k: ""

    pstatus = _NoStatus()


def _hostname() -> str:
    return os.getenv("COMPUTERNAME") or os.getenv("HOSTNAME") or "unknown"


def _log(msg: str) -> None:
    print(f"[DEFER] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


@dataclass
class DeferralMarker:
    """Tracks who owns THIS slot's up-to-2h grace period, so a second
    scheduled invocation that fires while one is already deferring can
    recognize that and collapse into it instead of starting its own
    countdown (the "deferrals must not stack" rule)."""

    deferring_since: str  # ISO 8601 UTC — the clock this marker's window is measured against
    pid: int
    host: str
    attempts: int = 0

    def elapsed_min(self) -> float:
        try:
            started = datetime.fromisoformat(self.deferring_since)
        except Exception:
            return float("inf")
        return (datetime.now(timezone.utc) - started).total_seconds() / 60.0

    def window_elapsed(self) -> bool:
        return self.elapsed_min() >= DEFER_WINDOW_MIN


def _read_marker() -> DeferralMarker | None:
    try:
        raw = json.loads(DEFER_MARKER_PATH.read_text(encoding="utf-8"))
        return DeferralMarker(
            deferring_since=str(raw["deferring_since"]),
            pid=int(raw.get("pid", -1)),
            host=str(raw.get("host", "?")),
            attempts=int(raw.get("attempts", 0)),
        )
    except FileNotFoundError:
        return None
    except Exception:
        return None  # corrupt marker — treated as absent, a fresh one is written


def _write_marker(m: DeferralMarker) -> None:
    DEFER_MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFER_MARKER_PATH.write_text(
        json.dumps(
            {
                "deferring_since": m.deferring_since,
                "pid": m.pid,
                "host": m.host,
                "attempts": m.attempts,
            }
        ),
        encoding="utf-8",
    )


def _clear_marker() -> None:
    try:
        DEFER_MARKER_PATH.unlink()
    except FileNotFoundError:
        pass


def _marker_is_live(m: DeferralMarker | None) -> bool:
    """True when another process is actively (right now) owning this
    slot's deferral — i.e. this invocation must collapse into it rather
    than starting a second countdown."""
    if m is None:
        return False
    if m.window_elapsed():
        return False  # its grace period is already up; not "live" anymore
    return pid_alive(m.pid)


def _describe_marker(m: DeferralMarker) -> str:
    return f"pid {m.pid} on {m.host} (deferring since {m.deferring_since}, attempt {m.attempts})"


def run_with_defer(run_fn: Callable[[], None]) -> str:
    """Entry point for the SCHEDULED trigger only. See module docstring for
    the full state machine.

    Returns one of:
      "ran"        - the lock was acquired (immediately or after retrying)
                      and run_fn() was called and completed.
      "collapsed"  - another process is already deferring this slot; this
                      invocation did nothing, by design (no double-run).
      "abandoned"  - the 2h window elapsed still blocked; this slot was
                      skipped and logged loudly.

    Propagates whatever run_fn() itself raises (after releasing the lock).
    """
    try:
        lock = acquire("scheduled")
    except PipelineLockHeld as held:
        return _defer_and_retry(run_fn, held.holder)
    else:
        _clear_marker()  # lock was free; any leftover marker is stale junk
        try:
            run_fn()
        finally:
            lock.release()
        return "ran"


def _defer_and_retry(run_fn: Callable[[], None], first_holder: LockHolder) -> str:
    existing = _read_marker()
    if existing is not None:
        if _marker_is_live(existing):
            _log(
                f"another scheduled invocation is already deferring this slot "
                f"({_describe_marker(existing)}) — collapsing, not starting a "
                "second wait loop."
            )
            pstatus.deferring(
                "scheduled",
                first_holder.describe(),
                existing.deferring_since,
                existing.attempts,
                note="(collapsed into an existing deferral)",
            )
            return "collapsed"
        # Stale marker: its owner died, or its window is long past without
        # anyone logging the abandonment (a crash mid-defer). That earlier
        # deferral is over either way — surface an overdue abandonment
        # honestly (never silently drop it) before starting fresh.
        if existing.window_elapsed():
            _log(
                f"recovering an orphaned deferral marker whose window already "
                f"elapsed ({_describe_marker(existing)}) — its abandonment was "
                "never logged (the owner likely crashed); logging it now."
            )
            pstatus.deferral_abandoned(
                "scheduled",
                first_holder.describe(),
                existing.deferring_since,
                existing.attempts,
                recovered=True,
            )
            since = datetime.now(timezone.utc).isoformat()  # that slot is over; this is a fresh one
        else:
            # The previous owner died mid-wait but its 2h window hasn't run
            # out yet. Resume its ORIGINAL clock rather than restarting a
            # fresh 2h from now — otherwise a crash-during-defer could push
            # the total wait for one blocked slot past the owner's 2h cap.
            since = existing.deferring_since
            _log(
                f"resuming a deferral whose owner died mid-wait "
                f"({_describe_marker(existing)}) — keeping its original clock "
                f"({since}) so the total wait still caps at 2h."
            )
        _clear_marker()
    else:
        since = datetime.now(timezone.utc).isoformat()

    marker = DeferralMarker(deferring_since=since, pid=os.getpid(), host=_hostname(), attempts=0)
    _write_marker(marker)
    deadline_note = f"retrying every ~{RETRY_INTERVAL_MIN:.0f} min, giving up after 2h if still blocked."
    _log(f"lock held by {first_holder.describe()} — deferring (since {since}). {deadline_note}")
    pstatus.deferring("scheduled", first_holder.describe(), since, marker.attempts, note=deadline_note)

    while True:
        if marker.window_elapsed():
            _clear_marker()
            _log(
                f"defer window elapsed after {marker.attempts} retries — abandoning this "
                f"slot. Still blocked by {first_holder.describe()}. The next 8h slot "
                "proceeds normally."
            )
            pstatus.deferral_abandoned(
                "scheduled",
                first_holder.describe(),
                marker.deferring_since,
                marker.attempts,
            )
            return "abandoned"

        remaining_min = DEFER_WINDOW_MIN - marker.elapsed_min()
        sleep_min = min(RETRY_INTERVAL_MIN, max(0.0, remaining_min))
        time.sleep(sleep_min * 60)

        marker.attempts += 1
        _write_marker(marker)
        try:
            with acquire("scheduled") as lock:  # noqa: F841 (context manager releases for us)
                _clear_marker()
                _log(f"lock cleared after {marker.attempts} retries — running now.")
                pstatus.deferring(
                    "scheduled",
                    first_holder.describe(),
                    marker.deferring_since,
                    marker.attempts,
                    note="lock cleared — starting the run.",
                )
                run_fn()
            return "ran"
        except PipelineLockHeld as held:
            first_holder = held.holder
            _log(f"attempt {marker.attempts}: still blocked by {first_holder.describe()}.")
            pstatus.deferring(
                "scheduled",
                first_holder.describe(),
                marker.deferring_since,
                marker.attempts,
                note=deadline_note,
            )
            continue
