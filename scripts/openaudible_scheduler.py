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


OPENAUDIBLE_HOME = Path(os.getenv("OPENAUDIBLE_HOME", "/config/OpenAudible"))
STATUS_PATH = OPENAUDIBLE_HOME / "status.json"
COMMAND_PATH = OPENAUDIBLE_HOME / "commands.json"
INTERVAL_SECONDS = max(60, int(os.getenv("OPENAUDIBLE_SYNC_INTERVAL_SECONDS", "21600")))
INITIAL_DELAY_SECONDS = max(0, int(os.getenv("OPENAUDIBLE_INITIAL_DELAY_SECONDS", "180")))
POLL_SECONDS = 5


def read_status() -> dict | None:
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def queue_quick_sync() -> bool:
    """Atomically queue Sync_Quick when OpenAudible is ready and idle."""
    status = read_status()
    if not status or status.get("quit"):
        return False
    if status.get("queues"):
        print("OpenAudible is busy; delaying Quick Sync.", flush=True)
        return False
    if COMMAND_PATH.exists():
        print("OpenAudible already has a pending command; delaying Quick Sync.", flush=True)
        return False

    temp_path = COMMAND_PATH.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(["Sync_Quick"]), encoding="utf-8")
    temp_path.replace(COMMAND_PATH)
    print("Queued OpenAudible Quick Sync.", flush=True)
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
