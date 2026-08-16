"""Consume manual "run the pipeline now" requests from Firestore.

Why polling rather than an inbound trigger: the pipeline runs on a home machine
behind a router, and the repo is PUBLIC — a self-hosted GitHub Actions runner
would let any fork PR execute code on it. Polling needs no open port and no
inbound trust at all.

Security model (see the pipeline_requests block in firestore.rules):
  * Firestore rules make pipeline_requests create-only and UNREADABLE, so the
    shared token inside a pending request cannot be harvested by a third party.
  * This watcher holds the same token in .env and refuses to run anything that
    does not match. A forged request is deleted, never executed.
  * Requests older than MAX_REQUEST_AGE_MIN are ignored (no replay of a stale
    doc after the machine has been off for a day).
  * COOLDOWN_MIN throttles back-to-back runs so a spammer — or an impatient
    double-click — cannot queue up overlapping pipelines.
  * A lock file stops two watcher ticks overlapping if a run outlives the
    poll interval.

Usage:
    python -m app.tools.pipeline_watcher           # one poll, then exit
    python -m app.tools.pipeline_watcher --status  # show config, poll nothing
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from app.config import PROJECT_ROOT
from app.pipeline_status import _client, _lane_suffix  # shared credential path

COOLDOWN_MIN = int(os.getenv("PIPELINE_COOLDOWN_MIN", "10"))
MAX_REQUEST_AGE_MIN = int(os.getenv("PIPELINE_MAX_REQUEST_AGE_MIN", "60"))
LOCK_PATH = PROJECT_ROOT / "output_files" / "pipeline_watcher.lock"
LAST_RUN_PATH = PROJECT_ROOT / "output_files" / "pipeline_last_manual_run.txt"
LOG_PATH = PROJECT_ROOT / "output_files" / "pipeline_8h.log"
STALE_LOCK_HOURS = 6

# Fine-grained manual step controls (owner ask 2026-08-16, catalog-platform
# /status Operations section) — a `pipeline_requests` doc may now carry an
# OPTIONAL `step` field alongside token/requestedAt/requestedBy. Deliberately
# hardcoded here rather than imported from scripts.sync_to_drive: this
# module's whole design is "know as little as possible about the pipeline
# internals, just orchestrate subprocesses" (it never imports sync_to_drive
# even for a full run — see _run_pipeline()). MUST mirror
# scripts/sync_to_drive.py's STEP_INFO keys exactly — a
# tests/test_pipeline_watcher.py assertion pins the two in sync.
PIPELINE_STEP_CHOICES = frozenset({"audit", "sort", "detect", "folders", "upload", "catalog", "publish"})
# The standalone "force full upload to the shelf server" control (see
# scripts/sync_to_server.py) — NOT a pipeline step (no entry in
# PIPELINE_STEP_CHOICES / STEP_INFO), recognized as its own special marker.
FORCE_UPLOAD_STEP = "force-upload-server"


def _token() -> str:
    return (os.getenv("PIPELINE_TRIGGER_TOKEN") or "").strip()


NOTICE_HOURS = 6
NOTICE_PATH = PROJECT_ROOT / "output_files" / "pipeline_watcher_notice.txt"


def _log(msg: str) -> None:
    print(f"[watcher] {datetime.now().strftime('%Y-%m-%d %H:%M')} {msg}", flush=True)


def _notice(msg: str) -> None:
    """Log a recurring condition at most once every NOTICE_HOURS."""
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


def _lock_held() -> bool:
    """True if another run is in flight. Clears locks left by a crash."""
    if not LOCK_PATH.exists():
        return False
    age_h = (time.time() - LOCK_PATH.stat().st_mtime) / 3600
    if age_h > STALE_LOCK_HOURS:
        _log(f"clearing stale lock ({age_h:.1f}h old)")
        LOCK_PATH.unlink(missing_ok=True)
        return False
    return True


def _cooldown_remaining() -> float:
    """Minutes left before another manual run is allowed."""
    if not LAST_RUN_PATH.exists():
        return 0.0
    try:
        last = datetime.fromisoformat(LAST_RUN_PATH.read_text(encoding="utf-8").strip())
    except Exception:
        return 0.0
    elapsed = (datetime.now(timezone.utc) - last).total_seconds() / 60
    return max(0.0, COOLDOWN_MIN - elapsed)


def _run_pipeline() -> int:
    """Run the same two commands as the scheduled task, logging to the same file."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PIPELINE_TRIGGER="manual")
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    try:
        with open(LOG_PATH, "a", encoding="utf-8", errors="replace") as log:
            log.write(f"\n================= MANUAL RUN {datetime.now()} =================\n")
            log.flush()
            for cmd in (
                [sys.executable, "-m", "app.tools.auto_acquire", "--notify", "--stop-after"],
                [sys.executable, str(PROJECT_ROOT / "scripts" / "sync_to_drive.py")],
            ):
                _log(f"running: {' '.join(cmd[1:])}")
                rc = subprocess.call(cmd, cwd=str(PROJECT_ROOT), env=env, stdout=log, stderr=log)
                _log(f"  exit={rc}")
        LAST_RUN_PATH.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
        return 0
    finally:
        LOCK_PATH.unlink(missing_ok=True)


def _run_pipeline_step(step: str) -> int:
    """Run ONE pipeline stage via `sync_to_drive.py --step <step>` — a single
    subprocess instead of _run_pipeline()'s two-command chain (there is no
    need to also run auto_acquire for an isolated step; the step itself
    decides what it touches). Same watcher-tick lock file, same log file.
    The actual cross-run safety guarantee (never overlapping the 8h
    scheduled run or another manual invocation) comes from
    scripts/sync_to_drive.py's run_step() taking app/core/pipeline_lock.py's
    single-flight lock internally — this function's LOCK_PATH only stops two
    watcher TICKS overlapping, same role it already plays for _run_pipeline().
    """
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PIPELINE_TRIGGER="manual")
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    try:
        with open(LOG_PATH, "a", encoding="utf-8", errors="replace") as log:
            log.write(f"\n================= MANUAL STEP '{step}' {datetime.now()} =================\n")
            log.flush()
            cmd = [sys.executable, str(PROJECT_ROOT / "scripts" / "sync_to_drive.py"), "--step", step]
            _log(f"running: {' '.join(cmd[1:])}")
            rc = subprocess.call(cmd, cwd=str(PROJECT_ROOT), env=env, stdout=log, stderr=log)
            _log(f"  exit={rc}")
        LAST_RUN_PATH.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
        return rc
    finally:
        LOCK_PATH.unlink(missing_ok=True)


def _run_force_upload() -> int:
    """Run the standalone shelf-server reconciliation (scripts/
    sync_to_server.py) — NOT a pipeline step. Uses the same watcher-tick
    lock/log as every other subprocess this watcher runs; sync_to_server.py
    takes its own copy of app/core/pipeline_lock.py's single-flight lock
    internally (defense in depth — see that script's run_locked())."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    try:
        with open(LOG_PATH, "a", encoding="utf-8", errors="replace") as log:
            log.write(f"\n================= FORCE UPLOAD TO SHELF SERVER {datetime.now()} =================\n")
            log.flush()
            cmd = [sys.executable, str(PROJECT_ROOT / "scripts" / "sync_to_server.py")]
            _log(f"running: {' '.join(cmd[1:])}")
            rc = subprocess.call(cmd, cwd=str(PROJECT_ROOT), env=env, stdout=log, stderr=log)
            _log(f"  exit={rc}")
        LAST_RUN_PATH.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
        return rc
    finally:
        LOCK_PATH.unlink(missing_ok=True)


def poll_once() -> int:
    """Check for a valid pending request and run the pipeline if there is one.

    Returns 0 = nothing to do / ran fine, 1 = misconfigured.
    """
    # Unconfigured is a normal state (fresh clone, credentials not added yet).
    # This runs every few minutes, so complain at most once every NOTICE_HOURS
    # and still exit 0 — otherwise the scheduled task shows permanently failed
    # and the log fills with the same line 480 times a day.
    token = _token()
    db = _client() if token else None
    if not token or db is None:
        why = "PIPELINE_TRIGGER_TOKEN not set in .env" if not token else "no Firestore credentials"
        _notice(f"idle — {why} (see docs/access/FIREBASE.md)")
        return 0

    coll = f"pipeline_requests{_lane_suffix()}"
    try:
        docs = list(db.collection(coll).limit(25).stream())
    except Exception as e:
        _log(f"could not read {coll}: {type(e).__name__}: {e}")
        return 1

    if not docs:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=MAX_REQUEST_AGE_MIN)
    valid = []
    for d in docs:
        data = d.to_dict() or {}
        # Constant-time-ish compare; these are short strings so the practical
        # risk is nil, but there is no reason to leak a prefix match either.
        import hmac
        if not hmac.compare_digest(str(data.get("token", "")), token):
            _log(f"discarding request {d.id}: bad token (from {data.get('requestedBy', '?')})")
            d.reference.delete()
            continue
        try:
            when = datetime.fromisoformat(str(data.get("requestedAt", "")).replace("Z", "+00:00"))
        except Exception:
            when = None
        if when is None or when < cutoff:
            _log(f"discarding request {d.id}: stale or unparseable timestamp")
            d.reference.delete()
            continue
        # Optional `step` field (fine-grained manual controls, 2026-08-16) —
        # blank/absent means "run the whole pipeline" (unchanged behaviour).
        # Anything present that isn't a known step or the force-upload
        # marker is a malformed/forged request — discard it the same as a
        # bad token, never guess what it meant.
        step = data.get("step") or None
        if step is not None and step not in PIPELINE_STEP_CHOICES and step != FORCE_UPLOAD_STEP:
            _log(f"discarding request {d.id}: unknown step {step!r}")
            d.reference.delete()
            continue
        valid.append((d, data))

    if not valid:
        return 0

    # Consume every valid request up front — a double-click must not queue two
    # runs, and if this run dies the request should not be retried forever.
    for d, _data in valid:
        d.reference.delete()

    if _lock_held():
        _log("a pipeline run is already in flight — request consumed, not re-running")
        return 0

    remaining = _cooldown_remaining()
    if remaining > 0:
        _log(f"cooldown active, {remaining:.1f} min left — request consumed, not running")
        return 0

    who = valid[0][1].get("requestedBy", "?")
    step = valid[0][1].get("step") or None
    if step == FORCE_UPLOAD_STEP:
        _log(f"valid force-upload-to-server request from {who} — running")
        _run_force_upload()
        _log("force-upload finished")
    elif step:
        _log(f"valid step request from {who} — running step '{step}'")
        _run_pipeline_step(step)
        _log(f"step '{step}' finished")
    else:
        _log(f"valid request from {who} — starting pipeline")
        _run_pipeline()
        _log("pipeline finished")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Poll Firestore for manual pipeline-run requests")
    parser.add_argument("--status", action="store_true", help="Show configuration and exit")
    args = parser.parse_args()

    if args.status:
        print(f"  token set     : {'yes' if _token() else 'NO'}")
        print(f"  firestore     : {'connected' if _client() is not None else 'unavailable'}")
        print(f"  collection    : pipeline_requests{_lane_suffix()}")
        print(f"  cooldown      : {COOLDOWN_MIN} min ({_cooldown_remaining():.1f} left)")
        print(f"  max req age   : {MAX_REQUEST_AGE_MIN} min")
        print(f"  run in flight : {'yes' if _lock_held() else 'no'}")
        return 0

    return poll_once()


if __name__ == "__main__":
    sys.exit(main())
