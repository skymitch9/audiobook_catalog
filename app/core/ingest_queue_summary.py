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
      "lanes": {"epub": 0, "text-pdf": 0, "twin": 0, "ocr-pdf": 0,
                "audiobook-with-review": 21, "audiobook": 1016,
                "deferred-pdf": 25}
    }

⚠️ THE LANE KEYS ARE THE PAGE'S VOCABULARY, NOT THE TIER NUMBERS. The reader
already maps `source` strings to those labels (`LANE_BY_SOURCE`), so emitting
tier integers would put a second, silently-drifting mapping between two repos.
`text-pdf` and `deferred-pdf` are spelled the page's way here for that reason —
they are `pdf-text` and `pdf-ocr` in this repo's own `source` vocabulary.

⚠️ `ocr-pdf` AND `deferred-pdf` ARE BOTH TIER 6 (B17, 2026-09-07). They are
told apart by `sort_tier`, not by tier: an ARMED needs-OCR PDF is running now
and belongs in `ocr-pdf`; an unarmed one is genuinely held and belongs in
`deferred-pdf`. See `lane_for_item`.

⚠️ IT IS NOT DELETED BETWEEN RUNS, so it can outlive the queue it describes.
That is deliberate — a page showing the last known lane split is better than one
showing nothing — but it means the file is NOT self-validating. The reader
checks `audiobook-with-review + audiobook` against the GPU bucket in the log
before believing the split, and falls back to the whole bucket when they
disagree. Do not remove that check on the strength of this file looking tidy.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Iterable, Optional

from app.core.atomic_json import write_json_atomic
from app.core.ingest_queue import (
    OCR_ARMED_SORT_TIER, TIER_EPUB, TIER_NEEDS_OCR, TIER_PDF_TEXT,
    TIER_REST_AUDIO, TIER_REVIEWED_AUDIO, TIER_TWIN, TRAINING_ROOT,
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

# --------------------------------------------------------------------------
# B17 (2026-09-07) — the lane an ARMED OCR book belongs in
# --------------------------------------------------------------------------
# 🔴 A BOOK BEING OCR'd TONIGHT RENDERED AS "Deferred PDF (needs OCR)".
# `tier` is an item's IDENTITY and stays 6 when a needs-OCR PDF is armed; only
# its `sort_tier` moves to 3.5 (`OCR_ARMED_SORT_TIER`), which is what promotes
# it from the back of the queue to just after the twins. `LANE_BY_TIER` maps
# tier alone, so the page said "held back by design" about the ten minutes of
# CPU work actually running next.
#
# ⚠️ THE TIER INTEGERS ARE NOT RENUMBERED, and that is the same call
# `_ocr_sort_tier` already made when it chose a FRACTION over a tier 7: the
# integers are a cross-repo contract, and moving them would silently re-label
# every other lane on the sibling repo's board. What is added is a lane KEY,
# which is additive — `processing.js` renders an unrecognised lane verbatim
# rather than dropping it, so the two repos can never disagree destructively.
#
# ⚠️ ARMED-NESS IS NOT A PROPERTY OF THE TIER, which is why this cannot be
# another `LANE_BY_TIER` row: two items with `tier == 6` belong in different
# lanes depending on whether a human has armed them. `sort_tier` is the only
# thing that distinguishes them, and it is already on the item.
LANE_OCR_ARMED = "ocr-pdf"

# Every lane key this exporter can emit, in QUEUE ORDER — which is why
# `ocr-pdf` sits between `twin` and `audiobook-with-review`: 3.5 is literally
# where an armed OCR book runs.
ALL_LANES = (
    "epub",
    "text-pdf",
    "twin",
    LANE_OCR_ARMED,
    "audiobook-with-review",
    "audiobook",
    "deferred-pdf",
)


def lane_for_item(item) -> str:
    """The lane key for one queue item — `tier`, except when armed-ness overrides it.

    ⚠️ AN UNRECOGNISED TIER GETS ITS OWN `tier-<n>` KEY rather than being
    dropped or folded into a neighbour. A new tier must show up as a new lane,
    not silently deflate an existing count; the reader treats a key it does not
    know as a lane it has not been taught, and says so.
    """
    tier = getattr(item, "tier", None)
    if tier == TIER_NEEDS_OCR and getattr(item, "sort_tier", None) == OCR_ARMED_SORT_TIER:
        # Armed: a person ran `--requeue-ocr` on it, so it is queued to run
        # NOW (at 3.5, ahead of the reviewed audiobooks) rather than held.
        return LANE_OCR_ARMED
    lane = LANE_BY_TIER.get(tier)
    return lane if lane is not None else f"tier-{tier if tier is not None else 'unknown'}"


def build_queue_summary(queue: Iterable, ingester_version: Optional[int] = None) -> dict:
    """Count a queue by lane. Pure — no clock beyond `generated_at`, no disk.

    ⚠️ EVERY LANE IS PRESENT, INCLUDING THE EMPTY ONES, and that is the point of
    initialising from `ALL_LANES` rather than counting what happens to be
    there. A lane that is genuinely empty must be distinguishable from a lane
    this exporter forgot: the first is `0`, the second is a MISSING KEY, and the
    reader treats a missing key as "unknown", never as zero.

    ⚠️ `ocr-pdf` vs `deferred-pdf` is decided per ITEM, not per tier — see
    `lane_for_item`. Both are `tier == 6`; only an ARMED one is running.

    `cpu`/`gpu` are recomputed here from `needs_gpu` rather than taken from the
    caller, so they are the same arithmetic the ingester's own log line does and
    the reader's cross-check compares like with like.
    """
    items = list(queue)
    lanes = {lane: 0 for lane in ALL_LANES}
    for item in items:
        lane = lane_for_item(item)
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

    ⚠️ Since 2026-09-07 (F3) it goes through the ONE shared writer,
    `app.core.atomic_json.write_json_atomic`, which adds `flush()`+`fsync()`
    (a power cut, not a kill, is what tmp+replace alone does not survive) and
    a unique temp name in place of the fixed `queue_summary.json.tmp`. The
    helper RAISES on failure by design; swallowing it stays here, where the
    "never stop a run" decision belongs.
    """
    try:
        write_json_atomic(path, summary, indent=1, ensure_ascii=False)
    except Exception:
        pass
