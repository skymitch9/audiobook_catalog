"""Periodically ask a running OpenAudible instance to perform a Quick Sync.

OpenAudible 4.8.x publishes a status.json file whose command_example documents
the commands.json queue. The app consumes and deletes that command file. This
controller deliberately queues only Sync_Quick; automatic download and convert
are user-controlled preferences inside OpenAudible.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    """Read an int env var, tolerating blank/invalid values."""
    raw = os.getenv(name, "")
    try:
        return int(raw)
    except ValueError:
        if raw:
            print(f"Ignoring invalid {name}={raw!r}; using {default}.", flush=True)
        return default


OPENAUDIBLE_HOME = Path(os.getenv("OPENAUDIBLE_HOME", "/config/OpenAudible"))
STATUS_PATH = OPENAUDIBLE_HOME / "status.json"
COMMAND_PATH = OPENAUDIBLE_HOME / "commands.json"
INTERVAL_SECONDS = max(60, _env_int("OPENAUDIBLE_SYNC_INTERVAL_SECONDS", 21600))
INITIAL_DELAY_SECONDS = max(0, _env_int("OPENAUDIBLE_INITIAL_DELAY_SECONDS", 180))
# A queued command OpenAudible hasn't consumed after this long is considered
# abandoned (app crashed/restarted mid-queue) and is replaced.
STALE_COMMAND_SECONDS = max(60, _env_int("OPENAUDIBLE_STALE_COMMAND_SECONDS", 900))
POLL_SECONDS = 30

_last_wait_reason: str | None = None


def _log_wait(reason: str) -> None:
    """Log a wait reason once per busy period instead of every poll."""
    global _last_wait_reason
    if reason != _last_wait_reason:
        print(reason, flush=True)
        _last_wait_reason = reason


def read_status() -> dict | None:
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def queue_quick_sync() -> bool:
    """Atomically queue Sync_Quick when OpenAudible is ready and idle."""
    global _last_wait_reason
    status = read_status()
    if not status or status.get("quit"):
        _log_wait("OpenAudible status unavailable; waiting.")
        return False
    if status.get("queues"):
        _log_wait("OpenAudible is busy; delaying Quick Sync.")
        return False
    if COMMAND_PATH.exists():
        try:
            pending_age = time.time() - COMMAND_PATH.stat().st_mtime
        except OSError:
            pending_age = 0  # consumed between exists() and stat(); treat as fresh
        if pending_age < STALE_COMMAND_SECONDS:
            _log_wait("OpenAudible already has a pending command; delaying Quick Sync.")
            return False
        print(
            f"Pending command is {int(pending_age)}s old (> {STALE_COMMAND_SECONDS}s); "
            "replacing stale command.",
            flush=True,
        )

    temp_path = COMMAND_PATH.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(["Sync_Quick"]), encoding="utf-8")
    temp_path.replace(COMMAND_PATH)
    print("Queued OpenAudible Quick Sync.", flush=True)
    _last_wait_reason = None
    return True


def run_forever() -> None:
    OPENAUDIBLE_HOME.mkdir(parents=True, exist_ok=True)
    if INITIAL_DELAY_SECONDS:
        print(f"Waiting {INITIAL_DELAY_SECONDS}s for OpenAudible startup.", flush=True)
        time.sleep(INITIAL_DELAY_SECONDS)

    while True:
        if queue_quick_sync():
            time.sleep(INTERVAL_SECONDS)
        else:
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run_forever()
