"""Audible purchase audit on a 15-minute cadence, with back-off.

Owner ask (2026-09-05): he bought books on 2026-09-04 and asked *"did the
pipeline run when they were detected or at the cron time"*. Measured answer,
off ``output_files/pipeline_8h.log``: a **purchase** is discovered only by the
8-hourly ``AudiobookSyncPipeline``'s acquisition stage — ``python -m
app.tools.auto_acquire``, the first of the two lines in
``scripts/sync_pipeline_8h.bat``. The two reactive watchers react to FILES:

    AudiobookFsWatcher       every 1 min    LOCAL disk arrivals (ROOT_DIR)
    AudiobookDrivePoll       every 15 min   Drive arrivals (Changes API)

and a purchase is not a file until something downloads it. So worst-case
purchase → site latency was **~8 hours**. Offered a choice, the owner picked
(a): *"15 min with back-off"*.

This module is ONE TICK of that. It is deliberately the thinnest possible
wrapper around work that already exists:

  * **It runs the 8h pipeline's own acquisition command, unchanged.** No
    extraction was needed — ``python -m app.tools.auto_acquire`` is already a
    standalone entry point, and it is invoked here exactly as
    ``sync_pipeline_8h.bat`` invokes it (``--notify --stop-after``). One
    canonical implementation of "what is missing, and download it": if that
    audit changes, both cadences change together. A subprocess rather than an
    import for the same reason STEP 8 shells out — a hard timeout is the only
    defence against an ``audible-cli`` call that decides to sit there.
  * **It does not run the pipeline itself.** When a book actually downloads it
    queues the same ``pipeline_requests`` document the /status "run now" button
    writes (``app/core/pipeline_requests.py``), which
    ``AudiobookPipelineWatcher`` consumes within 3 minutes.

⚠️ **WHY IT MUST QUEUE A RUN RATHER THAN LEAVE THE FILE TO THE WATCHERS.**
Measured 2026-09-05: ``audible_download.DEFAULT_OUT`` is
``<repo>/runtime/openaudible/books``, and ``AudiobookFsWatcher`` watches
``ROOT_DIR`` = ``C:\\Users\\nbasl\\OpenAudible\\books`` — **different
directories**. The container books dir is ingested by
``scripts/sync_to_drive.py``'s sort step (``CONTAINER_BOOKS_DIR``, lines 461
and 579) and by nothing else. So a download left to "the watchers will pick it
up" would have sat there until the next 8-hourly run — i.e. this whole feature
would have moved the 8-hour wait from *finding* the purchase to *publishing*
it, and nothing would have said so.

⚠️ **SINGLE-FLIGHT IS TWO CHECKS, NOT ONE, AND THE SECOND ONE IS THE POINT.**

  1. ``app/core/pipeline_lock.py`` — held while a pipeline RUN is in flight
     (the 8h run's sync stage, a reactive run, a hand run). Same defer rule as
     ``drive_poll``: skip the tick, change nothing.
  2. The ``AudiobookSyncPipeline`` scheduled task's own **Status** (read-only
     ``schtasks /query``). This one exists because ``auto_acquire`` **takes no
     lock at all**: in the 8h .bat it runs to completion *before*
     ``sync_to_drive.py`` acquires the pipeline lock, so during that window —
     ~20 s on a quiet run, minutes when something downloads — check (1) reports
     "free" while the very command this tick is about to run is already
     running. Two concurrent ``audible-cli`` downloads of the same ASIN into
     the same directory is the collision that check exists to prevent.

     Teaching ``auto_acquire`` to take the lock would have been the other fix
     and was deliberately NOT taken: it changes the 8h path's behaviour (an
     acquisition stage that now *refuses* under a lock it never used to see),
     and the 8-hourly slot is the self-healing pass that must not become more
     fragile in order to make a 15-minute convenience safer. The cheap,
     read-only, outside-in check is the one that leaves the 8h path exactly as
     it was.

**Back-off.** On any failing tick — an ``audible-cli`` export failure (the
shape a throttle, an expired auth or an HTTP error takes here), a failed
download, a timeout, or no purchase source at all — the interval doubles:
15 → 30 → 60, capped. A clean tick resets it to 15. The Task Scheduler entry
stays at a flat 15 minutes and the back-off is enforced *in here*, against a
state file, so the cadence can be backed off without touching Task Scheduler
(and so a hand-run tick can see and say what the current interval is).

⚠️ **UNMEASURED RISK, stated rather than hidden: nobody knows whether Audible
rate-limits or flags a client that polls 96×/day.** The 8-hourly cadence was
3×/day. The back-off is the mitigation and ``output_files/purchase_audit.log``
is the instrument — one line per tick, including the ones that do nothing. If
Audible starts refusing, the log shows the interval walking 15 → 30 → 60 and
the reason on every line. Kill switch: ``PURCHASE_AUDIT_ENABLED=0``.

Registration is the OWNER's call and is NOT done by this module — the .bat, the
.vbs and the schtasks line are in ``docs/access/PIPELINE.md``.

Usage:
    python -m app.tools.purchase_audit            # one tick, then exit
    python -m app.tools.purchase_audit --status   # config/state; audits nothing
    python -m app.tools.purchase_audit --dry-run  # audit and REPORT; download
                                                  # nothing, queue nothing, and
                                                  # do not touch the back-off
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.config import OUTPUT_DIR, PROJECT_ROOT
from app.core import pipeline_lock, pipeline_requests

# ---------------------------------------------------------------------------
# Tunables — env-overridable so an operator can retune without a code change.
# ---------------------------------------------------------------------------


def _flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


# The base cadence AND the self-throttle floor. The Task Scheduler entry owns
# the real cadence (15 min); this is what a tick measures itself against, so a
# mis-registered 1-minute task cannot hammer Audible.
BASE_MINUTES = _int_env("PURCHASE_AUDIT_MINUTES", 15)
MAX_MINUTES = _int_env("PURCHASE_AUDIT_MAX_MINUTES", 60)
# A download of a long book plus its ffmpeg remux is minutes, not seconds; the
# timeout is a wedge-breaker, not a work budget.
AUDIT_TIMEOUT_S = _int_env("PURCHASE_AUDIT_TIMEOUT_S", 1800)
# The 8h task whose acquisition stage this tick must never run beside.
SYNC_TASK_NAME = os.getenv("PURCHASE_AUDIT_SYNC_TASK", "AudiobookSyncPipeline")

STATE_PATH: Path = OUTPUT_DIR / "purchase_audit_state.json"
TICK_LOCK_PATH: Path = OUTPUT_DIR / "purchase_audit.lock"
NOTICE_PATH: Path = OUTPUT_DIR / "purchase_audit_notice.txt"
# A tick can legitimately outlive the interval (a big download). Six hours is
# past any plausible one and well inside the 8h self-heal.
STALE_TICK_LOCK_HOURS = 6
NOTICE_HOURS = 6

_EMPTY_STATE: dict = {
    "interval_minutes": BASE_MINUTES,
    "last_attempt": 0.0,          # epoch seconds of the last tick that RAN
    "consecutive_errors": 0,
    "last_result": None,          # the summary line of the last tick that ran
    "last_error": None,
    "last_download_at": None,
    "last_downloaded": 0,
    "total_downloaded": 0,
    # The `last_attempt` value whose throttled window has already been
    # announced — see _log_throttle().
    "throttle_logged_for": None,
}

# Markers in auto_acquire's own output. audit_new_purchases.audible_cli_books()
# prints these and then falls back to the container's books.json, so a tick can
# exit 0 having quietly audited a STALE list — which is exactly the condition
# back-off exists for.
_CLI_FAILURE_MARKERS = ("[audible-cli] export failed", "[audible-cli] export error")


def _now() -> float:
    return time.time()


def _log(msg: str) -> None:
    print(f"[purchase-audit] {datetime.now().strftime('%Y-%m-%d %H:%M')} {msg}", flush=True)


def _notice(msg: str) -> None:
    """Log a recurring condition at most once every NOTICE_HOURS — this runs
    ~96 times a day and an unconfigured machine must not write 96 identical
    lines."""
    try:
        if NOTICE_PATH.exists():
            age_h = (time.time() - NOTICE_PATH.stat().st_mtime) / 3600
            if age_h < NOTICE_HOURS:
                return
        NOTICE_PATH.parent.mkdir(parents=True, exist_ok=True)
        NOTICE_PATH.write_text(msg, encoding="utf-8")
    except Exception:
        pass
    _log(msg)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def _load_state() -> dict:
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        state = dict(_EMPTY_STATE)
        state.update({k: raw[k] for k in _EMPTY_STATE if k in raw})
        return state
    except FileNotFoundError:
        return dict(_EMPTY_STATE)
    except Exception:
        _log("state file unreadable — starting fresh at the base interval")
        return dict(_EMPTY_STATE)


def _save_state(state: dict) -> None:
    """Atomic: a crash mid-write must not leave a truncated file that reads as
    "no back-off" the next time Audible is refusing us."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def interval_minutes(state: dict) -> int:
    """The current cadence, clamped into [BASE, MAX] however the file reads.
    A hand-edited or corrupt value must never produce a 0-minute interval."""
    try:
        got = int(state.get("interval_minutes") or BASE_MINUTES)
    except (TypeError, ValueError):
        got = BASE_MINUTES
    return max(BASE_MINUTES, min(MAX_MINUTES, got))


def next_interval(current: int) -> int:
    """15 → 30 → 60 → 60. Doubling, capped."""
    return min(MAX_MINUTES, max(BASE_MINUTES, current) * 2)


def _log_throttle(state: dict, elapsed_min: float, interval: int) -> None:
    """Say that a tick was throttled, ONCE per throttled window.

    Same rule (and the same reasoning) as ``drive_poll._log_throttle``: a tick
    that does nothing must say why, but a 15-minute cadence must not repeat
    itself. Keyed on the ``last_attempt`` it belongs to, so the first throttled
    tick after a real one speaks and the rest of that window is silent.

    ⚠️ It names the interval, because that is how a reader tells a NORMAL
    throttle from a BACKED-OFF one without opening the state file.
    """
    marker = float(state.get("last_attempt") or 0.0)
    try:
        if float(state.get("throttle_logged_for") or -1.0) == marker:
            return
    except (TypeError, ValueError):
        pass
    remaining = max(0.0, interval - elapsed_min)
    why = "backed off" if interval > BASE_MINUTES else "cadence floor"
    _log(f"skipped — {why}, interval is {interval} min "
         f"({state.get('consecutive_errors', 0)} consecutive error(s)), last tick "
         f"{elapsed_min:.1f} min ago, next in {remaining:.1f} min. Audible was not contacted.")
    try:
        state["throttle_logged_for"] = marker
        _save_state(state)
    except Exception:
        pass


def _tick_lock_held() -> bool:
    """True if another tick is in flight — a tick that downloads a 1 GB book
    can outlive the interval. Same mtime-staleness pattern as fs_watcher."""
    if not TICK_LOCK_PATH.exists():
        return False
    age_h = (time.time() - TICK_LOCK_PATH.stat().st_mtime) / 3600
    if age_h > STALE_TICK_LOCK_HOURS:
        _log(f"clearing stale tick lock ({age_h:.1f}h old)")
        TICK_LOCK_PATH.unlink(missing_ok=True)
        return False
    return True


# ---------------------------------------------------------------------------
# Is the 8-hourly run's acquisition stage up? (see the module docstring)
# ---------------------------------------------------------------------------


def sync_task_running() -> bool | None:
    """True/False if the ``AudiobookSyncPipeline`` task's Status could be read;
    None when it could not (no such task, schtasks missing, a parse we do not
    recognise).

    ⚠️ Read-only, always: ``/query`` only. Nothing in this module may change,
    start, stop or delete a scheduled task.

    ⚠️ Unknown is NOT treated as running. Failing closed here would mean that
    a machine where schtasks answers oddly never audits again and says so only
    every six hours; the pipeline lock still covers the larger half of the
    window, and an unknown is reported rather than silently assumed either way.
    """
    try:
        proc = subprocess.run(
            ["schtasks", "/query", "/tn", SYNC_TASK_NAME, "/fo", "LIST"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    for line in (proc.stdout or "").splitlines():
        if line.lower().startswith("status:"):
            return line.split(":", 1)[1].strip().lower() == "running"
    return None


# ---------------------------------------------------------------------------
# Running the audit, and reading what it said
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TickOutcome:
    """What one invocation of auto_acquire actually did."""

    ok: bool               # False -> back off
    downloaded: int
    failed: int
    summary: str           # the one line that lands in the log
    titles: tuple[str, ...] = ()


def classify(rc: int | None, stdout: str) -> TickOutcome:
    """Read auto_acquire's output. Pure, so every branch is testable without
    Audible.

    ``rc is None`` means the subprocess timed out.

    ⚠️ An ``[audible-cli] export failed`` line is a FAILING tick even when the
    exit code is 0. ``audit_new_purchases`` falls back to the container's
    ``books.json`` when every profile's export fails, so the run can succeed
    against a list that is hours or days old — a fresh-looking "0 missing" that
    means "we could not ask Audible". That is precisely the condition (a
    throttle, an expired registration) that back-off is for, and the exit code
    does not carry it.
    """
    text = stdout or ""
    lines = text.splitlines()

    if rc is None:
        return TickOutcome(False, 0, 0, f"FAILED — audit timed out after {AUDIT_TIMEOUT_S}s")

    downloaded = [ln.split("DOWNLOADED:", 1)[1].strip()
                  for ln in lines if ln.startswith("DOWNLOADED:")]
    failed = [ln.split("FAILED:", 1)[1].strip()
              for ln in lines if ln.startswith("FAILED:")]

    cli_failures = [ln.strip() for ln in lines
                    if any(m in ln for m in _CLI_FAILURE_MARKERS)]
    if cli_failures:
        return TickOutcome(
            False, len(downloaded), len(failed),
            "FAILED — audible-cli could not export a library list "
            f"({cli_failures[0][:160]}); the audit fell back to a possibly stale list",
            tuple(downloaded),
        )

    if rc == 2:
        return TickOutcome(False, 0, 0,
                           "FAILED — no purchase source available (no audible-cli profile "
                           "and no books.json); nothing was audited")

    if failed:
        return TickOutcome(
            False, len(downloaded), len(failed),
            f"{len(downloaded)} downloaded, {len(failed)} FAILED — {failed[0][:160]}",
            tuple(downloaded),
        )

    if downloaded:
        shown = ", ".join(downloaded[:3])
        more = f" (+{len(downloaded) - 3} more)" if len(downloaded) > 3 else ""
        return TickOutcome(True, len(downloaded), 0,
                           f"found {len(downloaded)} new purchase(s), downloaded: {shown}{more}",
                           tuple(downloaded))

    if rc == 1:
        # --no-download (the dry run) reports missing purchases this way. Not a
        # failure: it is the answer we asked for.
        missing = [ln.strip() for ln in lines if "[MISSING]" in ln]
        shown = "; ".join(m.split("[MISSING]", 1)[1].strip() for m in missing[:3])
        return TickOutcome(True, 0, 0,
                           f"{len(missing)} new purchase(s) NOT downloaded (report-only): {shown}")

    return TickOutcome(True, 0, 0, "0 new — library is current")


def run_purchase_audit(download: bool = True) -> TickOutcome:
    """Run the 8h pipeline's own acquisition command as a subprocess.

    ⚠️ The argument list is deliberately the one in
    ``scripts/sync_pipeline_8h.bat`` (``--notify --stop-after``), with
    ``--no-download`` added for the dry run and nothing else. Two cadences
    running the same audit with different flags would be two behaviours wearing
    one name.
    """
    cmd = [sys.executable, "-m", "app.tools.auto_acquire", "--notify", "--stop-after"]
    if not download:
        cmd.append("--no-download")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.run(
            cmd, cwd=str(PROJECT_ROOT), env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=AUDIT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return classify(None, "")
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    outcome = classify(proc.returncode, out)
    if not outcome.ok:
        tail = "\n".join(ln for ln in out.splitlines() if ln.strip())[-800:]
        _log(f"auto_acquire exited {proc.returncode}; last output:\n{tail}")
    return outcome


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------


def poll_once(dry_run: bool = False) -> int:
    """One tick. Returns 0 in every normal case — Task Scheduler runs this ~96
    times a day and a nonzero exit for a routine condition (throttled,
    deferred, backed off) would show the task permanently failed."""
    if _tick_lock_held():
        _log("skipped — another tick is still running (a download in flight)")
        return 0
    TICK_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    TICK_LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
    try:
        return _tick(dry_run=dry_run)
    finally:
        TICK_LOCK_PATH.unlink(missing_ok=True)


def _tick(dry_run: bool = False) -> int:
    if not _flag("PURCHASE_AUDIT_ENABLED"):
        _notice("idle — PURCHASE_AUDIT_ENABLED=0 (kill switch)")
        return 0

    state = _load_state()
    interval = interval_minutes(state)

    last_attempt = float(state.get("last_attempt") or 0.0)
    elapsed_min = (_now() - last_attempt) / 60.0
    if not dry_run and elapsed_min < interval:
        _log_throttle(state, elapsed_min, interval)
        return 0

    # ⚠️ DEFER, DON'T COLLIDE — and do NOT touch the back-off or last_attempt.
    # A deferral is not a failure and not a completed tick: the next tick
    # should try again as soon as the way is clear.
    if pipeline_lock.LOCK_PATH.exists():
        holder = pipeline_lock.current_holder()
        who = holder.describe() if holder else "unreadable lock file"
        _log(f"skipped-locked — a pipeline run is in flight ({who}). "
             "Nothing audited; the back-off is unchanged.")
        return 0

    running = sync_task_running()
    if running:
        _log(f"skipped-locked — the {SYNC_TASK_NAME} task is Running, and its acquisition "
             "stage takes no lock. Nothing audited; the back-off is unchanged.")
        return 0
    if running is None:
        _notice(f"could not read the {SYNC_TASK_NAME} task's status (schtasks /query). "
                "Auditing anyway — the pipeline lock still covers a run in flight, but the "
                "8h acquisition window is unguarded until this reads again.")

    outcome = run_purchase_audit(download=not dry_run)

    if dry_run:
        _log(f"DRY-RUN — {outcome.summary}. Nothing downloaded, no run queued, "
             f"back-off untouched (interval stays {interval} min).")
        return 0

    now = _now()
    state["last_attempt"] = now
    state["last_result"] = outcome.summary

    if outcome.ok:
        if interval != BASE_MINUTES:
            _log(f"clean tick — resetting the interval {interval} → {BASE_MINUTES} min")
        state["interval_minutes"] = BASE_MINUTES
        state["consecutive_errors"] = 0
        state["last_error"] = None
    else:
        bumped = next_interval(interval)
        state["interval_minutes"] = bumped
        state["consecutive_errors"] = int(state.get("consecutive_errors") or 0) + 1
        state["last_error"] = outcome.summary
        when = datetime.fromtimestamp(now + bumped * 60).strftime("%Y-%m-%d %H:%M")
        _log(f"backed-off-until {when} — interval {interval} → {bumped} min "
             f"({state['consecutive_errors']} consecutive error(s))")

    if outcome.downloaded:
        state["last_download_at"] = datetime.now(timezone.utc).isoformat()
        state["last_downloaded"] = outcome.downloaded
        state["total_downloaded"] = int(state.get("total_downloaded") or 0) + outcome.downloaded

    _log(outcome.summary)
    _save_state(state)

    # ⚠️ The hand-off. See the module docstring: the download landed in the
    # container books dir, which NO watcher watches — only sync_to_drive.py's
    # sort step reads it. Without this the book would wait for the next 8h run.
    if outcome.downloaded:
        shown = ", ".join(outcome.titles[:3])
        more = f" (+{len(outcome.titles) - 3} more)" if len(outcome.titles) > 3 else ""
        pipeline_requests.request_run(
            f"{outcome.downloaded} new purchase(s) downloaded from Audible ({shown}{more})",
            source="purchase-audit", log=_log, notice=_notice,
        )
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_status() -> None:
    state = _load_state()
    interval = interval_minutes(state)
    holder = pipeline_lock.current_holder()
    last = float(state.get("last_attempt") or 0.0)
    when = f"{(_now() - last) / 60.0:.1f} min ago" if last else "never"
    running = sync_task_running()
    print(f"  enabled        : {'yes' if _flag('PURCHASE_AUDIT_ENABLED') else 'NO (PURCHASE_AUDIT_ENABLED=0)'}")
    print(f"  interval       : {interval} min"
          f"{' (BACKED OFF from ' + str(BASE_MINUTES) + ')' if interval > BASE_MINUTES else ''}")
    print(f"  last tick      : {when}")
    print(f"  last result    : {state.get('last_result') or 'never run'}")
    print(f"  errors in a row: {state.get('consecutive_errors', 0)}")
    print(f"  last error     : {state.get('last_error') or 'none'}")
    print(f"  last download  : {state.get('last_download_at') or 'never'} "
          f"({state.get('last_downloaded', 0)} book(s); {state.get('total_downloaded', 0)} all-time)")
    print(f"  8h task        : {SYNC_TASK_NAME} "
          f"{'RUNNING' if running else ('not running' if running is False else 'status unreadable')}")
    print(f"  pipeline lock  : {holder.describe() if holder else ('held (unreadable)' if pipeline_lock.LOCK_PATH.exists() else 'free')}")
    print(f"  trigger token  : {'set' if (os.getenv('PIPELINE_TRIGGER_TOKEN') or '').strip() else 'NOT SET'}")
    print(f"  state file     : {STATE_PATH}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="One tick of the Audible purchase audit (15 min, with back-off)",
    )
    parser.add_argument("--status", action="store_true",
                        help="Show configuration/state and exit; audits nothing")
    parser.add_argument("--dry-run", action="store_true",
                        help=("Audit and report only: nothing is downloaded, no pipeline run "
                              "is queued, and the interval/back-off is left exactly as it was."))
    args = parser.parse_args()
    if args.status:
        _print_status()
        return 0
    return poll_once(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
