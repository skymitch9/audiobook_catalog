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


def status_note() -> str:
    """One line for the console so a silent no-op is visible in the log."""
    if _client() is not None:
        return f"[status] publishing to pipeline_status{_lane_suffix()}/current"
    return f"[status] disabled ({_disabled_reason})"
