"""
`scripts/publish_audio_manifest.py` — the only route out of this machine.

AUDIO PLAYER PHASE 1 (2026-08-18). ⚠️ Why this file matters more than its size
suggests: `site/audio_manifest.json` is GITIGNORED (it lists 630 GB of the
household's library file by file, and this repo is PUBLIC), so the manifest can
never reach the Worker through git or through the Pages deployment. This script
is the whole path. If it silently stops publishing, every uploaded audiobook is
600 MB sitting in R2 that the player still answers "not streamable yet" for —
billed and unusable, with nothing red anywhere.

⚠️ Stated plainly: nothing here proves an object reached the bucket. That needs
wrangler, a network and a credential. What these pin is the shape, the
idempotence rule that makes the pipeline call cheap, and the two-repo contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import publish_audio_manifest as pub
from scripts import upload_audio_r2 as up

REPO = Path(__file__).resolve().parents[1]


def _row(anchor="b-aud0001", streamable=True):
    return {
        "anchor": anchor,
        "title": "Skyward",
        "bookId": "skyward",
        "size": 402653184,
        "mtime_ns": 1,
        "sha256": None,
        "streamable": streamable,
        "since": "2026-08-18T01:00:00Z",
        "uploaded_at": "2026-08-18T01:00:00Z",
        "last_stream_at": None,
        "last_position_at": None,
    }


# --------------------------------------------------------------------------- #
# The idempotence rule — the reason the 8-hourly call is free
# --------------------------------------------------------------------------- #
def test_digest_ignores_the_generated_timestamp():
    """⚠️ THE BUG THIS PREVENTS: a re-PUT on every pipeline run, forever.

    `record_payload` stamps a fresh `generated` every call, so a digest over the
    serialised DOCUMENT would differ every time and the "unchanged" branch would
    never fire. The digest is over the `files` map alone, which is the only part
    that changes what the Worker answers.

    Mutation that turns this red: hashing `record_payload(files)` instead of
    `files`.
    """
    files = {"A/one.m4b": _row()}
    first = pub.files_digest(files)
    second = pub.files_digest(files)
    assert first == second
    # And the two payloads built either side of it really do differ, so this is
    # not a test that passes because nothing moves.
    p1 = up.record_payload(files)
    p2 = up.record_payload(files)
    assert p1["files"] == p2["files"]


def test_digest_moves_when_a_row_moves():
    """The other half: a real change must publish."""
    base = {"A/one.m4b": _row()}
    assert pub.files_digest(base) != pub.files_digest({"A/one.m4b": _row(streamable=False)})
    assert pub.files_digest(base) != pub.files_digest(
        {"A/one.m4b": _row(), "B/two.m4b": _row(anchor="b-aud0002")}
    )


def test_digest_is_order_independent():
    """Two dicts with the same content publish once, not twice."""
    a = {"A/one.m4b": _row(), "B/two.m4b": _row(anchor="b-aud0002")}
    b = {"B/two.m4b": _row(anchor="b-aud0002"), "A/one.m4b": _row()}
    assert pub.files_digest(a) == pub.files_digest(b)


# --------------------------------------------------------------------------- #
# What may and may not be published
# --------------------------------------------------------------------------- #
def test_an_empty_manifest_is_publishable():
    """🔴 THE DAY-ONE CASE, and it must not be treated as broken.

    Ingest is on demand (owner decision 3), so before anybody presses "request
    it" the correct manifest has zero rows. Refusing to publish it leaves the
    site's audio row on a 503 error path forever; publishing it gets a clean
    200 with zero books and "not streamable yet — request it" on everything.
    """
    assert pub.manifest_problems(up.record_payload({})) == []


def test_a_payload_with_no_files_object_is_refused():
    """A half-written or wrong-shaped document must never overwrite a good one."""
    assert pub.manifest_problems({"count": 3}) != []
    assert pub.manifest_problems({"files": []}) != []


def test_a_streamable_row_with_no_anchor_is_refused():
    """⚠️ It would be invisible forever while the record claimed it was up.

    The Worker's index is keyed on the anchor, so an anchorless row is
    unreachable — the book would be paid for and unplayable, and nothing would
    say so. Loud beats silent.
    """
    row = _row()
    row["anchor"] = ""
    problems = pub.manifest_problems(up.record_payload({"A/one.m4b": row}))
    assert problems and "anchor" in problems[0]


def test_an_evicted_row_with_no_anchor_is_tolerated():
    """Only STREAMABLE rows need to be reachable. An evicted one is history."""
    row = _row(streamable=False)
    row["anchor"] = ""
    assert pub.manifest_problems(up.record_payload({"A/one.m4b": row})) == []


# --------------------------------------------------------------------------- #
# One shape, one place
# --------------------------------------------------------------------------- #
def test_write_record_and_record_payload_are_the_same_document(tmp_path, monkeypatch):
    """⚠️ Two implementations of this shape would be two answers to "what is
    streamable", and the Worker would follow whichever published last.

    `record_payload` was split out of `write_record` for this phase precisely so
    the publisher could build the document without a file on disk. This asserts
    the split did not fork them.
    """
    files = {"A/one.m4b": _row()}
    target = tmp_path / "audio_manifest.json"
    monkeypatch.setattr(up, "RECORD_PATH", target)
    up.write_record(files)
    on_disk = json.loads(target.read_text(encoding="utf-8"))
    in_memory = up.record_payload(files)
    # `generated` is a clock read and legitimately differs between the two calls.
    on_disk.pop("generated"), in_memory.pop("generated")
    assert on_disk == in_memory


# --------------------------------------------------------------------------- #
# The two-repo contract — skips where the sibling checkout is absent (CI)
# --------------------------------------------------------------------------- #
def _worker_src() -> Path:
    return (
        REPO.parents[1]
        / "catalog-platform"
        / "apps"
        / "audiobook-worker"
        / "src"
        / "audio-manifest.ts"
    )


def test_bucket_and_key_match_the_worker():
    """⚠️ ONE CONTRACT, TWO REPOS — and breaking it looks like a stalled pipeline.

    `BUCKET`/`KEY` here must equal the Worker's `EBOOKS_GATED` binding and
    `AUDIO_MANIFEST_KEY`. Change either half alone and every listener gets a 503
    `manifest_absent`, which reads as "the library machine has not run" rather
    than as "somebody renamed an object key".
    """
    src = _worker_src()
    if not src.exists():
        pytest.skip(
            "catalog-platform checkout not available, so the two-repo contract cannot be "
            "compared. Expected in CI and NOT a pass — only the shape tests above ran."
        )
    text = src.read_text(encoding="utf-8")
    assert f"AUDIO_MANIFEST_KEY = '{pub.KEY}'" in text, (
        f"this script publishes key {pub.KEY!r}; the Worker's AUDIO_MANIFEST_KEY no longer "
        "matches. Both halves move together or listeners get manifest_absent."
    )
    assert pub.BUCKET == "ebooks-gated", (
        "the audio manifest rides in the ebook shelf's private bucket under a second key — "
        "owner decision 1 fused the two grants, so it is the ONE gated-manifest bucket for "
        "the ONE book-files grant. Moving it means moving the Worker's EBOOKS_GATED read too."
    )
