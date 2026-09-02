"""
The eviction access timestamps — audio player phase 2 (2026-09-02).

WHAT THIS IS. Design §10.1 names one thing phase 1 could not wire and phase 2
must: `evict_candidates()` refuses to delete anything because
`last_stream_at` is null for every object, and the evictor reads a file on the
library machine that a Cloudflare Worker cannot write to. The seam is
Firestore — the Worker's byte route stamps `audio_streams/{anchor}`, this tool
reads that collection and merges it into the manifest.

⚠️ THIS FILE TESTS THE READ SIDE ONLY, WHICH IS THE HALF THAT LIVES HERE. The
Worker half is in `catalog-platform/apps/audiobook-worker` and this repo does
not touch it — it is a handoff. So what is pinned is the CONTRACT this side
will honour when the other side arrives, plus the two behaviours that decide
whether somebody's half-finished book survives:

  🔴 a stamp that cannot be parsed must be IGNORED, never guessed at — an
     ignored stamp reads as "never listened", which is the same as the correct
     day-one answer and merely delays an eviction. A guessed one deletes a
     book somebody is halfway through.
  🔴 a merge must never move a book's last-listened time BACKWARDS.
"""

from __future__ import annotations

import app.tools.fulfill_audio_requests as fr


def doc(anchor: str, field: dict | None, name: str | None = None) -> dict:
    fields = {"anchor": {"stringValue": anchor}}
    if field is not None:
        fields["lastStreamAt"] = field
    return {"name": name or f"projects/x/databases/(default)/documents/audio_streams/{anchor}",
            "fields": fields}


# ---------------------------------------------------------------------------
# parse_stream_doc — the wire format between two codebases
# ---------------------------------------------------------------------------
def test_epoch_milliseconds_is_the_canonical_form():
    """The contract for the Worker half: `lastStreamAt` in epoch MILLIseconds,
    which is what a JS `Date.now()` produces and what Firestore's REST
    encoding calls an `integerValue`."""
    out = fr.parse_stream_doc(doc("b-abc", {"integerValue": "1788000000000"}))
    assert out is not None
    anchor, stamp = out
    assert anchor == "b-abc"
    assert stamp.endswith("Z") and stamp.startswith("2026-")


def test_every_encoding_firestore_might_use_is_accepted():
    """⚠️ PERMISSIVE ON PURPOSE. A stamp silently dropped because the writer
    used a timestamp rather than a number is indistinguishable from "nobody
    listened" — and that is the reading that lets a book be evicted."""
    forms = [
        {"integerValue": "1788000000000"},
        {"doubleValue": 1788000000000.0},
        {"stringValue": "2026-08-29T12:00:00Z"},
        {"timestampValue": "2026-08-29T12:00:00Z"},
    ]
    for f in forms:
        out = fr.parse_stream_doc(doc("b-abc", f))
        assert out is not None, f"{f} was dropped"
        assert out[1].endswith("Z")


def test_epoch_seconds_are_understood_too():
    """`_parse_stamp` treats anything under 1e11 as seconds. A Worker written
    against `Date.now()/1000` is wrong-by-contract but must not be MISREAD as
    1970, which would evict every book instantly."""
    out = fr.parse_stream_doc(doc("b-abc", {"integerValue": "1788000000"}))
    assert out is not None
    assert out[1].startswith("2026-")


def test_the_document_id_is_the_anchor_when_the_body_omits_it():
    d = {"name": "projects/x/databases/(default)/documents/audio_streams/b-fromid",
         "fields": {"lastStreamAt": {"integerValue": "1788000000000"}}}
    out = fr.parse_stream_doc(d)
    assert out is not None and out[0] == "b-fromid"


def test_an_unusable_document_is_dropped_rather_than_guessed():
    assert fr.parse_stream_doc(doc("b-abc", None)) is None                      # no stamp
    assert fr.parse_stream_doc(doc("b-abc", {"stringValue": "soon"})) is None    # unparseable
    assert fr.parse_stream_doc({"fields": {}}) is None                           # no anchor
    assert fr.parse_stream_doc({}) is None


# ---------------------------------------------------------------------------
# merge_stream_stamps — joined on the ANCHOR, never the path
# ---------------------------------------------------------------------------
def test_the_join_is_on_the_anchor_not_the_key():
    """The record is keyed on the library-relative PATH — which is exactly
    what the Worker does not have and must never be sent. The anchor is the
    shared identity; that is what an anchor is for."""
    files = {
        "Brandon Sanderson/Skyward.m4b": {"anchor": "b-475", "streamable": True},
        "Other/Book.m4b": {"anchor": "b-999", "streamable": True},
    }
    merged = fr.merge_stream_stamps(files, {"b-475": "2026-08-29T12:00:00Z"})
    assert merged == 1
    assert files["Brandon Sanderson/Skyward.m4b"]["last_stream_at"] == "2026-08-29T12:00:00Z"
    assert "last_stream_at" not in files["Other/Book.m4b"]


def test_an_entry_with_no_anchor_is_skipped_without_raising():
    files = {"k": {"streamable": True}}
    assert fr.merge_stream_stamps(files, {"b-475": "2026-08-29T12:00:00Z"}) == 0


# 🔴 THE ONE THAT PROTECTS A HALF-FINISHED BOOK.
def test_a_merge_never_moves_a_stamp_backwards():
    """A Firestore listing that loses a page, or a lane not written recently,
    must not be able to make a recently-played book look older than it is —
    that is the reading that evicts it."""
    files = {"k": {"anchor": "b-475", "streamable": True, "last_stream_at": "2026-08-29T12:00:00Z"}}
    assert fr.merge_stream_stamps(files, {"b-475": "2026-01-01T00:00:00Z"}) == 0
    assert files["k"]["last_stream_at"] == "2026-08-29T12:00:00Z"


def test_a_newer_stamp_does_win():
    files = {"k": {"anchor": "b-475", "streamable": True, "last_stream_at": "2026-08-01T00:00:00Z"}}
    assert fr.merge_stream_stamps(files, {"b-475": "2026-08-29T12:00:00Z"}) == 1
    assert files["k"]["last_stream_at"] == "2026-08-29T12:00:00Z"


# ---------------------------------------------------------------------------
# stream_stamps — both lanes, and a failure that fails SAFE
# ---------------------------------------------------------------------------
def test_both_lanes_are_read_and_the_newest_wins(monkeypatch):
    """⚠️ A book listened to on /dev/ has been listened to. A dev-only stamp
    the evictor could not see would make a book somebody is actually playing
    look abandoned."""
    calls = []

    def fake_fetch(collection):
        calls.append(collection)
        if collection == "audio_streams":
            return [doc("b-475", {"stringValue": "2026-08-01T00:00:00Z"})]
        return [doc("b-475", {"stringValue": "2026-08-29T12:00:00Z"}),
                doc("b-999", {"stringValue": "2026-08-15T00:00:00Z"})]

    monkeypatch.setattr(fr, "fs_fetch", fake_fetch)
    out = fr.stream_stamps()
    assert calls == ["audio_streams", "audio_streams_dev"]
    assert out["b-475"] == "2026-08-29T12:00:00Z"   # the /dev/ one is newer
    assert out["b-999"] == "2026-08-15T00:00:00Z"


def test_a_listing_failure_is_a_warning_and_an_empty_answer(monkeypatch, capsys):
    """🔴 FAILING THIS WAY ROUND IS THE SAFE ONE. An empty answer means no
    access data, which means `evict_candidates()` refuses to delete anything.
    Raising would stop the pipeline step; guessing would delete books."""
    def boom(_collection):
        raise RuntimeError("network")

    monkeypatch.setattr(fr, "fs_fetch", boom)
    assert fr.stream_stamps() == {}
    assert "[WARN]" in capsys.readouterr().out


def test_no_stamps_still_means_no_evictions(monkeypatch):
    """The end-to-end statement of the guard: stamps unavailable ⇒ the pair of
    access fields stays null ⇒ every object is REFUSED, in words."""
    monkeypatch.setattr(fr, "fs_fetch", lambda _c: [])
    files = {"k": {"anchor": "b-475", "streamable": True,
                   "last_stream_at": None, "last_position_at": None}}
    fr.merge_stream_stamps(files, fr.stream_stamps())
    candidates, refusals = fr.evict_candidates(files)
    assert candidates == []
    assert any("no access data yet" in r for r in refusals)


# ---------------------------------------------------------------------------
# The rules clause that lets the evictor read at all
# ---------------------------------------------------------------------------
def test_firestore_rules_carry_both_stream_lanes():
    """⚠️ Rules deploy as ONE document. A missing `_dev` clause falls through
    to the catch-all, and its behaviour is whatever that says — not what the
    prod clause says. Both lanes, or neither is trustworthy."""
    from pathlib import Path

    rules = (Path(__file__).resolve().parents[1] / "firestore.rules").read_text(encoding="utf-8")
    assert "match /audio_streams/{anchor}" in rules
    assert "match /audio_streams_dev/{anchor}" in rules


def test_browsers_may_read_but_never_write_the_stamps():
    """🔴 `allow write: if false` is correct BECAUSE a service account bypasses
    rules: the Worker writes fine, every browser is refused. A forged stamp
    keeps a dead book on the bill for ever; a missing one evicts a book
    somebody is halfway through. And `allow read: if true` is LOAD-BEARING —
    this tool lists the collection with the PUBLIC web API key, exactly as it
    does for audio_requests."""
    from pathlib import Path

    rules = (Path(__file__).resolve().parents[1] / "firestore.rules").read_text(encoding="utf-8")
    for lane in ("audio_streams", "audio_streams_dev"):
        block = rules.split(f"match /{lane}/{{anchor}}", 1)[1].split("}", 1)[0]
        assert "allow read: if true;" in block, f"{lane}: the evictor cannot list it"
        assert "allow write: if false;" in block, f"{lane}: a browser can forge a listen"
