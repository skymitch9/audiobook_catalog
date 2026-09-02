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
