"""ONE door from "something wants a pipeline run" to a run actually happening.

Extracted 2026-09-05 from ``app/tools/drive_poll.py``, unchanged in behaviour,
because a second reactive trigger now needs the same door:
``app/tools/purchase_audit.py`` (the 15-minute Audible purchase audit) has to
wake the pipeline after it downloads a book, and a private copy of this
function in that module would have been a second answer to the question *"how
does a watcher start a run?"*.

The rule this file exists to keep: **a watcher never starts a pipeline
itself.** It writes the same ``pipeline_requests`` document the /status "run
now" button writes, and ``AudiobookPipelineWatcher`` (every 3 min) consumes it
through its existing, already-hardened path — token check, cooldown, and
``app/core/pipeline_lock.py``'s single-flight lock. Adding a direct start would
be a fourth way to launch a run and a fourth place the lock rules could be got
wrong.

``requestedBy`` carries the SOURCE, so the origin of a run is legible on the
status panel afterwards ("drive-poll: 1 book(s) pulled from Drive", or
"purchase-audit: 2 new purchase(s) downloaded from Audible").

Callers pass their own ``log``/``notice`` functions so the line lands in THEIR
log file with THEIR tag — a shared module that printed ``[drive-poll]`` into
``purchase_audit.log`` would be a small lie in an operator's only instrument.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Callable

Logger = Callable[[str], None]


def request_run(
    reason: str,
    *,
    source: str,
    log: Logger = print,
    notice: Logger | None = None,
) -> bool:
    """Queue a pipeline run request. Returns False (having said why) when the
    trigger token or Firestore is unavailable; the caller treats that as "did
    not act".

    ``notice`` is for the recurring condition (an unconfigured machine) — a
    watcher on a 15-minute cadence must not write 96 identical lines a day, so
    it passes its own rate-limited notice function here. It defaults to
    ``log``.
    """
    from app.pipeline_status import _client, _lane_suffix

    say_once = notice or log

    token = (os.getenv("PIPELINE_TRIGGER_TOKEN") or "").strip()
    db = _client() if token else None
    if not token or db is None:
        why = "PIPELINE_TRIGGER_TOKEN not set in .env" if not token else "no Firestore credentials"
        say_once(f"wanted a pipeline run but CANNOT request one — {why} (see docs/access/FIREBASE.md)")
        return False

    try:
        db.collection(f"pipeline_requests{_lane_suffix()}").add({
            "token": token,
            "requestedAt": datetime.now(timezone.utc).isoformat(),
            "requestedBy": f"{source}: {reason}",
        })
    except Exception as e:
        log(f"could not enqueue a run request: {type(e).__name__}: {e}")
        return False
    log(f"queued a pipeline run — {reason}")
    return True
