"""The MID-BOOK SHIELD — audio player phase 3 (2026-09-02).

WHAT THIS IS. `evict_candidates()` refuses to delete an R2 object unless it
carries positive evidence of being idle, and that evidence is a PAIR:
`last_stream_at` (phase 2, stamped by the Worker's byte route) and
`last_position_at` — this. The second is the 🔴 MID-BOOK SHIELD, and it is the
entire reason the idle threshold is 30 days rather than the owner's 7: a
30-hour book over a month of commutes is the normal case, not an abandoned
one. The module docstring of `fulfill_audio_requests` says so in those words,
and it said "do not run --evict --commit until phase 3" for this reason.

⚠️ WHERE THE FACT COMES FROM, AND WHY NOT `readingPositions`. The real
positions live there and it is `allow list: if false` in both lanes,
deliberately: "enumerating what a household reads is not a query any client
needs". The evictor lists collections with the PUBLIC web API key, so it is
gated exactly like a browser. `audio_positions/{anchor}` carries the one fact
eviction needs — an opaque anchor and a timestamp — and nothing else. Full
reasoning: site/audio-position.js §4, and the rules block itself.

⚠️ THE UNITS ARE THE TRAP AND THEY ARE THE SAME TRAP AS PHASE 2'S.
`_parse_stamp` reads anything under 1e11 as SECONDS. A stamp written in
seconds therefore decodes to a date in 1970 — older than every cutoff — so a
wrong unit does not fail loudly, it says "evict this book" about a book
somebody is listening to right now.
"""

from __future__ import annotations

import time
from pathlib import Path

RULES = (Path(__file__).resolve().parents[1] / "firestore.rules").read_text(encoding="utf-8")


def _block(lane: str) -> str:
    """The body of one `match /<lane>/{anchor}` clause.

    ⚠️ The opening brace is part of the needle on purpose: the comment above
    the clause CITES `match /audio_positions/{anchor}` by name, and splitting
    on the bare path lands inside prose that satisfies nothing.
    """
    return RULES.split(f"match /{lane}/{{anchor}} {{", 1)[1].split("\n    }", 1)[0]


# ---------------------------------------------------------------------------
# The rules clause — both lanes, or neither is trustworthy
# ---------------------------------------------------------------------------
def test_both_position_lanes_exist():
    """⚠️ Rules deploy as ONE document, and an absent clause is a DENY, not a
    fall-through to something reasonable. It matters more here than for the
    stream stamps: the player has only ever run on the /dev/ lane, so today
    EVERY position anybody saves lands in `audio_positions_dev`."""
    assert "match /audio_positions/{anchor}" in RULES
    assert "match /audio_positions_dev/{anchor}" in RULES


def test_the_evictor_can_still_list_them():
    """🔴 `allow read: if true` is LOAD-BEARING. The evictor lists this with
    the PUBLIC web API key, exactly as it does audio_streams and
    audio_requests. A refused list answers "nobody has ever listened", which
    is ALSO the correct day-one answer — so the failure would be invisible
    until a book somebody was mid-way through disappeared."""
    for lane in ("audio_positions", "audio_positions_dev"):
        assert "allow read: if true;" in _block(lane), f"{lane}: the evictor cannot list it"


def test_a_browser_may_write_this_one_unlike_the_stream_stamps():
    """The deliberate difference from /audio_streams, whose `allow write: if
    false` is correct because a service account bypasses rules and "nobody but
    the Worker has anything true to say here". A saved position is the
    opposite: only the listener's browser holds it."""
    for lane in ("audio_positions", "audio_positions_dev"):
        block = _block(lane)
        assert "allow create: if validAudioPositionStamp(anchor);" in block
        assert "allow write: if false" not in block


def test_a_stamp_can_never_be_dragged_backwards():
    """🔴 THE ONE THAT PROTECTS A HALF-FINISHED BOOK. A forged stamp merely
    keeps a cached object on the bill; a stamp moved BACKWARDS is what makes a
    book look idle enough to evict."""
    for lane in ("audio_positions", "audio_positions_dev"):
        assert ("request.resource.data.lastPositionAt >= resource.data.lastPositionAt"
                in _block(lane)), f"{lane}: a stamp could be moved backwards"


def test_deleting_a_stamp_is_refused_because_that_removes_the_shield():
    for lane in ("audio_positions", "audio_positions_dev"):
        assert "allow delete: if false;" in _block(lane)


def test_the_stamp_is_one_fact_and_carries_nothing_else():
    """`hasOnly` — anything else written here would be an unverifiable claim
    riding on a world-readable document."""
    fn = RULES.split("function validAudioPositionStamp(anchor)", 1)[1].split("\n    }", 1)[0]
    assert "hasOnly(['anchor', 'lastPositionAt'])" in fn
    assert "request.auth != null" in fn
    assert "request.resource.data.anchor == anchor" in fn
    assert "request.resource.data.lastPositionAt is number" in fn


def test_the_clock_window_is_generous_in_the_safe_direction():
    """⚠️ reading-position.js writes a CLIENT-CLOCK epoch on purpose and
    accepts skew. A tight window would refuse the stamps of anyone whose clock
    is wrong, and a refused stamp fails in the HARMFUL direction — it is the
    ABSENCE of a stamp that evicts. The window exists only to stop a year-3000
    value pinning an object on the bill for ever."""
    fn = RULES.split("function validAudioPositionStamp(anchor)", 1)[1].split("\n    }", 1)[0]
    assert "request.time.toMillis() + 604800000" in fn      # 7 days forward
    assert "request.time.toMillis() - 2592000000" in fn     # 30 days back


def test_the_anchor_shape_accepts_the_one_anchor_that_actually_exists():
    """⚠️ MEASURED, not assumed. `b-4754c8e4548e` is Skyward's anchor — the
    only book in the bucket (884 MB, since 2026-08-23) and therefore the only
    thing anybody can currently play. A shape rule that refused it would
    disarm the shield for the entire streaming set, silently."""
    import re

    fn = RULES.split("function validAudioPositionStamp(anchor)", 1)[1].split("\n    }", 1)[0]
    pattern = re.search(r"anchor\.matches\('([^']+)'\)", fn).group(1)
    assert re.match(pattern, "b-4754c8e4548e")
    assert not re.match(pattern, "not-an-anchor")


# ---------------------------------------------------------------------------
# The reading-position clause phase 3 writes through
# ---------------------------------------------------------------------------
def test_reading_positions_accept_the_audio_format_and_kind():
    """⚠️ "In the file" is not "in the project" — these still need deploying,
    and a position written against rules that refuse it fails silently and
    looks exactly like "the player does not save your spot" (design §1.4).
    `scripts/smoke_reading_position_rules.py` is the instrument that answers
    the deployed question; this only pins the text."""
    fn = RULES.split("function validReadingPosition()", 1)[1].split("\n    }", 1)[0]
    assert "'audio'" in fn.split("format in", 1)[1].split("\n", 1)[0]
    assert "'audio'" in fn.split("pos.kind in", 1)[1].split("\n", 1)[0]


def test_reading_positions_are_still_unlistable_in_both_lanes():
    """🔴 THE REASON `audio_positions` EXISTS AT ALL. If this ever becomes
    listable, the shield could read the real store — but that would be an
    access-INCREASING change to the only genuinely per-person collection in
    the project, and it is not one to make as a side effect."""
    for lane in ("readingPositions", "readingPositions_dev"):
        block = RULES.split(f"match /{lane}/{{docId}}", 1)[1].split("\n    }", 1)[0]
        assert "allow list: if false;" in block


# ---------------------------------------------------------------------------
# The smoke script — the only instrument that answers the DEPLOYED question
# ---------------------------------------------------------------------------
def test_a_smoke_script_exists_for_the_deployed_rules():
    """A unit test proves the text of a rules file. Rules are enforced by
    Google and are project-wide the moment they deploy; only a live smoke
    proves the behaviour. This asserts the instrument exists so a future
    session finds it instead of writing a second one."""
    smoke = Path(__file__).resolve().parents[1] / "scripts" / "smoke_audio_position_rules.py"
    assert smoke.exists()
    text = smoke.read_text(encoding="utf-8")
    assert "audio_positions_dev" in text
    # ⚠️ It must clean up after itself, and `delete` is refused to every
    # browser on purpose — so only the service account can, which is also the
    # premise /audio_streams' `allow write: if false` rests on.
    assert "service_account_token" in text


# ---------------------------------------------------------------------------
# The READ side — app/tools/fulfill_audio_requests.py
# ---------------------------------------------------------------------------
def _doc(anchor: str, field: dict | None, collection: str = "audio_positions") -> dict:
    fields = {"anchor": {"stringValue": anchor}}
    if field is not None:
        fields["lastPositionAt"] = field
    return {"name": f"projects/x/databases/(default)/documents/{collection}/{anchor}",
            "fields": fields}


def test_epoch_milliseconds_is_the_canonical_form():
    """🔴 THE UNIT, AND IT IS ASYMMETRIC. Seconds decode to 1970, which is
    older than every cutoff — so the wrong unit does not fail, it says "evict
    this book" about a book being listened to right now."""
    import app.tools.fulfill_audio_requests as fr

    out = fr.parse_position_doc(_doc("b-abc", {"integerValue": "1788000000000"}))
    assert out is not None
    anchor, stamp = out
    assert anchor == "b-abc"
    assert stamp.startswith("2026-") and stamp.endswith("Z")


def test_every_encoding_firestore_might_use_is_accepted():
    """⚠️ PERMISSIVE ON PURPOSE, exactly as the stream stamps are. A stamp
    dropped because the writer used a float is indistinguishable from "nobody
    saved a spot" — and that is the reading that removes the shield."""
    import app.tools.fulfill_audio_requests as fr

    for f in ({"integerValue": "1788000000000"},
              {"doubleValue": 1788000000000.0},
              {"stringValue": "2026-08-29T12:00:00Z"},
              {"timestampValue": "2026-08-29T12:00:00Z"}):
        out = fr.parse_position_doc(_doc("b-abc", f))
        assert out is not None, f"{f} was dropped"


def test_an_unusable_position_document_is_dropped_rather_than_guessed():
    import app.tools.fulfill_audio_requests as fr

    assert fr.parse_position_doc(_doc("b-abc", None)) is None
    assert fr.parse_position_doc(_doc("b-abc", {"stringValue": "later"})) is None
    assert fr.parse_position_doc({}) is None


def test_the_two_parsers_read_different_fields_and_do_not_cross_wires():
    """⚠️ One parser, two field names. A position document must not be read as
    a stream stamp or vice versa: they answer different questions and the
    evictor's whole guard is that BOTH are absent before it refuses."""
    import app.tools.fulfill_audio_requests as fr

    pos = _doc("b-abc", {"integerValue": "1788000000000"})
    assert fr.parse_position_doc(pos) is not None
    assert fr.parse_stream_doc(pos) is None


def test_the_shield_merges_onto_last_position_at_joined_on_the_anchor():
    """The record is keyed on the library-relative PATH — which is exactly
    what a browser does not have and must never be sent. The anchor is the
    shared identity."""
    import app.tools.fulfill_audio_requests as fr

    files = {"Brandon Sanderson/Skyward.m4b": {"anchor": "b-4754c8e4548e", "streamable": True},
             "Other/Book.m4b": {"anchor": "b-999", "streamable": True}}
    assert fr.merge_position_stamps(files, {"b-4754c8e4548e": "2026-08-29T12:00:00Z"}) == 1
    assert files["Brandon Sanderson/Skyward.m4b"]["last_position_at"] == "2026-08-29T12:00:00Z"
    assert "last_position_at" not in files["Other/Book.m4b"]


def test_a_position_merge_never_moves_a_stamp_backwards():
    """🔴 THE ONE THAT PROTECTS A HALF-FINISHED BOOK, on this side too."""
    import app.tools.fulfill_audio_requests as fr

    files = {"k": {"anchor": "b-1", "streamable": True,
                   "last_position_at": "2026-08-29T12:00:00Z"}}
    assert fr.merge_position_stamps(files, {"b-1": "2026-01-01T00:00:00Z"}) == 0
    assert files["k"]["last_position_at"] == "2026-08-29T12:00:00Z"


def test_both_position_lanes_are_read_and_the_newest_wins(monkeypatch):
    """⚠️ A spot saved on /dev/ is a saved spot. It matters more here than for
    the streams: the player has only ever run on the dev lane."""
    import app.tools.fulfill_audio_requests as fr

    calls = []

    def fake_fetch(collection):
        calls.append(collection)
        if collection == "audio_positions":
            return [_doc("b-1", {"stringValue": "2026-08-01T00:00:00Z"})]
        return [_doc("b-1", {"stringValue": "2026-08-29T12:00:00Z"}, "audio_positions_dev")]

    monkeypatch.setattr(fr, "fs_fetch", fake_fetch)
    out = fr.position_stamps()
    assert calls == ["audio_positions", "audio_positions_dev"]
    assert out["b-1"] == "2026-08-29T12:00:00Z"


def test_a_listing_failure_is_told_apart_from_an_empty_listing(monkeypatch, capsys):
    """🔴 THE SILENT-STALENESS TRAP, named. "Could not list it" and "listed it
    and found nothing" produce the SAME empty dict and mean completely
    different things — the first is what a missing rules deploy looks like
    from here, and it is the one that must never be read as "nobody has saved
    a spot"."""
    import app.tools.fulfill_audio_requests as fr

    monkeypatch.setattr(fr, "fs_fetch", lambda _c: [])
    stamps, unreadable = fr._collect_stamps(fr.POSITION_COLLECTIONS, "lastPositionAt")
    assert stamps == {} and unreadable == []

    def boom(_c):
        raise RuntimeError("HTTP Error 403: Forbidden")

    monkeypatch.setattr(fr, "fs_fetch", boom)
    stamps, unreadable = fr._collect_stamps(fr.POSITION_COLLECTIONS, "lastPositionAt")
    assert stamps == {}
    assert unreadable == list(fr.POSITION_COLLECTIONS)
    assert "[WARN]" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 🔴 The refusal — the reason --evict --commit was unsafe for three phases
# ---------------------------------------------------------------------------
def _idle_record(monkeypatch, fr, positions):
    """One streamable object, streamed 90 days ago, with `positions` available."""
    record = {"Brandon Sanderson/Skyward.m4b": {
        "anchor": "b-4754c8e4548e", "streamable": True, "size": 884_000_000,
        "since": "2026-08-23T00:00:00Z", "last_stream_at": "2026-05-01T00:00:00Z"}}
    monkeypatch.setattr(fr.up, "load_record", lambda: dict(record))
    monkeypatch.setattr(fr.up, "write_record", lambda _r: None)
    monkeypatch.setattr(fr, "fs_fetch", lambda c: positions if "position" in c else [])
    return record


def test_commit_refuses_to_delete_while_the_shield_has_no_data(monkeypatch, capsys):
    """The whole point. A book with a stream stamp and NO position data goes
    idle 30 days after the last listen — precisely the paused-30-hour-book
    case the shield exists for. Refusing costs storage; not refusing costs
    somebody their place in a book."""
    import app.tools.fulfill_audio_requests as fr

    _idle_record(monkeypatch, fr, [])
    deleted = []
    monkeypatch.setattr(fr.up, "s3_client", lambda: _explode(deleted))
    stats = fr.run_eviction(commit=True)
    out = capsys.readouterr().out
    assert stats.get("refused_no_shield") is True
    assert stats["candidates"] == 1
    assert "REFUSED" in out and "MID-BOOK SHIELD" in out
    assert deleted == [], "an object was deleted with no shield data"


def test_the_refusal_says_WHICH_of_the_two_causes_it_is(monkeypatch, capsys):
    """⚠️ "The lane is empty" and "the lane could not be read" have different
    fixes, and a refusal that does not say which sends somebody to the wrong
    one. A person must never meet a bare failure."""
    import app.tools.fulfill_audio_requests as fr

    _idle_record(monkeypatch, fr, [])
    monkeypatch.setattr(fr, "fs_fetch", _forbidden)
    monkeypatch.setattr(fr.up, "s3_client", lambda: _explode([]))
    fr.run_eviction(commit=True)
    out = capsys.readouterr().out
    assert "could not be listed" in out
    assert "firebase deploy" in out


def test_a_shielded_book_is_kept_rather_than_refused_wholesale(monkeypatch, capsys):
    """The shield WORKING looks different from the shield MISSING: this object
    is kept because somebody has a place in it, and the run reports a normal
    keep rather than the guard's refusal."""
    import app.tools.fulfill_audio_requests as fr

    recent = int(time.time() * 1000) - 86_400_000        # yesterday, in MILLIseconds
    _idle_record(monkeypatch, fr,
                 [_doc("b-4754c8e4548e", {"integerValue": str(recent)})])
    stats = fr.run_eviction(commit=True)
    out = capsys.readouterr().out
    assert stats["candidates"] == 0
    assert "refused_no_shield" not in stats
    assert "mid-book shield" in out
    assert "[keep]" in out


def test_the_override_exists_and_is_deliberately_ugly_to_type():
    """Per the estate's "mechanical guards beat written advice" rule: a real
    guard has a real escape hatch, and the hatch is an explicit flag rather
    than something anybody types by accident."""
    import app.tools.fulfill_audio_requests as fr

    src = Path(fr.__file__).read_text(encoding="utf-8")
    assert "--evict-without-position-shield" in src
    assert "without_position_shield" in src


def _explode(seen):
    class _Client:
        def delete_object(self, **kw):
            seen.append(kw.get("Key"))
    return _Client()


def _forbidden(_collection):
    raise RuntimeError("HTTP Error 403: Forbidden")
