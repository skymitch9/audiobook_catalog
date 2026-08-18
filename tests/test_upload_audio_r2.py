"""Tests for the audio-player phase 0b ingest — uploader + on-demand queue.

Four things are worth pinning, and none of them is "does boto3 work":

  1. ⚠️ **THE KEY SCHEME.** The R2 object key is the library-relative path,
     verbatim. The phase-1 Worker will resolve `anchor -> path -> object key`
     assuming the last arrow is the identity function, and objects are stored
     under it. Changing `object_key()` is a migration, not an edit, so the
     golden fixtures spell out exact keys and `test_key_scheme_mutations_fail`
     states as executable code which plausible "improvements" must break.
  2. **The anchor fold is SHARED with ebooks**, not a second implementation.
  3. ⚠️ **THE DUPLICATE CLAUSE.** One request doc per BOOK; a second requester
     joins the pile. `dedupe_requests()` is the read-side half.
  4. 🔴 **THE EVICTION GUARD.** `evict_candidates()` must delete NOTHING while
     access timestamps are absent. A date-based purge on missing data would
     evict every book 30 days after upload, mid-listen included.

Everything that touches the network (`upload_via_s3`, the Firestore listing)
is deliberately NOT tested here — it is exercised for real, and a mock of
boto3 would only test the mock.
"""

from __future__ import annotations

import time

import pytest

from app.tools import fulfill_audio_requests as fr
from scripts import upload_audio_r2 as up
from scripts.build_ebook_manifest import ebook_anchor


# ---------------------------------------------------------------------------
# 1. the key scheme
# ---------------------------------------------------------------------------
GOLDEN = [
    ("Brandon Sanderson/Skyward.m4b", "Brandon Sanderson/Skyward.m4b"),
    # Windows separators fold; this pipeline only ever runs on Windows.
    ("Brandon Sanderson\\Skyward.m4b", "Brandon Sanderson/Skyward.m4b"),
    ("/Disney Books/Doc McStuffins.m4b", "Disney Books/Doc McStuffins.m4b"),
    # Apostrophes, ampersands and non-ASCII are carried literally, unencoded.
    ("Marvel Press Book Group/Friends and Foes - Marvel's Avengers.m4b",
     "Marvel Press Book Group/Friends and Foes - Marvel's Avengers.m4b"),
    ("Brené Brown/Atlas of the Heart.m4b", "Brené Brown/Atlas of the Heart.m4b"),
]


@pytest.mark.parametrize("rel,expected", GOLDEN)
def test_object_key_is_the_relative_path_verbatim(rel, expected):
    assert up.object_key(rel) == expected


def test_key_scheme_mutations_fail():
    """The four "improvements" that would silently orphan every object."""
    key = up.object_key("Brandon Sanderson/Skyward.m4b")
    assert not key.startswith("audio/"), "a prefix is a migration, not a tidy-up"
    assert key != key.lower(), "case folding is a migration"
    assert not key.startswith("b-"), "keying on the anchor is a migration"
    assert "%20" not in key, "URL-encoding the key is a migration"


def test_an_empty_path_is_refused_rather_than_guessed():
    for bad in ("", None, "/"):
        with pytest.raises(ValueError):
            up.object_key(bad)


# ---------------------------------------------------------------------------
# 2. the anchor fold
# ---------------------------------------------------------------------------
def test_audio_anchor_is_the_same_fold_as_ebook_anchor():
    # ⚠️ Not "produces the same shape" — literally the same function, so the
    # two shelves can never drift into two answers for a file's identity.
    rel = "Brandon Sanderson/Skyward.m4b"
    assert up.audio_anchor(rel) == ebook_anchor(rel)
    assert up.audio_anchor(rel).startswith("b-")
    assert len(up.audio_anchor(rel)) == 14


def test_audio_anchor_normalises_separators_before_hashing():
    assert up.audio_anchor("A\\B.m4b") == up.audio_anchor("A/B.m4b")


# ---------------------------------------------------------------------------
# 3. the manifest record
# ---------------------------------------------------------------------------
def test_since_is_first_streamable_and_never_moves_on_re_upload():
    meta = {"size": 10, "mtime_ns": 1, "anchor": "b-x", "title": "T", "bookId": "t"}
    first = up.record_entry("A/B.m4b", meta, None)
    again = up.record_entry("A/B.m4b", meta, None, previous=first)
    assert again["since"] == first["since"]
    assert again["streamable"] is True


def test_a_re_upload_does_not_forge_access_data():
    """⚠️ Eviction reads these. A re-upload must neither look like a stream
    nor erase a real one."""
    meta = {"size": 10, "mtime_ns": 1}
    previous = {"since": "2026-01-01T00:00:00Z", "last_stream_at": "2026-08-01T00:00:00Z",
                "last_position_at": None}
    entry = up.record_entry("A/B.m4b", meta, None, previous=previous)
    assert entry["last_stream_at"] == "2026-08-01T00:00:00Z"
    assert entry["last_position_at"] is None


def test_there_is_no_bulk_mode(capsys):
    """⚠️ Ingest is on-demand by owner decision. Naming nothing is an error,
    not an invitation to upload 630 GB."""
    with pytest.raises(SystemExit):
        up.main([])
    assert "no bulk mode" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 4. the duplicate clause
# ---------------------------------------------------------------------------
def req(book_id, title, requesters, doc):
    return {"bookId": book_id, "bookTitle": title, "requesters": requesters, "docName": doc}


def test_three_people_asking_for_one_book_is_one_upload():
    merged = fr.dedupe_requests([
        req("skyward", "Skyward", ["uid-a"], "docs/audio_requests/skyward"),
        req("skyward", "Skyward", ["uid-b"], "docs/audio_requests_dev/skyward"),
        req("skyward", "Skyward", ["uid-c"], "docs/audio_requests/skyward"),
    ])
    assert len(merged) == 1
    assert merged[0]["requesters"] == ["uid-a", "uid-b", "uid-c"]
    # ⚠️ Both lanes' docs are cleared, or the book re-uploads next run.
    assert len(merged[0]["docNames"]) == 3


def test_distinct_books_stay_distinct():
    merged = fr.dedupe_requests([
        req("skyward", "Skyward", ["uid-a"], "d1"),
        req("defiant", "Defiant", ["uid-a"], "d2"),
    ])
    assert [m["bookId"] for m in merged] == ["defiant", "skyward"]


def test_a_missing_book_id_is_derived_from_the_title():
    merged = fr.dedupe_requests([req("", "The Way of Kings", ["uid-a"], "d1")])
    assert merged[0]["bookId"] == "the-way-of-kings"


def test_a_request_naming_nothing_is_dropped():
    assert fr.dedupe_requests([req("", "", ["uid-a"], "d1"), None]) == []


def test_parse_request_reads_the_requesters_array():
    doc = {"name": "docs/audio_requests/skyward", "fields": {
        "bookTitle": {"stringValue": "Skyward"},
        "bookId": {"stringValue": "skyward"},
        "requestedBy": {"stringValue": "uid-a"},
        "requesters": {"arrayValue": {"values": [
            {"stringValue": "uid-a"}, {"stringValue": "uid-b"}]}},
    }}
    assert fr.parse_request(doc)["requesters"] == ["uid-a", "uid-b"]


def test_parse_request_falls_back_to_requested_by():
    doc = {"name": "d", "fields": {"bookTitle": {"stringValue": "Skyward"},
                                   "requestedBy": {"stringValue": "uid-a"}}}
    assert fr.parse_request(doc)["requesters"] == ["uid-a"]


def test_already_streamable_matches_on_book_id_not_path():
    record = {"Brandon Sanderson/Skyward.m4b": {"streamable": True, "bookId": "skyward"}}
    assert fr.already_streamable("skyward", record) == "Brandon Sanderson/Skyward.m4b"
    assert fr.already_streamable("defiant", record) is None


def test_an_evicted_object_is_not_already_streamable():
    record = {"A/B.m4b": {"streamable": False, "bookId": "skyward"}}
    assert fr.already_streamable("skyward", record) is None


# ---------------------------------------------------------------------------
# 5. 🔴 the eviction guard
# ---------------------------------------------------------------------------
NOW = 1_800_000_000.0
DAY = 86400.0


def obj(**kw):
    base = {"streamable": True, "last_stream_at": None, "last_position_at": None}
    base.update(kw)
    return base


def test_no_access_data_evicts_nothing_and_says_so():
    """🔴 THE GUARD. Two nulls mean "never measured", not "never listened to".
    Until phase 2 wires access timestamps this must delete nothing at all."""
    files = {f"book-{i}.m4b": obj() for i in range(5)}
    candidates, refusals = fr.evict_candidates(files, now=NOW)
    assert candidates == []
    assert len(refusals) == 5
    assert all("no access data yet" in r for r in refusals)


def test_a_stream_older_than_thirty_days_is_evictable():
    files = {"old.m4b": obj(last_stream_at=NOW - 45 * DAY)}
    candidates, _ = fr.evict_candidates(files, now=NOW)
    assert candidates == ["old.m4b"]


def test_a_recent_stream_is_kept():
    files = {"fresh.m4b": obj(last_stream_at=NOW - 3 * DAY)}
    candidates, refusals = fr.evict_candidates(files, now=NOW)
    assert candidates == []
    assert "kept" in refusals[0]


def test_the_mid_book_shield_beats_a_stale_stream():
    """⚠️ Owner tuning (ii): a 30 h book over a month of commutes is paused for
    a week routinely. An in-progress POSITION keeps it, even if the last
    stream request is ancient."""
    files = {"midbook.m4b": obj(last_stream_at=NOW - 60 * DAY,
                                last_position_at=NOW - 2 * DAY)}
    candidates, _ = fr.evict_candidates(files, now=NOW)
    assert candidates == []


def test_the_threshold_is_thirty_days_not_seven():
    # The owner said 7; the accepted tuning is 30 (decision 3, ratified in 5).
    assert fr.EVICT_IDLE_DAYS == 30
    files = {"b.m4b": obj(last_stream_at=NOW - 10 * DAY)}
    assert fr.evict_candidates(files, now=NOW)[0] == []


def test_an_object_that_is_not_streamable_is_not_a_candidate():
    files = {"gone.m4b": obj(streamable=False, last_stream_at=NOW - 400 * DAY)}
    assert fr.evict_candidates(files, now=NOW) == ([], [])


@pytest.mark.parametrize("value,expected", [
    ("2026-08-17T21:03:11Z", True),
    (1_700_000_000, True),          # epoch seconds
    (1_700_000_000_000, True),      # epoch milliseconds
    ("not a date", False),
    (None, False),
])
def test_timestamp_parsing_accepts_both_shapes_and_refuses_junk(value, expected):
    assert (fr._parse_stamp(value) is not None) is expected


def test_a_junk_timestamp_is_treated_as_no_data_not_as_ancient():
    """⚠️ The dangerous failure: an unparseable stamp read as epoch 0 would
    make every object look 56 years idle."""
    files = {"junk.m4b": obj(last_stream_at="whenever")}
    candidates, refusals = fr.evict_candidates(files, now=NOW)
    assert candidates == []
    assert "no access data yet" in refusals[0]


def test_evict_candidates_never_mutates_the_record():
    files = {"a.m4b": obj(last_stream_at=NOW - 90 * DAY)}
    before = dict(files["a.m4b"])
    fr.evict_candidates(files, now=NOW)
    assert files["a.m4b"] == before


def test_default_now_is_the_clock():
    files = {"a.m4b": obj(last_stream_at=time.time() - 90 * DAY)}
    assert fr.evict_candidates(files)[0] == ["a.m4b"]
