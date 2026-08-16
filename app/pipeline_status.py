# app/pipeline_status.py
# Publish live pipeline status to Firestore so the admin panel can show what
# the 8-hourly run is doing without anyone sitting at the machine.
#
# Design rules:
#   1. NEVER raise. The pipeline moves audiobooks and deploys a site; a status
#      backend being down, misconfigured or absent must not cost a run. Every
#      public function swallows its own errors and degrades to a no-op.
#   2. No credentials => silent no-op. A fresh clone with no service account
#      behaves exactly as before this module existed.
#   3. Writes are throttled (see MIN_WRITE_INTERVAL_S) because step 4 reports
#      upload progress per chunk and Firestore bills per write.
#
# Collections (lane-suffixed to match site/fb-env.js — the /dev/ Pages lane and
# localhost read *_dev, so a dev run never overwrites the prod status card):
#
#   pipeline_status/current   one doc, overwritten — what is happening NOW
#   pipeline_runs/{run_id}    one doc per run — history for the panel
#
# Credentials: a Firebase service-account JSON at scripts/firebase_service_account.json
# (gitignored), or the path in FIREBASE_SERVICE_ACCOUNT. See docs/access/FIREBASE.md.

from __future__ import annotations

import os
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import PROJECT_ROOT

# Step 0-6 mirror scripts/sync_to_drive.py run_pipeline(). Kept here so the UI
# can render the whole list greyed out before the run reaches each one.
STEPS: list[tuple[str, str]] = [
    ("audit", "Purchase audit"),
    ("sort", "Sort books"),
    ("detect", "Detect new books"),
    ("folders", "Read Drive folders"),
    ("upload", "Upload to Drive"),
    ("catalog", "Rebuild catalog"),
    ("publish", "Commit & deploy"),
]

DEFAULT_KEY_PATH = PROJECT_ROOT / "scripts" / "firebase_service_account.json"
MIN_WRITE_INTERVAL_S = 3.0  # throttle upload-progress chatter

_db: Any = None
_init_tried = False
_run_id: str | None = None
_run_started: str | None = None
_state: dict[str, Any] = {}
_last_write = 0.0
_disabled_reason: str | None = None


def _lane_suffix() -> str:
    """'' for prod, '_dev' for dev runs — mirrors site/fb-env.js col()."""
    return "_dev" if os.getenv("PIPELINE_LANE", "").lower() == "dev" else ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _client():
    """Lazily build the Firestore client. Returns None if unavailable."""
    global _db, _init_tried, _disabled_reason
    if _init_tried:
        return _db
    _init_tried = True

    key_path = Path(os.getenv("FIREBASE_SERVICE_ACCOUNT") or DEFAULT_KEY_PATH)
    if not key_path.exists():
        _disabled_reason = f"no service account at {key_path}"
        return None

    try:
        import firebase_admin
        from firebase_admin import credentials, firestore

        if not firebase_admin._apps:
            firebase_admin.initialize_app(credentials.Certificate(str(key_path)))
        _db = firestore.client()
    except ImportError:
        _disabled_reason = "firebase-admin not installed (pip install firebase-admin)"
    except Exception as e:  # bad key, no network, project mismatch …
        _disabled_reason = f"{type(e).__name__}: {e}"
    return _db


def _push(force: bool = False) -> None:
    """Write _state to pipeline_status/current. Throttled unless force."""
    global _last_write
    db = _client()
    if db is None:
        return
    if not force and (time.time() - _last_write) < MIN_WRITE_INTERVAL_S:
        return
    try:
        db.collection(f"pipeline_status{_lane_suffix()}").document("current").set(_state)
        _last_write = time.time()
    except Exception:
        pass  # never let telemetry break a run


# --------------------------------------------------------------------------
# Public API — all no-ops when Firestore is unavailable
# --------------------------------------------------------------------------
def start_run(trigger: str = "scheduled") -> str:
    """Begin a run. trigger: 'scheduled' | 'manual' | 'cli'."""
    global _run_id, _run_started, _state
    try:
        _run_started = _now()
        _run_id = _run_started.replace(":", "").replace("-", "")[:15]
        _state = {
            "runId": _run_id,
            "state": "running",
            "trigger": trigger,
            "startedAt": _run_started,
            "updatedAt": _run_started,
            "host": os.getenv("COMPUTERNAME") or "unknown",
            "stepIndex": -1,
            "stepKey": None,
            "stepLabel": None,
            "steps": [{"key": k, "label": lbl, "state": "pending", "detail": ""} for k, lbl in STEPS],
            "progress": None,
            "error": None,
            "summary": {},
        }
        _push(force=True)
    except Exception:
        pass
    return _run_id or ""


def step(key: str, detail: str = "") -> None:
    """Mark a step active; everything before it becomes done."""
    try:
        if not _state:
            return
        idx = next((i for i, (k, _) in enumerate(STEPS) if k == key), None)
        if idx is None:
            return
        for i, s in enumerate(_state["steps"]):
            if i < idx and s["state"] in ("pending", "active"):
                s["state"] = "done"
            elif i == idx:
                s["state"] = "active"
                if detail:
                    s["detail"] = detail
        _state.update(
            stepIndex=idx, stepKey=key, stepLabel=STEPS[idx][1],
            progress=None, updatedAt=_now(),
        )
        _push(force=True)
    except Exception:
        pass


def step_detail(key: str, detail: str) -> None:
    """Update a step's one-line detail ('3 sorted', '397 folders cached')."""
    try:
        if not _state:
            return
        for s in _state["steps"]:
            if s["key"] == key:
                s["detail"] = detail
        _state["updatedAt"] = _now()
        _push(force=True)
    except Exception:
        pass


def upload_progress(file_name: str, pct: int, index: int, total: int, size_mb: float = 0.0) -> None:
    """Per-file upload progress. Throttled — safe to call every chunk."""
    try:
        if not _state:
            return
        _state["progress"] = {
            "file": file_name, "pct": int(pct), "index": index,
            "total": total, "sizeMb": round(size_mb, 1),
        }
        _state["updatedAt"] = _now()
        _push()  # throttled
    except Exception:
        pass


def set_summary(**fields: Any) -> None:
    """Accumulate run totals (uploaded, sorted, books, newBooks …)."""
    try:
        if not _state:
            return
        _state.setdefault("summary", {}).update(fields)
        _state["updatedAt"] = _now()
        _push(force=True)
    except Exception:
        pass


def finish_run(state: str = "success", error: str | None = None) -> None:
    """Close the run: flip the status doc and append to pipeline_runs history."""
    try:
        if not _state:
            return
        for s in _state["steps"]:
            if s["state"] == "active":
                s["state"] = "done" if state == "success" else "failed"
        # An idle run legitimately stops at step 2. Leaving the rest "pending"
        # on a SUCCESS card reads as though the run stalled, so distinguish
        # "never reached, and that's fine" from "still to come".
        if state != "running":
            for s in _state["steps"]:
                if s["state"] == "pending":
                    s["state"] = "skipped"
        _state.update(
            state=state, error=error, progress=None,
            finishedAt=_now(), updatedAt=_now(),
        )
        if _run_started:
            try:
                started = datetime.fromisoformat(_run_started)
                _state["durationSec"] = round(
                    (datetime.now(timezone.utc) - started).total_seconds()
                )
            except Exception:
                pass
        _push(force=True)

        db = _client()
        if db is not None and _run_id:
            try:
                db.collection(f"pipeline_runs{_lane_suffix()}").document(_run_id).set(_state)
            except Exception:
                pass
    except Exception:
        pass


def fail_run(exc: BaseException) -> None:
    """Record a crash, including the traceback tail, then close the run."""
    try:
        tail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        finish_run(state="failed", error=tail[:500])
    except Exception:
        pass


# --------------------------------------------------------------------------
# Fine-grained manual step controls (owner ask 2026-08-16, catalog-platform
# /status Operations section: "give us fine control over each part of the
# pipeline in case we need to do part way steps"). See
# scripts/sync_to_drive.py's run_step()/STEP_INFO for the dispatcher this
# backs.
# --------------------------------------------------------------------------
def start_step_run(step_key: str, step_label: str, trigger: str) -> str:
    """Begin a STANDALONE single-step run. Deliberately NOT start_run(): that
    function scaffolds the full 7-entry STEPS list and its companion step()
    marks every entry BEFORE the given index 'done' on the assumption of
    sequential progression through the whole pipeline — true for a full run,
    false for an isolated step. Running 'upload' alone must never make the
    status page claim 'sort' also ran just now. So this scaffolds only the
    ONE step that is actually running, already 'active' (no separate call to
    step() needed). finish_run() itself needs no change: it walks
    _state['steps'] generically by state, not by position, so closing a
    single-step card correctly marks that one step done/failed and leaves
    nothing 'pending' to fall through to 'skipped'."""
    global _run_id, _run_started, _state
    try:
        _run_started = _now()
        _run_id = _run_started.replace(":", "").replace("-", "")[:15]
        _state = {
            "runId": _run_id,
            "state": "running",
            "trigger": trigger,
            "startedAt": _run_started,
            "updatedAt": _run_started,
            "host": os.getenv("COMPUTERNAME") or "unknown",
            "stepIndex": 0,
            "stepKey": step_key,
            "stepLabel": step_label,
            "steps": [{"key": step_key, "label": step_label, "state": "active", "detail": ""}],
            "progress": None,
            "error": None,
            "summary": {},
        }
        _push(force=True)
    except Exception:
        pass
    return _run_id or ""


def force_upload_result(ok: bool, configured: bool, reachable: bool | None, message: str) -> None:
    """Publish the standalone 'force full upload to the shelf server' result
    (owner ask 2026-08-16; see scripts/sync_to_server.py) to ITS OWN doc,
    `shelf_upload_status/current` — deliberately NOT `pipeline_status/current`.
    That doc is the pipeline's own health signal (the /status page's primary
    'Automated Book Pipeline' row); overwriting it here would make a
    force-upload run masquerade as a pipeline outcome. Same never-raise,
    silent-no-op-without-credentials contract as every other function in this
    module — a status-backend outage must never cost the underlying transfer.
    """
    db = _client()
    if db is None:
        return
    try:
        state = (
            "not_configured" if not configured
            else "unreachable" if not reachable
            else "success" if ok
            else "failed"
        )
        doc = {
            "ok": ok,
            "state": state,
            "message": message,
            "updatedAt": _now(),
            "host": os.getenv("COMPUTERNAME") or "unknown",
        }
        db.collection(f"shelf_upload_status{_lane_suffix()}").document("current").set(doc)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Single-flight lock reporting (2026-08-16, docs/info/ROLES.md §1c/§1d) —
# "a blocked run must FAIL LOUDLY AND VISIBLE, never silently no-op — print
# who holds the lock and since when, and write it to pipeline_status so the
# /status page can show it." These write pipeline_status/current directly
# (self-contained snapshots; they don't go through start_run()/_state since
# no run actually began) and, for the abandon case, a pipeline_runs history
# entry too — a skipped scheduled slot must be as visible in history as a
# failed run, never a silent gap. Same "never raise" contract as the rest of
# this module.
# --------------------------------------------------------------------------
def blocked_run(trigger: str, holder_desc: str) -> None:
    """A run refused to start outright because app/core/pipeline_lock is
    held by someone else. Used by every non-scheduled entry point (manual,
    --rebuild-only, the watcher's manual trigger) — none of them retry, they
    fail immediately. See deferral_abandoned() for the scheduled trigger,
    which retries for up to 2h before giving up."""
    global _state
    try:
        now = _now()
        _state = {
            "runId": None,
            "state": "blocked",
            "trigger": trigger,
            "startedAt": now,
            "updatedAt": now,
            "finishedAt": now,
            "host": os.getenv("COMPUTERNAME") or "unknown",
            "stepIndex": -1,
            "stepKey": None,
            "stepLabel": None,
            "steps": [],
            "progress": None,
            "error": f"Blocked: pipeline lock held by {holder_desc}. Refused to start (trigger={trigger} does not retry).",
            "summary": {},
        }
        _push(force=True)
    except Exception:
        pass


def deferring(trigger: str, holder_desc: str, deferring_since: str, attempt: int, note: str = "") -> None:
    """Heartbeat published while the SCHEDULED trigger retries against a
    held lock (app/core/pipeline_schedule.py). Overwrites
    pipeline_status/current on every attempt; no history entry yet — one is
    written only once the deferral resolves, either here-adjacent via
    deferral_abandoned() or by the normal start_run()/finish_run() pair once
    the lock clears and the real run begins."""
    global _state
    try:
        now = _now()
        _state = {
            "runId": None,
            "state": "deferred",
            "trigger": trigger,
            "startedAt": deferring_since,
            "updatedAt": now,
            "finishedAt": None,
            "host": os.getenv("COMPUTERNAME") or "unknown",
            "stepIndex": -1,
            "stepKey": None,
            "stepLabel": None,
            "steps": [],
            "progress": None,
            "error": f"Deferred (attempt {attempt}): blocked by {holder_desc}. {note}".strip(),
            "summary": {"deferringSince": deferring_since, "attempt": attempt},
        }
        _push(force=True)
    except Exception:
        pass


def deferral_abandoned(trigger: str, holder_desc: str, deferring_since: str, attempts: int, recovered: bool = False) -> None:
    """The 2h defer window elapsed still blocked (or — 'recovered' — a
    crashed deferral's overdue abandonment is being surfaced late by a
    later invocation, so it is never silently lost). Writes BOTH the live
    card and a real pipeline_runs history entry: a skipped scheduled slot
    must be as visible in history as a failed run."""
    global _state
    try:
        now = _now()
        run_id = deferring_since.replace(":", "").replace("-", "")[:15] + "-deferred"
        if recovered:
            prefix = "RECOVERED (an earlier deferral crashed without logging its own abandonment): "
        else:
            prefix = ""
        error = (
            f"{prefix}ABANDONED after {attempts} retr{'y' if attempts == 1 else 'ies'} "
            f"over up to 2h: still blocked by {holder_desc} when the defer window "
            f"elapsed (deferring since {deferring_since}). This scheduled slot was "
            "skipped — the NEXT 8h slot proceeds normally on its original schedule."
        )
        _state = {
            "runId": run_id,
            "state": "skipped",
            "trigger": trigger,
            "startedAt": deferring_since,
            "updatedAt": now,
            "finishedAt": now,
            "host": os.getenv("COMPUTERNAME") or "unknown",
            "stepIndex": -1,
            "stepKey": None,
            "stepLabel": None,
            "steps": [],
            "progress": None,
            "error": error,
            "summary": {"deferringSince": deferring_since, "attempts": attempts},
        }
        try:
            started = datetime.fromisoformat(deferring_since)
            _state["durationSec"] = round((datetime.now(timezone.utc) - started).total_seconds())
        except Exception:
            pass
        _push(force=True)

        db = _client()
        if db is not None:
            try:
                db.collection(f"pipeline_runs{_lane_suffix()}").document(run_id).set(_state)
            except Exception:
                pass
    except Exception:
        pass


def status_note() -> str:
    """One line for the console so a silent no-op is visible in the log."""
    if _client() is not None:
        return f"[status] publishing to pipeline_status{_lane_suffix()}/current"
    return f"[status] disabled ({_disabled_reason})"
