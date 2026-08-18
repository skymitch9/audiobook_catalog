"""Export build_queue()'s per-tier counts so the status page can show the lanes.

WHY THIS EXISTS
---------------
`https://heygabi.ai/status/processing` drew the whole audio shelf as ONE lane of
1,039 books, because the only queue depth written down anywhere was the
ingester's own log line:

    queue: 1062 books (25 CPU, 1037 GPU)

Two buckets. The owner's board wants lanes, and the interesting split — which
audiobooks somebody has REVIEWED, tier 4, the ones that transcribe first — lives
inside `build_queue()` and was never persisted. `catalog-platform`'s
`scripts/lib/processing-board.mjs` therefore reported the bucket whole and said
in as many words that the split was not knowable from there. That was the honest
answer, and this module is what changes the fact rather than the wording.

⚠️ RE-DERIVING THE SPLIT IN JAVASCRIPT WAS THE OTHER OPTION AND IT IS THE ONE
THING NOT TO DO. It would mean a second implementation of six tiers, a review
join, a twin skip and an additions-log read, in a second language — and a second
implementation of a decision is how two numbers start disagreeing in public.
`processing-board.mjs` says so about the queue line itself; this file is the
counterpart it asks for. **`build_queue()` stays the one place that decides; this
only counts what it returned.**

THE FILE IT WRITES
------------------
`estate-training-data/queue_summary.json`, beside `ingest_state.json`:

    {
      "generated_at": "2026-08-18T20:00:05Z",
      "ingester_version": 1,
      "total": 1062, "cpu": 25, "gpu": 1037,
      "lanes": {"epub": 0, "text-pdf": 0, "twin": 0,
                "audiobook-with-review": 21, "audiobook": 1016,
                "deferred-pdf": 25}
    }

⚠️ THE LANE KEYS ARE THE PAGE'S VOCABULARY, NOT THE TIER NUMBERS. The reader
already maps `source` strings to those labels (`LANE_BY_SOURCE`), so emitting
tier integers would put a second, silently-drifting mapping between two repos.
`text-pdf` and `deferred-pdf` are spelled the page's way here for that reason —
they are `pdf-text` and `pdf-ocr` in this repo's own `source` vocabulary.

⚠️ IT IS NOT DELETED BETWEEN RUNS, so it can outlive the queue it describes.
That is deliberate — a page showing the last known lane split is better than one
showing nothing — but it means the file is NOT self-validating. The reader
checks `audiobook-with-review + audiobook` against the GPU bucket in the log
before believing the split, and falls back to the whole bucket when they
disagree. Do not remove that check on the strength of this file looking tidy.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Iterable, Optional

from app.core.ingest_queue import (
    TIER_EPUB, TIER_NEEDS_OCR, TIER_PDF_TEXT, TIER_REST_AUDIO,
    TIER_REVIEWED_AUDIO, TIER_TWIN, TRAINING_ROOT,
)

QUEUE_SUMMARY_PATH = TRAINING_ROOT / "queue_summary.json"

# tier -> the lane label /status/processing uses. See the header for why the
# page's spelling wins over this repo's `source` spelling.
LANE_BY_TIER = {
    TIER_EPUB: "epub",
    TIER_PDF_TEXT: "text-pdf",
    TIER_TWIN: "twin",
    TIER_REVIEWED_AUDIO: "audiobook-with-review",
    TIER_REST_AUDIO: "audiobook",
    TIER_NEEDS_OCR: "deferred-pdf",
}


def build_queue_summary(queue: Iterable, ingester_version: Optional[int] = None) -> dict:
    """Count a queue by lane. Pure — no clock beyond `generated_at`, no disk.

    ⚠️ EVERY LANE IS PRESENT, INCLUDING THE EMPTY ONES, and that is the point of
    initialising from `LANE_BY_TIER` rather than counting what happens to be
    there. A lane that is genuinely empty must be distinguishable from a lane
    this exporter forgot: the first is `0`, the second is a MISSING KEY, and the
    reader treats a missing key as "unknown", never as zero.

    `cpu`/`gpu` are recomputed here from `needs_gpu` rather than taken from the
    caller, so they are the same arithmetic the ingester's own log line does and
    the reader's cross-check compares like with like.
    """
    items = list(queue)
    lanes = {lane: 0 for lane in LANE_BY_TIER.values()}
    for item in items:
        lane = LANE_BY_TIER.get(getattr(item, "tier", None))
        # An unrecognised tier is counted under its own key rather than dropped
        # or folded into a neighbour — a new tier must show up as a new lane,
        # not silently deflate an existing count.
        if lane is None:
            lane = f"tier-{getattr(item, 'tier', 'unknown')}"
            lanes.setdefault(lane, 0)
        lanes[lane] += 1

    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total": len(items),
        "cpu": sum(1 for i in items if not getattr(i, "needs_gpu", False)),
        "gpu": sum(1 for i in items if getattr(i, "needs_gpu", False)),
        "lanes": lanes,
    }
    if ingester_version is not None:
        summary["ingester_version"] = ingester_version
    return summary


def write_queue_summary(summary: dict, path: Path = QUEUE_SUMMARY_PATH) -> None:
    """Write the summary atomically. Never raises — see below.

    ⚠️ FAILING TO WRITE THIS MUST NEVER STOP AN INGEST RUN. It is a reporting
    artefact for a status page; the books are the job. A full disk or a locked
    file costs the page its lane split (which degrades to the whole bucket, by
    design) and costs the run nothing.

    tmp-then-rename for the same reason `save_state` does it: the pusher reads
    this file on a 15-minute timer and must never catch it half-written.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass
