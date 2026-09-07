"""B17 (2026-09-07) — a book being OCR'd tonight stops rendering as "deferred".

THE DEFECT
----------
`/status/processing` showed an ARMED needs-OCR PDF in the `deferred-pdf` lane,
labelled *"Deferred PDF (needs OCR) — held back by design"*, while it was in
fact the next ten minutes of CPU work the ingester would do.

The cause is a deliberate design decision working exactly as designed. An
item's `tier` is its IDENTITY and a cross-repo contract; arming a needs-OCR PDF
moves only its `sort_tier`, to 3.5, which promotes it from the back of the
queue to just after the twins (`_ocr_sort_tier`, 2026-09-01 — chosen as a
FRACTION precisely so the tier integers would not have to be renumbered).
`LANE_BY_TIER` maps tier alone, so both armed and unarmed tier-6 items landed
in one lane.

⚠️ SO THE ASSERTION THAT MATTERS IS THAT TWO ITEMS WITH THE SAME `tier` GO TO
DIFFERENT LANES. Anything weaker re-tests `LANE_BY_TIER`.

⚠️ NOT covered here: the reader. `catalog-platform/scripts/lib/
processing-board.mjs` and `status/processing/processing.js` learn the same key,
pinned by `scripts/test/processing-board.test.mjs` in that repo. A lane key
emitted by one side and unknown to the other is the whole failure mode of a
two-repo change.
"""

from __future__ import annotations

from app.core.ingest_queue import (
    OCR_ARMED_SORT_TIER, QueueItem, STATUS_NEEDS_OCR, STATUS_PENDING,
    TIER_EPUB, TIER_NEEDS_OCR, TIER_REST_AUDIO, TIER_REVIEWED_AUDIO,
    TIER_PDF_TEXT, TIER_TWIN, build_queue,
)
from app.core.ingest_queue_summary import (
    ALL_LANES, LANE_BY_TIER, LANE_OCR_ARMED, build_queue_summary, lane_for_item,
)


def _scan(book_id="scan-1", *, armed: bool) -> QueueItem:
    """A tier-6 image-scan PDF, armed or not. `sort_tier` is the ONLY
    difference — which is the point."""
    return QueueItem(
        book_id, "A Scan", TIER_NEEDS_OCR, "pdf-ocr", f"{book_id}.pdf",
        sort_tier=OCR_ARMED_SORT_TIER if armed else None,
    )


# ---------------------------------------------------------------------------
# lane_for_item
# ---------------------------------------------------------------------------

def test_two_items_of_the_SAME_TIER_land_in_different_lanes():
    """⚠️ THE WHOLE ITEM. Armed-ness is not a property of the tier, so no
    tier→lane map could ever have got this right."""
    armed, held = _scan("a", armed=True), _scan("b", armed=False)

    assert armed.tier == held.tier == TIER_NEEDS_OCR
    assert lane_for_item(armed) == LANE_OCR_ARMED == "ocr-pdf"
    assert lane_for_item(held) == "deferred-pdf"
    assert lane_for_item(armed) != lane_for_item(held)


def test_the_tier_integers_are_NOT_renumbered():
    """The contract `_ocr_sort_tier` protected by choosing 3.5 over a tier 7.
    B17 adds a lane KEY, which is additive; renumbering would silently
    re-label every other lane on the sibling repo's board."""
    assert LANE_BY_TIER == {
        TIER_EPUB: "epub",
        TIER_PDF_TEXT: "text-pdf",
        TIER_TWIN: "twin",
        TIER_REVIEWED_AUDIO: "audiobook-with-review",
        TIER_REST_AUDIO: "audiobook",
        TIER_NEEDS_OCR: "deferred-pdf",
    }


def test_every_other_tier_is_unaffected():
    for tier, lane in LANE_BY_TIER.items():
        item = QueueItem(f"b{tier}", "T", tier, "x")
        assert lane_for_item(item) == lane


def test_a_sort_tier_that_is_not_the_armed_value_does_not_claim_the_lane():
    """Only 3.5 means armed. A future `sort_tier` used for something else must
    not silently inherit this lane."""
    odd = QueueItem("o", "Odd", TIER_NEEDS_OCR, "pdf-ocr", sort_tier=5.5)
    assert lane_for_item(odd) == "deferred-pdf"


def test_a_sort_tier_on_a_NON_ocr_tier_does_not_claim_the_lane():
    """3.5 is only meaningful on tier 6. An armed marker on an audiobook is
    not an OCR job."""
    audio = QueueItem("a", "Audio", TIER_REST_AUDIO, "transcript",
                      sort_tier=OCR_ARMED_SORT_TIER)
    assert lane_for_item(audio) == "audiobook"


def test_an_unknown_tier_still_gets_its_own_key():
    """Unchanged behaviour, re-pinned because the branch moved into a new
    function: a new tier must show up as a NEW lane, never deflate an
    existing count."""
    assert lane_for_item(QueueItem("x", "X", 99, "?")) == "tier-99"


def test_an_item_with_no_tier_at_all_is_named_rather_than_dropped():
    class _Bare:
        pass

    assert lane_for_item(_Bare()) == "tier-unknown"


# ---------------------------------------------------------------------------
# the exported summary — what the sibling repo actually reads
# ---------------------------------------------------------------------------

def test_the_summary_splits_armed_from_held():
    summary = build_queue_summary([
        _scan("a", armed=True),
        _scan("b", armed=True),
        _scan("c", armed=False),
    ])
    assert summary["lanes"]["ocr-pdf"] == 2
    assert summary["lanes"]["deferred-pdf"] == 1
    assert summary["total"] == 3


def test_the_new_lane_is_present_and_ZERO_when_nothing_is_armed():
    """⚠️ THE MODULE'S STANDING CONTRACT, and it now has to cover one more key:
    an empty lane is `0`, a lane the exporter forgot is a MISSING KEY, and the
    reader treats missing as "unknown" rather than as zero. A new lane that
    only appears once it is non-empty would read as unknown on every quiet
    night."""
    summary = build_queue_summary([])
    assert set(summary["lanes"]) == set(ALL_LANES)
    assert summary["lanes"]["ocr-pdf"] == 0


def test_the_lane_order_follows_the_QUEUE_order():
    """`ocr-pdf` sits between `twin` and `audiobook-with-review` because 3.5 is
    literally where an armed book runs — a board that lists it beside the
    deferred lane would put the running work under the held heading again, one
    layer up."""
    lanes = list(build_queue_summary([])["lanes"])
    assert lanes.index("twin") < lanes.index("ocr-pdf") < lanes.index("audiobook-with-review")
    assert lanes.index("ocr-pdf") < lanes.index("deferred-pdf")


def test_the_totals_still_reconcile():
    """The reader trusts the split only when `reviewed + rest` equals the GPU
    bucket, and folds every lane back against cpu/gpu. Splitting one lane in
    two must not change either arithmetic."""
    items = [
        _scan("a", armed=True),
        _scan("b", armed=False),
        QueueItem("g", "Audio", TIER_REST_AUDIO, "transcript", needs_gpu=True),
    ]
    summary = build_queue_summary(items)
    assert summary["cpu"] == 2 and summary["gpu"] == 1
    assert sum(summary["lanes"].values()) == summary["total"] == 3


# ---------------------------------------------------------------------------
# end to end through build_queue()
# ---------------------------------------------------------------------------

def test_build_queue_produces_the_armed_lane_for_a_really_armed_book(monkeypatch, tmp_path):
    """⚠️ NOT hand-built items: the `sort_tier` has to come from the real
    `_ocr_sort_tier` reading a real state row, because that is the only thing
    that decides armed-ness in production."""
    import app.core.ingest_queue as iq
    from app.core.review_join import book_id_from_title

    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    # ⚠️ The id build_queue will actually use, derived the way it derives it —
    # not a hand-picked string. A state row keyed on anything else silently
    # reads as "no state row", which is the unarmed answer, so the test would
    # pass its `deferred-pdf` half for the wrong reason.
    bid = book_id_from_title("A Scan")
    monkeypatch.setattr(iq, "load_ebooks", lambda: [
        {"title": "A Scan", "path": str(pdf), "format": "pdf"},
    ])
    monkeypatch.setattr(iq, "load_catalog", lambda: [])
    monkeypatch.setattr(iq, "load_additions_log", lambda: {})

    for status, expected in ((STATUS_PENDING, "ocr-pdf"), (STATUS_NEEDS_OCR, "deferred-pdf")):
        queue = build_queue(
            state={"books": {bid: {"status": status}}},
            pdf_classifier=lambda _p: {"ok": False, "reason": "image-scan"},
        )
        scans = [i for i in queue if i.book_id == bid]
        assert len(scans) == 1, f"expected exactly one scan row for {status}"
        assert scans[0].tier == TIER_NEEDS_OCR
        assert build_queue_summary(queue)["lanes"][expected] == 1
