"""Reactive filesystem watcher — fire the pipeline when a book ARRIVES.

Owner ask (docs/TODO.md "⚡ REACTIVE PIPELINE", 2026-08-16): *"should we make
it reactive instead of on a timer so we arent just uploading nothing
sometimes? ... kick off if a new book is detected or a change in folder
structure."* The agreed shape is HYBRID and this module is only the ADDITIVE
half: the 8-hourly Task Scheduler run stays exactly as it is (owner's exact
words: *"lets keep the scheduled runs at the times it is now, so every 8
hours it checks to self heal"*) and is reframed as the self-healing pass.
This watcher's job is latency — a dropped book appears in minutes, not up to
8 hours later. Anything it structurally misses (PC asleep, watcher dead,
Drive-side changes) is exactly what the unchanged scheduled run heals.

Why a SIBLING of app/tools/pipeline_watcher.py rather than an extension of
it: that module polls Firestore for remote "run now" requests — different
input domain, different failure modes (token auth, request replay), and it is
already at the flake8 complexity budget after the step-dispatch additions.
This module copies its idioms instead: one poll per invocation from Task
Scheduler, subprocess-to-sync_to_drive.py runner, the same rate-limited
`_notice` pattern, a tick lock with mtime staleness. State that must survive
between one-shot ticks lives in output_files/fs_watcher_state.json.

The runner deliberately launches ONLY scripts/sync_to_drive.py — not the
auto_acquire step the remote watcher's full run prepends. A reactive fire
means a file already arrived locally; acquisition (Audible container
downloads) is upstream of arrival and stays on the 8h schedule.

⚠️ SETTLE, NOT CREATE — the hazard this module exists to avoid: a 368 MB m4b
fires create/modify events while OpenAudible is still writing it, and
ingesting it truncated is the failure mode. A change only counts as settled
when ALL of:
  * its size+mtime signature has been stable for SETTLE_SECONDS (~60s), AND
  * the file parses as valid media (mutagen opens it; epubs must be a real
    zip). Removals and folder-structure changes need only the quiet period.
A file that stays byte-stable but NEVER validates would otherwise block every
future fire, so after INVALID_GIVEUP_SECONDS it is abandoned loudly: folded
into the baseline and left for the 8h self-heal run to deal with.

COALESCING: eight books arriving together are ONE run. Every new or
still-changing delta resets its own settle clock, and a fire requires EVERY
pending change to be settled — so a burst of arrivals collapses into a
single run after the last one goes quiet.

⚠️ SINGLE-FLIGHT: the fired subprocess goes through the same
app/core/pipeline_lock.py O_CREAT|O_EXCL lock as every other pipeline entry
point — trigger "reactive" is non-scheduled, so it fails LOUDLY if the lock
is held rather than deferring (only the true 8h trigger defers, see
app/core/pipeline_schedule.py). The watcher's own retry model is its pending
state: when a run is already in flight the pending deltas simply persist and
the next tick tries again. Conversely, while OUR reactive run holds the
lock, a scheduled slot that fires defers around it for up to 2h — exactly
the defer-don't-skip machinery, untouched.

FOREIGN-RUN RULE: while any other pipeline run holds the lock the watcher
only observes; when that run completes, the watcher RE-BASELINES instead of
firing — a completed full run already ingested whatever the tree held (its
own sort step is what moved the files, which would otherwise look like fresh
deltas and trigger a pointless run after every scheduled slot). The
vanishingly rare race (a book settling in the final minute of a foreign run)
is precisely what the retained 8h self-heal pass covers.

Accepted consequence (owner-stated): book-only commits auto-promote, so
reactive means PROD PUBLISHES ON ARRIVAL. That is desired; there is no
confirmation step.

⚠️ zzzz_Books_to_be_Converted is a staging pile of part-files awaiting m4b
assembly and is excluded from watching entirely — never trigger on it.

Usage:
    python -m app.tools.fs_watcher           # one tick, then exit
    python -m app.tools.fs_watcher --status  # show config/state, poll nothing
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config import EXTS, OUTPUT_DIR, PROJECT_ROOT, ROOT_DIR
from app.core import pipeline_lock

# Tunables — env-overridable so an operator can tighten/loosen without a
# code change. Defaults are the agreed design numbers.
SETTLE_SECONDS = int(os.getenv("FS_SETTLE_SECONDS", "60"))
INVALID_GIVEUP_SECONDS = int(os.getenv("FS_INVALID_GIVEUP_SECONDS", "1800"))
COOLDOWN_MIN = int(os.getenv("FS_COOLDOWN_MIN", "10"))
MAX_FAIL_STREAK = 3  # consecutive nonzero exits before giving the delta to self-heal

WATCH_ROOT: Path = ROOT_DIR
STATE_PATH: Path = OUTPUT_DIR / "fs_watcher_state.json"
TICK_LOCK_PATH: Path = OUTPUT_DIR / "fs_watcher.lock"
NOTICE_PATH: Path = OUTPUT_DIR / "fs_watcher_notice.txt"
LOG_PATH: Path = OUTPUT_DIR / "pipeline_8h.log"  # the pipeline's canonical log
STALE_TICK_LOCK_HOURS = 6
NOTICE_HOURS = 6

# What counts as "a book": the pipeline's media extensions plus epubs (the
# pipeline's own step 1a renames/sorts root-level epubs). Everything else
# (json sidecars, jpgs, temp files) is noise — but FOLDER structure changes
# are always tracked regardless of contents.
WATCH_EXTS = frozenset(e.lower() for e in EXTS) | {".epub"}
EXCLUDED_DIR_NAMES = frozenset({"zzzz_books_to_be_converted"})


def _now() -> float:
    return time.time()


def _log(msg: str) -> None:
    print(f"[fs-watcher] {datetime.now().strftime('%Y-%m-%d %H:%M')} {msg}", flush=True)


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


# ---------------------------------------------------------------------------
# Tree snapshot / diff
# ---------------------------------------------------------------------------


def _scan() -> Optional[dict]:
    """Snapshot the watched tree: media/epub files with (size, mtime_ns)
    signatures, plus the folder structure. None if the root is missing
    (drive unmounted / misconfigured) — never treat that as 'everything was
    deleted'."""
    root = WATCH_ROOT
    if not root.is_dir():
        return None
    files: dict[str, list] = {}
    dirs: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.casefold() not in EXCLUDED_DIR_NAMES]
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir != ".":
            dirs.append(rel_dir.replace("\\", "/"))
        for name in filenames:
            if os.path.splitext(name)[1].lower() not in WATCH_EXTS:
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError:
                continue  # vanished mid-scan; next tick sees the truth
            files[os.path.relpath(full, root).replace("\\", "/")] = [st.st_size, st.st_mtime_ns]
    return {"files": files, "dirs": sorted(dirs)}


def _diff(baseline: dict, current: dict) -> dict:
    """Changes since baseline. Keys are 'f:<relpath>' or 'd:<relpath>',
    values {'kind': added|changed|removed, 'sig': [size, mtime_ns] | None}."""
    deltas: dict[str, dict] = {}
    bfiles, cfiles = baseline.get("files", {}), current.get("files", {})
    for rel, sig in cfiles.items():
        old = bfiles.get(rel)
        if old is None:
            deltas[f"f:{rel}"] = {"kind": "added", "sig": sig}
        elif old != sig:
            deltas[f"f:{rel}"] = {"kind": "changed", "sig": sig}
    for rel in bfiles:
        if rel not in cfiles:
            deltas[f"f:{rel}"] = {"kind": "removed", "sig": None}
    bdirs, cdirs = set(baseline.get("dirs", [])), set(current.get("dirs", []))
    for d in sorted(cdirs - bdirs):
        deltas[f"d:{d}"] = {"kind": "added", "sig": None}
    for d in sorted(bdirs - cdirs):
        deltas[f"d:{d}"] = {"kind": "removed", "sig": None}
    return deltas


def _update_pending(pending: dict, deltas: dict, now: float) -> dict:
    """Merge this tick's deltas into the pending set. An entry whose kind AND
    signature are unchanged keeps its settle clock running; anything new or
    still-changing gets its clock reset to now. Entries whose delta vanished
    (temp file gone, file restored to baseline state) are dropped."""
    fresh: dict[str, dict] = {}
    for key, d in deltas.items():
        prev = pending.get(key)
        if prev is not None and prev.get("kind") == d["kind"] and prev.get("sig") == d["sig"]:
            fresh[key] = prev
        else:
            fresh[key] = {"kind": d["kind"], "sig": d["sig"], "since": now}
    return fresh


# ---------------------------------------------------------------------------
# Settle / validity
# ---------------------------------------------------------------------------


def _file_valid(path: Path) -> bool:
    """Secondary settle signal: does the file parse as what it claims to be?
    Size+mtime stability is the primary signal; this catches a copy that
    stalled dead rather than finishing. Never raises."""
    try:
        if path.suffix.lower() == ".epub":
            import zipfile

            return zipfile.is_zipfile(str(path))
        import mutagen

        return mutagen.File(str(path)) is not None
    except Exception:
        return False


def _evaluate(pending: dict, now: float) -> tuple[bool, list[str]]:
    """(ready_to_fire, keys_to_abandon). Ready only when EVERY pending entry
    is settled — that is the coalescing rule: one late or still-copying file
    holds the whole batch, so a burst becomes exactly one run."""
    ready = True
    abandoned: list[str] = []
    for key, entry in pending.items():
        age = now - float(entry["since"])
        if age < SETTLE_SECONDS:
            ready = False
            continue
        if key.startswith("f:") and entry["kind"] in ("added", "changed"):
            if not _file_valid(WATCH_ROOT / key[2:]):
                if age >= INVALID_GIVEUP_SECONDS:
                    abandoned.append(key)
                else:
                    ready = False
    return ready, abandoned


def _abandon_invalid(state: dict, abandoned: list[str], current: dict) -> None:
    """A byte-stable file that never parses would block every future fire.
    Fold it into the baseline (so it stops registering as a delta), drop it
    from pending, and say so loudly — the 8h self-heal run owns it now."""
    for key in abandoned:
        state["pending"].pop(key, None)
        rel = key[2:]
        sig = current.get("files", {}).get(rel)
        if sig is None:
            state["baseline"].get("files", {}).pop(rel, None)
        else:
            state["baseline"].setdefault("files", {})[rel] = sig
        # Fold in the file's own newly-created parent folders too — otherwise
        # their pending "folder added" deltas would fire a run right after we
        # said we were standing down on this file.
        parent = os.path.dirname(rel)
        while parent:
            if state["pending"].pop(f"d:{parent}", None) is not None:
                bdirs = set(state["baseline"].get("dirs") or [])
                bdirs.add(parent)
                state["baseline"]["dirs"] = sorted(bdirs)
            parent = os.path.dirname(parent)
        _log(
            f"GIVING UP on {rel!r}: byte-stable for {INVALID_GIVEUP_SECONDS}s but never "
            "parsed as valid media — folded into baseline; the 8h self-heal run owns it."
        )


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

_EMPTY_STATE: dict = {
    "baseline": None,
    "pending": {},
    "last_fire": 0.0,
    "fail_streak": 0,
    "foreign_run": None,
}


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
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


# ---------------------------------------------------------------------------
# Firing
# ---------------------------------------------------------------------------


def _summary(pending: dict) -> str:
    kinds = {"added": 0, "changed": 0, "removed": 0}
    folders = 0
    for key, entry in pending.items():
        if key.startswith("d:"):
            folders += 1
        else:
            kinds[entry["kind"]] = kinds.get(entry["kind"], 0) + 1
    parts = [f"{n} {k}" for k, n in kinds.items() if n]
    if folders:
        parts.append(f"{folders} folder change(s)")
    return ", ".join(parts) or "no changes"


def _cooldown_remaining(state: dict) -> float:
    """Minutes left before another reactive fire is allowed."""
    elapsed_min = (_now() - float(state.get("last_fire") or 0.0)) / 60.0
    return max(0.0, COOLDOWN_MIN - elapsed_min)


def _fire(state: dict, summary: str) -> None:
    """Run the pipeline (sync_to_drive.py only — see module docstring for why
    auto_acquire is not prepended) with PIPELINE_TRIGGER=reactive. The child
    takes the single-flight lock itself; a lost race exits nonzero and the
    pending state simply persists for the next tick."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PIPELINE_TRIGGER="reactive")
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(PROJECT_ROOT / "scripts" / "sync_to_drive.py")]
    with open(LOG_PATH, "a", encoding="utf-8", errors="replace") as log:
        log.write(f"\n================= REACTIVE RUN {datetime.now()} ({summary}) =================\n")
        log.flush()
        _log(f"settled ({summary}) — running: {' '.join(cmd[1:])} (trigger=reactive)")
        rc = subprocess.call(cmd, cwd=str(PROJECT_ROOT), env=env, stdout=log, stderr=log)
        _log(f"  exit={rc}")
    state["last_fire"] = _now()
    if rc == 0:
        state["fail_streak"] = 0
        rescan = _scan()
        if rescan is not None:
            state["baseline"] = rescan  # the run moved files; that is the new truth
        state["pending"] = {}
        return
    streak = int(state.get("fail_streak", 0)) + 1
    state["fail_streak"] = streak
    if streak >= MAX_FAIL_STREAK:
        _log(
            f"reactive run failed {streak}x in a row — re-baselining and standing down "
            "on this delta; the 8h self-heal run owns it. See the pipeline log above."
        )
        rescan = _scan()
        if rescan is not None:
            state["baseline"] = rescan
        state["pending"] = {}
        state["fail_streak"] = 0
    else:
        _log(f"reactive run exited {rc} (attempt {streak}/{MAX_FAIL_STREAK}) — will retry after cooldown")


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------


def _tick_lock_held() -> bool:
    """True if another tick is in flight (a fire takes minutes; ticks fire
    every minute). Clears locks left by a crash — same mtime-staleness
    pattern as app/tools/pipeline_watcher.py."""
    if not TICK_LOCK_PATH.exists():
        return False
    age_h = (time.time() - TICK_LOCK_PATH.stat().st_mtime) / 3600
    if age_h > STALE_TICK_LOCK_HOURS:
        _log(f"clearing stale tick lock ({age_h:.1f}h old)")
        TICK_LOCK_PATH.unlink(missing_ok=True)
        return False
    return True


def _handle_pipeline_lock(state: dict, current: dict) -> bool:
    """Foreign-run rule (module docstring). Returns True when this tick must
    stop here: either a run is in flight (observe, don't evaluate — the run
    is mutating the tree under us), or one just completed (re-baseline; it
    already ingested whatever the tree held)."""
    if pipeline_lock.LOCK_PATH.exists():
        holder = pipeline_lock.current_holder()
        who = holder.describe() if holder else "unreadable lock file"
        if state.get("foreign_run") is None:
            _log(f"pipeline run in flight ({who}) — observing, not evaluating")
        state["foreign_run"] = who
        _save_state(state)
        return True
    if state.get("foreign_run"):
        _log(
            f"run by {state['foreign_run']} completed — re-baselining without firing; "
            "a completed run already ingested the tree (8h self-heal backstops the race)"
        )
        state["foreign_run"] = None
        state["baseline"] = current
        state["pending"] = {}
        state["fail_streak"] = 0
        _save_state(state)
        return True
    return False


def poll_once() -> int:
    """One tick: snapshot, diff, settle-check, maybe fire. Returns 0 in every
    normal case (Task Scheduler runs this constantly; a nonzero exit would
    show the task permanently failed for routine conditions)."""
    if _tick_lock_held():
        return 0
    TICK_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    TICK_LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
    try:
        return _tick()
    finally:
        TICK_LOCK_PATH.unlink(missing_ok=True)


def _tick() -> int:
    current = _scan()
    if current is None:
        _notice(f"idle — watch root {WATCH_ROOT} does not exist (drive unmounted? ROOT_DIR wrong?)")
        return 0

    state = _load_state()
    now = _now()

    if _handle_pipeline_lock(state, current):
        return 0

    if state.get("baseline") is None:
        state["baseline"] = current
        _save_state(state)
        _log(
            f"baseline initialized: {len(current['files'])} files, "
            f"{len(current['dirs'])} folders — not firing on the pre-existing tree"
        )
        return 0

    state["pending"] = _update_pending(state.get("pending") or {}, _diff(state["baseline"], current), now)
    if not state["pending"]:
        _save_state(state)
        return 0

    ready, abandoned = _evaluate(state["pending"], now)
    if abandoned:
        _abandon_invalid(state, abandoned, current)
        ready = bool(state["pending"]) and _evaluate(state["pending"], now)[0]
    if not state["pending"]:
        _save_state(state)
        return 0
    if not ready:
        _save_state(state)
        _log(f"waiting for settle: {_summary(state['pending'])}")
        return 0

    remaining = _cooldown_remaining(state)
    if remaining > 0:
        _save_state(state)
        _log(f"settled ({_summary(state['pending'])}) but cooling down — {remaining:.1f} min left")
        return 0

    _fire(state, _summary(state["pending"]))
    _save_state(state)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_status() -> None:
    state = _load_state()
    baseline = state.get("baseline")
    holder = pipeline_lock.current_holder()
    print(f"  watch root     : {WATCH_ROOT} ({'exists' if WATCH_ROOT.is_dir() else 'MISSING'})")
    print(f"  settle window  : {SETTLE_SECONDS}s (invalid-media giveup {INVALID_GIVEUP_SECONDS}s)")
    print(f"  cooldown       : {COOLDOWN_MIN} min ({_cooldown_remaining(state):.1f} left)")
    if baseline is None:
        print("  baseline       : none yet (first tick will snapshot, not fire)")
    else:
        print(f"  baseline       : {len(baseline.get('files') or {})} files, {len(baseline.get('dirs') or [])} folders")
    print(f"  pending        : {_summary(state.get('pending') or {}) if state.get('pending') else 'nothing'}")
    print(f"  pipeline lock  : {holder.describe() if holder else ('held (unreadable)' if pipeline_lock.LOCK_PATH.exists() else 'free')}")
    print(f"  foreign run    : {state.get('foreign_run') or 'none observed'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Reactive pipeline trigger: watch ROOT_DIR, fire on settled changes")
    parser.add_argument("--status", action="store_true", help="Show configuration/state and exit")
    args = parser.parse_args()
    if args.status:
        _print_status()
        return 0
    return poll_once()


if __name__ == "__main__":
    sys.exit(main())
