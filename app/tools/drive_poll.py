"""Drive-side reactive trigger — pull a Drive-only drop MINUTES after it lands.

Owner ask (2026-08-26): *"rip it down right away — users expect books fast."*

The gap this closes. Three triggers existed and none of them watched Drive:

    AudiobookSyncPipeline   every 8h 00/08/16   STEP 0b pulls from Drive
    AudiobookFsWatcher      every 1 min         reacts to LOCAL arrivals only
    AudiobookPipelineWatcher every 3 min        polls Firestore for "run now"

So a book someone drops straight into a Drive author folder is invisible to
both watchers — the fs watcher only sees this machine's disk, and nobody
presses the remote button for a file they just uploaded. It waits for the next
8-hourly slot: **up to 8 hours, average 4.**

This module is the missing input domain, and it is deliberately the CHEAPEST
honest mechanism rather than the most thorough one:

  * **The Drive Changes API, not a listing.** ``changes.list`` against a
    persisted ``startPageToken`` returns only what has changed since the last
    tick — normally an empty page, one small request. Re-listing ~200 author
    folders every 15 minutes to diff them would be the expensive way to learn
    the same thing, and it is the shape that made ``audit_drive_vs_local``
    slow enough that nobody ran it.
  * **It runs the EXISTING pull, and asks IT what was new.** Our own STEP 4
    uploads also generate change events, so a change is a candidate, never a
    conclusion. ``scripts/drive_pull.py --enforce --json-summary`` is the one
    matcher that knows copy-safe/series-safe/all-format identity
    (``app/core/drive_pull.py``); this module reads ``pulled`` off its
    ``PULL_JSON`` line and only wakes the pipeline when that number is > 0.
    No second matcher, no second definition of "new".
  * **It does not run the pipeline itself.** It enqueues the same
    ``pipeline_requests`` document the /status "run now" button writes, which
    ``AudiobookPipelineWatcher`` already consumes within 3 minutes. That keeps
    ONE path from "something wants a run" to "a run happens", with its
    existing token check, cooldown and lock handling — rather than a fourth
    thing that can start a pipeline.

⚠️ **SINGLE-FLIGHT: defer, never collide.** When any pipeline run holds
``app/core/pipeline_lock.py``, this tick stops before pulling and — the part
that matters — does **NOT** advance the persisted page token. The same changes
are therefore re-seen on the next tick and nothing is lost. Advancing the token
on a deferred tick would silently drop the drop this module exists to catch.
The same rule covers a failed pull.

⚠️ **The token is the dedup.** There is deliberately no cooldown timer: a
change is acted on exactly once because the token moves past it only after it
has been acted on. A cooldown would add a second, weaker answer to the same
question.

**Kill switch:** ``DRIVE_POLL_ENABLED=0`` stands the whole thing down. It also
honours ``DRIVE_PULL_ENABLED=0`` (STEP 0b's switch): if pulling from Drive is
off, watching Drive for things to pull is pointless, and one switch should not
be silently overridden by another.

**First tick baselines and fires nothing** — same rule as
``app/tools/fs_watcher.py``: the pre-existing state of Drive is not news.

Registration is the OWNER's call and is NOT done by this module — the .bat and
the schtasks line are in ``docs/access/README.md``.

Usage:
    python -m app.tools.drive_poll           # one tick, then exit
    python -m app.tools.drive_poll --status  # show config/state, poll nothing
    python -m app.tools.drive_poll --dry-run # detect and report; pull nothing,
                                             # enqueue nothing, advance nothing
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from app.config import OUTPUT_DIR, PROJECT_ROOT
from app.core import pipeline_lock
from app.core.drive_pull import ALL_EXTS, is_copy_name

# ---------------------------------------------------------------------------
# Tunables — env-overridable so an operator can retune without a code change.
# ---------------------------------------------------------------------------


def _flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


# The scheduled cadence AND a self-throttle floor. The Task Scheduler entry
# owns the real cadence (15 min); this stops a mis-registered 1-minute task
# hammering the Changes API, so the documented default is true either way.
POLL_MINUTES = int(os.getenv("DRIVE_POLL_MINUTES", "15"))
PULL_TIMEOUT_S = int(os.getenv("DRIVE_PULL_TIMEOUT_S", "1800"))

STATE_PATH: Path = OUTPUT_DIR / "drive_poll_state.json"
TICK_LOCK_PATH: Path = OUTPUT_DIR / "drive_poll.lock"
NOTICE_PATH: Path = OUTPUT_DIR / "drive_poll_notice.txt"
STALE_TICK_LOCK_HOURS = 6
NOTICE_HOURS = 6

_EMPTY_STATE: dict = {
    "page_token": None,   # None = never baselined; first tick takes one
    "last_poll": 0.0,
    "last_pull_at": None,
    "last_pulled": 0,
}


def _now() -> float:
    return time.time()


def _log(msg: str) -> None:
    print(f"[drive-poll] {datetime.now().strftime('%Y-%m-%d %H:%M')} {msg}", flush=True)


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
        _log("state file unreadable — starting fresh (will re-baseline, not fire)")
        return dict(_EMPTY_STATE)


def _save_state(state: dict) -> None:
    """Atomic, for the same reason every other state writer here is: a crash
    mid-write must leave the PREVIOUS page token intact. A truncated token file
    silently re-baselines, and a re-baseline is how a drop gets skipped."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def _tick_lock_held() -> bool:
    """True if another tick is in flight — a tick that pulls a 400 MB book can
    outlive the poll interval. Same mtime-staleness pattern as fs_watcher."""
    if not TICK_LOCK_PATH.exists():
        return False
    age_h = (time.time() - TICK_LOCK_PATH.stat().st_mtime) / 3600
    if age_h > STALE_TICK_LOCK_HOURS:
        _log(f"clearing stale tick lock ({age_h:.1f}h old)")
        TICK_LOCK_PATH.unlink(missing_ok=True)
        return False
    return True


# ---------------------------------------------------------------------------
# Drive — ONE client, the same one audit_drive_vs_local and drive_pull use
# ---------------------------------------------------------------------------


def drive_service():
    """``scripts/drive_auth.build_drive_service`` — the single OAuth client for
    this repo. ``audit_drive_vs_local`` and ``drive_pull`` reach the same
    function object; there is deliberately no second auth path here."""
    from scripts import drive_auth
    return drive_auth.build_drive_service()


def library_folder_ids(service) -> set[str]:
    """Every Drive folder id that counts as "the library": the parent plus each
    author folder under it.

    Reuses ``sync_to_drive.fetch_all_drive_folders`` rather than re-issuing its
    query, so there is one definition of what the library's folders are. The
    Changes feed is account-wide, and without this filter a change to any
    unrelated Drive file would fire the pipeline.
    """
    from scripts.sync_to_drive import DRIVE_PARENT_FOLDER_ID, fetch_all_drive_folders
    return {DRIVE_PARENT_FOLDER_ID} | set(fetch_all_drive_folders(service).values())


_CHANGE_FIELDS = (
    "nextPageToken,newStartPageToken,"
    "changes(removed,fileId,file(id,name,mimeType,trashed,parents))"
)


def start_page_token(service) -> str:
    return str(service.changes().getStartPageToken().execute()["startPageToken"])


def list_changes(service, token: str) -> tuple[list[dict], str]:
    """Every change since ``token``, plus the token to persist for next time.

    Pages to exhaustion: a machine that was off for a day can have several
    pages waiting, and stopping early would leave the rest unseen forever
    (the next tick starts from the token we saved).
    """
    changes: list[dict] = []
    page = token
    while True:
        resp = service.changes().list(
            pageToken=page, spaces="drive", pageSize=1000, fields=_CHANGE_FIELDS,
        ).execute()
        changes.extend(resp.get("changes") or [])
        nxt = resp.get("nextPageToken")
        if not nxt:
            return changes, str(resp.get("newStartPageToken") or page)
        page = nxt


_FOLDER_MIME = "application/vnd.google-apps.folder"


def new_book_files(changes: list[dict], folder_ids: set[str]) -> list[str]:
    """Filenames from ``changes`` that are CANDIDATE new books in the library.

    Pure and side-effect free, so the whole decision is testable without Drive.

    ⚠️ "Candidate", not "new". Our own STEP 4 uploads raise change events too,
    so this deliberately does not try to decide whether a file is genuinely
    missing locally — ``scripts/drive_pull.py`` owns that question and is the
    only place the copy-safe/series-safe/all-format matcher lives. A ``Copy
    of …``/``(N)`` name is dropped here only because ``plan_pull`` would refuse
    it anyway and there is no point waking anything for one.
    """
    out: list[str] = []
    for ch in changes:
        if ch.get("removed"):
            continue
        f = ch.get("file") or {}
        if not f or f.get("trashed") or f.get("mimeType") == _FOLDER_MIME:
            continue
        name = f.get("name") or ""
        if Path(name).suffix.lower() not in ALL_EXTS:
            continue
        if is_copy_name(name):
            continue
        if not (set(f.get("parents") or ()) & folder_ids):
            continue
        out.append(name)
    return out


# ---------------------------------------------------------------------------
# Acting on a change
# ---------------------------------------------------------------------------


def run_drive_pull() -> tuple[int, int]:
    """Run ``scripts/drive_pull.py --enforce --json-summary``.

    Returns ``(returncode, pulled)``. Subprocess for the same reason STEP 0b
    uses one: a hard timeout around a client that can otherwise block on an
    interactive OAuth prompt. ``pulled`` comes off the ``PULL_JSON`` line —
    the pull is the authority on what was genuinely new, and 0 means every
    change we saw was already local (usually our own upload coming back).
    """
    cmd = [
        sys.executable, str(PROJECT_ROOT / "scripts" / "drive_pull.py"),
        "--enforce", "--json-summary",
    ]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.run(
            cmd, cwd=str(PROJECT_ROOT), env=env, capture_output=True,
            text=True, timeout=PULL_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        _log(f"drive_pull.py exceeded {PULL_TIMEOUT_S}s — treating as failed, token NOT advanced")
        return 1, 0

    pulled = 0
    for line in (proc.stdout or "").splitlines():
        if line.startswith("PULL_JSON "):
            try:
                pulled = int(json.loads(line[len("PULL_JSON "):]).get("pulled", 0))
            except Exception:
                _log("PULL_JSON line was unparseable — treating as 0 pulled")
    if proc.returncode != 0:
        _log(f"drive_pull.py exited {proc.returncode}: {(proc.stderr or '').strip()[:300]}")
    return proc.returncode, pulled


def enqueue_run_now(reason: str) -> bool:
    """Write the same ``pipeline_requests`` doc the /status "run now" button
    writes, so ``AudiobookPipelineWatcher`` starts the pipeline within 3
    minutes through its existing, already-hardened path.

    ⚠️ Not a fourth way to start a pipeline — the SAME way, with a named
    ``requestedBy`` so the run's origin is legible on the panel afterwards.
    Returns False (having said why) when Firestore or the token is
    unavailable; the caller treats that as "did not act".
    """
    from app.pipeline_status import _client, _lane_suffix

    token = (os.getenv("PIPELINE_TRIGGER_TOKEN") or "").strip()
    db = _client() if token else None
    if not token or db is None:
        why = "PIPELINE_TRIGGER_TOKEN not set in .env" if not token else "no Firestore credentials"
        _notice(f"pulled books but CANNOT request a run — {why} (see docs/access/FIREBASE.md)")
        return False

    try:
        db.collection(f"pipeline_requests{_lane_suffix()}").add({
            "token": token,
            "requestedAt": datetime.now(timezone.utc).isoformat(),
            "requestedBy": f"drive-poll: {reason}",
        })
    except Exception as e:
        _log(f"could not enqueue a run request: {type(e).__name__}: {e}")
        return False
    _log(f"queued a pipeline run — {reason}")
    return True


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------


def poll_once(dry_run: bool = False) -> int:
    """One tick. Returns 0 in every normal case — Task Scheduler runs this ~96
    times a day and a nonzero exit for a routine condition would show the task
    permanently failed."""
    if _tick_lock_held():
        return 0
    TICK_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    TICK_LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
    try:
        return _tick(dry_run=dry_run)
    finally:
        TICK_LOCK_PATH.unlink(missing_ok=True)


def _tick(dry_run: bool = False) -> int:
    if not _flag("DRIVE_POLL_ENABLED"):
        _notice("idle — DRIVE_POLL_ENABLED=0 (kill switch)")
        return 0
    if not _flag("DRIVE_PULL_ENABLED"):
        # STEP 0b's switch. Watching Drive for things to pull while pulling is
        # off would fire a pull that does nothing, every 15 minutes.
        _notice("idle — DRIVE_PULL_ENABLED=0, so there is nothing to poll FOR")
        return 0

    state = _load_state()

    elapsed_min = (_now() - float(state.get("last_poll") or 0.0)) / 60.0
    if elapsed_min < POLL_MINUTES:
        return 0

    service = drive_service()
    if not service:
        _notice("idle — Drive auth unavailable (run scripts/sync_to_drive.py "
                "interactively once to complete OAuth)")
        return 0

    # First ever tick: take a token and fire nothing. The pre-existing state of
    # Drive is not news, and the 8h STEP 0b pull is what covers it.
    if not state.get("page_token"):
        state["page_token"] = start_page_token(service)
        state["last_poll"] = _now()
        _save_state(state)
        _log("baselined on the current Drive change token — not firing on pre-existing files")
        return 0

    changes, next_token = list_changes(service, str(state["page_token"]))
    state["last_poll"] = _now()

    if not changes:
        state["page_token"] = next_token
        _save_state(state)
        return 0

    candidates = new_book_files(changes, library_folder_ids(service))
    if not candidates:
        state["page_token"] = next_token
        _save_state(state)
        _log(f"{len(changes)} Drive change(s), none a new book file in the library — nothing to do")
        return 0

    sample = ", ".join(sorted(set(candidates))[:3])
    more = f" (+{len(set(candidates)) - 3} more)" if len(set(candidates)) > 3 else ""
    _log(f"{len(set(candidates))} candidate book file(s) changed on Drive: {sample}{more}")

    if dry_run:
        _log("DRY-RUN — not pulling, not queuing a run, and NOT advancing the page token")
        return 0

    # ⚠️ DEFER, DON'T COLLIDE — and do not advance the token. A run in flight is
    # already mutating the library and may be mid-upload; the same changes are
    # re-seen next tick, so deferring costs latency and nothing else.
    if pipeline_lock.LOCK_PATH.exists():
        holder = pipeline_lock.current_holder()
        who = holder.describe() if holder else "unreadable lock file"
        _log(
            f"DEFERRED: a pipeline run is in flight ({who}). {len(set(candidates))} "
            "Drive change(s) left unconsumed — the page token was NOT advanced, so "
            "the next tick sees them again."
        )
        _save_state(state)
        return 0

    rc, pulled = run_drive_pull()
    if rc != 0:
        _log("pull FAILED — page token NOT advanced, so the next tick retries these changes")
        _save_state(state)
        return 0

    state["last_pull_at"] = datetime.now(timezone.utc).isoformat()
    state["last_pulled"] = pulled

    if pulled > 0:
        enqueue_run_now(f"{pulled} book(s) pulled from Drive ({sample}{more})")
    else:
        _log("pulled 0 — every changed file was already local (our own upload, or a "
             "copy the matcher refuses). No run queued.")

    # Only now is it safe to move past these changes: they have been acted on.
    state["page_token"] = next_token
    _save_state(state)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_status() -> None:
    state = _load_state()
    holder = pipeline_lock.current_holder()
    last_poll = float(state.get("last_poll") or 0.0)
    when = f"{(_now() - last_poll) / 60.0:.1f} min ago" if last_poll else "never"
    print(f"  enabled        : {'yes' if _flag('DRIVE_POLL_ENABLED') else 'NO (DRIVE_POLL_ENABLED=0)'}")
    print(f"  drive pull     : {'on' if _flag('DRIVE_PULL_ENABLED') else 'OFF (DRIVE_PULL_ENABLED=0)'}")
    print(f"  cadence        : every {POLL_MINUTES} min (last tick {when})")
    print(f"  page token     : {'set' if state.get('page_token') else 'none yet (first tick baselines)'}")
    print(f"  last pull      : {state.get('last_pull_at') or 'never'} ({state.get('last_pulled', 0)} pulled)")
    print(f"  trigger token  : {'set' if (os.getenv('PIPELINE_TRIGGER_TOKEN') or '').strip() else 'NOT SET'}")
    print(f"  pipeline lock  : {holder.describe() if holder else ('held (unreadable)' if pipeline_lock.LOCK_PATH.exists() else 'free')}")
    print(f"  state file     : {STATE_PATH}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Poll the Drive Changes API and pull a Drive-only book drop right away",
    )
    parser.add_argument("--status", action="store_true", help="Show configuration/state and exit")
    parser.add_argument(
        "--dry-run", action="store_true",
        help=("Detect and report only: no pull, no run request, and the page token "
              "is NOT advanced, so a real tick still sees the same changes."),
    )
    args = parser.parse_args()
    if args.status:
        _print_status()
        return 0
    return poll_once(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
