"""Standalone helper process for tests/test_pipeline_lock.py's real
cross-process exercises. NOT a test module (no test_* name, not collected by
pytest) — it is spawned via subprocess so the lock can be exercised against a
genuinely separate OS process, not a mock.

Usage:
    python _pipeline_lock_holder.py <lock_path> hold <seconds>
        Acquire the lock at <lock_path>, print "HELD <pid>", sleep, release,
        print "RELEASED". Simulates a normal, well-behaved long-running run.

    python _pipeline_lock_holder.py <lock_path> crash
        Acquire the lock, print "HELD <pid>", then exit immediately via
        os._exit() WITHOUT releasing — simulates a crash so the parent test
        can exercise real (not mocked) stale-lock, dead-pid recovery.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import pipeline_lock as pl  # noqa: E402


def main() -> None:
    lock_path = Path(sys.argv[1])
    mode = sys.argv[2]
    pl.LOCK_PATH = lock_path  # point this process at the test's isolated lock file

    lock = pl.acquire("manual")
    print(f"HELD {os.getpid()}", flush=True)

    if mode == "hold":
        seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 6.0
        time.sleep(seconds)
        lock.release()
        print("RELEASED", flush=True)
    elif mode == "crash":
        os._exit(0)  # deliberately skip release() — simulates a crash
    else:
        raise SystemExit(f"unknown mode: {mode}")


if __name__ == "__main__":
    main()
