"""Tests for the DISASTER-RECOVERY archive of the library into R2.

Four things are worth pinning here, and none of them is "does boto3 work":

  1. ⚠️ **THE EXCLUSION RULE.** `zzzz_Books_to_be_Converted/` is excluded by
     standing rule; everything else on disk is archived. Both halves are
     load-bearing and they fail in opposite directions — over-matching quietly
     drops real books from the only off-site copy, under-matching uploads 69 GB
     of part-files nobody wants. The rule matches a whole DIRECTORY segment, so
     a book whose *filename* contains the phrase is still archived.
  2. ⚠️ **THE KEY SCHEME.** `archive/` + the library-relative path, verbatim.
     Every object already uploaded is stored under it and a restore rebuilds the
     tree from it, so changing `archive_key()` means re-sending ~685 GB. The
     golden fixtures spell out exact keys and `test_key_scheme_mutations_fail`
     states as executable code which plausible "improvements" must break.
  3. 🔴 **THE PREFIX IS NOT A CACHE.** The streaming uploader writes to the same
     bucket at the root, and `fulfill_audio_requests.evict_candidates()` deletes
     from it. `test_eviction_never_touches_archive_prefix` is the mechanical
     guard for "nothing may ever evict the backup".
  4. **THE SKIP LOGIC.** An hourly task over 1,260 files must not re-read 685 GB
     to learn nothing changed, and must not skip a file that genuinely did.

Everything that touches the network (`upload_one`, `abort_stale_multipart`) is
deliberately NOT tested here — it is exercised for real against R2, and a mock
of boto3 would only test the mock.
"""

from __future__ import annotations

import json
import os

import pytest

from app.tools import fulfill_audio_requests as fr
from scripts import archive_audio_r2 as ar


# ---------------------------------------------------------------------------
# 1. the exclusion rule
# ---------------------------------------------------------------------------
EXCLUDED = [
    "zzzz_Books_to_be_Converted/part01.m4a",
    "zzzz_Books_to_be_Converted/Some Book/part01.m4a",
    # Case is not a property anyone maintains on Windows.
    "ZZZZ_BOOKS_TO_BE_CONVERTED/part01.m4a",
    "zzzz_books_to_be_converted/part01.m4a",
    # Backslashes: this pipeline only ever runs on Windows.
    "zzzz_Books_to_be_Converted\\Some Book\\part01.m4a",
    # Nested anywhere, not only at the root.
    "Brandon Sanderson/zzzz_Books_to_be_Converted/part01.m4a",
]

INCLUDED = [
    "Brandon Sanderson/Skyward.m4b",
    "epubor_ultimate.exe",
    "Ryuto/Ryuto - Shortcut.lnk",
    "Selkie Myth/BtDEM 15 Rise from the Ashes - Selkie Myth - 20250515.epub.bak",
    "B000Q9F2YE_EBSP.azw.kfx-zip",
    # ⚠️ The filename contains the excluded phrase; the FILE is still archived.
    # Only directory segments are matched.
    "Some Author/zzzz_books_to_be_converted.m4b",
    "zzzz_Books_to_be_Converted.m4b",
]


@pytest.mark.parametrize("rel", EXCLUDED)
def test_staging_pile_is_excluded(rel):
    assert ar.is_excluded(rel) is True


@pytest.mark.parametrize("rel", INCLUDED)
def test_everything_else_is_archived(rel):
    assert ar.is_excluded(rel) is False


def test_exclusion_is_a_directory_match_not_a_substring():
    """⚠️ A substring rule would have dropped these from the only off-site copy."""
    assert ar.is_excluded("Author/zzzz_Books_to_be_Converted Anthology.m4b") is False
    assert ar.is_excluded("Not zzzz_Books_to_be_Converted/book.m4b") is False


def test_walk_prunes_the_staging_pile(tmp_path):
    """The walk must never even descend into it — 117 files / 69 GB of stat
    calls for a directory whose whole point is being ignored."""
    (tmp_path / "Brandon Sanderson").mkdir()
    (tmp_path / "Brandon Sanderson" / "Skyward.m4b").write_bytes(b"x" * 10)
    staging = tmp_path / "zzzz_Books_to_be_Converted"
    staging.mkdir()
    (staging / "part01.m4a").write_bytes(b"y" * 10)
    (staging / "nested").mkdir()
    (staging / "nested" / "part02.m4a").write_bytes(b"z" * 10)
    (tmp_path / "loose_at_root.epub").write_bytes(b"w" * 10)

    found = ar.scan_local(tmp_path)
    assert set(found) == {"Brandon Sanderson/Skyward.m4b", "loose_at_root.epub"}


def test_scan_archives_every_extension(tmp_path):
    """⚠️ Not just app.config.EXTS. This is a mirror, not a catalogue: the exes,
    the .bak files and the .lnk go up too. 'We lose this data we lose it all'
    is an instruction to copy, not to judge."""
    for name in ("book.m4b", "installer.exe", "notes.pdf", "cover.jpg", "link.lnk", "old.epub.bak"):
        (tmp_path / name).write_bytes(b"x")
    assert set(ar.scan_local(tmp_path)) == {
        "book.m4b", "installer.exe", "notes.pdf", "cover.jpg", "link.lnk", "old.epub.bak",
    }


# ---------------------------------------------------------------------------
# 2. the key scheme
# ---------------------------------------------------------------------------
GOLDEN = [
    ("Brandon Sanderson/Skyward.m4b", "archive/Brandon Sanderson/Skyward.m4b"),
    # Windows separators fold.
    ("Brandon Sanderson\\Skyward.m4b", "archive/Brandon Sanderson/Skyward.m4b"),
    # A leading slash is stripped, not doubled into the prefix.
    ("/Disney Books/Doc McStuffins.m4b", "archive/Disney Books/Doc McStuffins.m4b"),
    # The 15 files that sit at the library root with no author folder.
    ("epubor_ultimate.exe", "archive/epubor_ultimate.exe"),
    # Apostrophes, ampersands and non-ASCII are carried literally, unencoded —
    # boto3 signs and encodes the key itself; wrangler's URL-splicing bugs
    # (which cost 8 covers) do not apply to this transport.
    ("Marvel Press Book Group/Friends and Foes - Marvel's Avengers.m4b",
     "archive/Marvel Press Book Group/Friends and Foes - Marvel's Avengers.m4b"),
    ("Brené Brown/Atlas of the Heart.m4b", "archive/Brené Brown/Atlas of the Heart.m4b"),
    ("J. S. Morin/Galaxy Outlaws- The Complete Black Ocean Mobius Missions, 1-16.5.m4b",
     "archive/J. S. Morin/Galaxy Outlaws- The Complete Black Ocean Mobius Missions, 1-16.5.m4b"),
]


@pytest.mark.parametrize("rel,expected", GOLDEN)
def test_key_scheme_is_golden(rel, expected):
    assert ar.archive_key(rel) == expected


def test_every_key_lives_under_the_archive_prefix():
    for rel, _ in GOLDEN:
        assert ar.archive_key(rel).startswith(ar.ARCHIVE_PREFIX)


def test_key_scheme_mutations_fail():
    """⚠️ Plausible 'improvements' that would orphan ~685 GB of objects.

    Stated as executable code so the next person to reach for one of them gets
    a red test rather than a silent re-upload of the whole library.
    """
    key = ar.archive_key("Brené Brown/Atlas of the Heart.m4b")
    assert key != key.lower(), "case folding would orphan every mixed-case object"
    assert "%20" not in key, "URL-encoding would orphan every object with a space"
    assert not key.startswith("/"), "a leading slash makes an empty first path segment"
    assert ar.archive_key("A/b.m4b") != "A/b.m4b", "dropping the prefix mixes archive with cache"


def test_empty_path_is_refused():
    for bad in ("", "/", "\\", None):
        with pytest.raises(ValueError):
            ar.archive_key(bad)


def test_content_type_for_known_and_unknown():
    assert ar.content_type_for("a/b.m4b") == "audio/mp4"
    assert ar.content_type_for("a/b.M4B") == "audio/mp4"
    assert ar.content_type_for("a/b.epub") == "application/epub+zip"
    assert ar.content_type_for("a/b.lnk") == "application/octet-stream"


# ---------------------------------------------------------------------------
# 3. 🔴 the archive prefix is not a cache
# ---------------------------------------------------------------------------
def test_archive_prefix_does_not_collide_with_the_streaming_keys():
    """The streaming uploader writes the SAME library at the bucket root. The
    two must be distinguishable by key alone, because that is the only thing a
    deletion pass can see."""
    from scripts import upload_audio_r2 as up

    rel = "Brandon Sanderson/Skyward.m4b"
    assert up.object_key(rel) == rel
    assert ar.archive_key(rel) == "archive/" + rel
    assert up.object_key(rel) != ar.archive_key(rel)
    assert up.BUCKET == ar.BUCKET, "same bucket — which is exactly why the prefix matters"


def test_eviction_never_touches_archive_prefix():
    """🔴 THE GUARD. An object under `archive/` is the off-site copy of the
    master; deleting it can lose the only copy. Fed an entry that is idle by
    every measure the evictor has, it must still refuse."""
    long_ago = "2020-01-01T00:00:00Z"
    files = {
        "archive/Brandon Sanderson/Skyward.m4b": {
            "streamable": True, "last_stream_at": long_ago, "last_position_at": long_ago,
        },
        "Brandon Sanderson/Skyward.m4b": {
            "streamable": True, "last_stream_at": long_ago, "last_position_at": long_ago,
        },
    }
    candidates, refusals = fr.evict_candidates(files, idle_days=30)
    assert "Brandon Sanderson/Skyward.m4b" in candidates, "the CACHE copy is evictable"
    assert "archive/Brandon Sanderson/Skyward.m4b" not in candidates
    assert any("archive/" in r for r in refusals), "the refusal must be worded, not silent"


# ---------------------------------------------------------------------------
# 4. the skip logic
# ---------------------------------------------------------------------------
def _hasher(value="deadbeef"):
    calls = []

    def h():
        calls.append(1)
        return value

    h.calls = calls
    return h


def test_new_file_uploads_without_hashing_first():
    h = _hasher()
    verdict, digest = ar.decide({"size": 10, "mtime_ns": 1}, None, False, h)
    assert verdict == "upload"
    assert digest is None
    assert not h.calls, "hashing a brand-new file before upload is wasted disk I/O"


def test_unchanged_size_and_mtime_skips_without_reading():
    h = _hasher()
    rec = {"size": 10, "mtime_ns": 1, "sha256": "abc"}
    verdict, digest = ar.decide({"size": 10, "mtime_ns": 1}, rec, False, h)
    assert (verdict, digest) == ("skip", "abc")
    assert not h.calls, "an hourly task must not read 685 GB to learn nothing changed"


def test_touched_but_identical_costs_a_hash_not_an_upload():
    h = _hasher("abc")
    rec = {"size": 10, "mtime_ns": 1, "sha256": "abc"}
    verdict, digest = ar.decide({"size": 10, "mtime_ns": 999}, rec, False, h)
    assert (verdict, digest) == ("skip", "abc")
    assert h.calls, "the hash is the authority; mtime alone may only say 'skip'"


def test_changed_bytes_upload():
    h = _hasher("newdigest")
    rec = {"size": 10, "mtime_ns": 1, "sha256": "abc"}
    verdict, digest = ar.decide({"size": 20, "mtime_ns": 2}, rec, False, h)
    assert (verdict, digest) == ("upload", "newdigest")


def test_force_bypasses_every_tier():
    h = _hasher()
    rec = {"size": 10, "mtime_ns": 1, "sha256": "abc"}
    verdict, _ = ar.decide({"size": 10, "mtime_ns": 1}, rec, True, h)
    assert verdict == "upload"
    assert not h.calls


def test_orphans_are_reported_and_never_deleted():
    """⚠️ A file that has left the disk is the exact event this archive exists
    for. Its object is the last copy; `orphan_paths` reports, it never deletes."""
    local = {"a.m4b": {}}
    record = {"a.m4b": {}, "gone.m4b": {}}
    assert ar.orphan_paths(local, record) == ["gone.m4b"]


def test_manifest_entry_shape():
    entry = ar.manifest_entry("Author/Book.m4b", {"size": 5, "mtime_ns": 7}, "abc")
    assert entry["path"] == "Author/Book.m4b"
    assert entry["key"] == "archive/Author/Book.m4b"
    assert entry["size"] == 5 and entry["mtime_ns"] == 7 and entry["sha256"] == "abc"
    assert entry["uploaded_at"].endswith("Z")


# ---------------------------------------------------------------------------
# rate / ETA — honest about not knowing
# ---------------------------------------------------------------------------
def test_rate_is_none_without_enough_samples():
    assert ar.observed_rate_bps({}) is None
    assert ar.observed_rate_bps({"a": {"uploaded_at": "2026-08-18T00:00:00Z", "size": 1}}) is None


def test_rate_is_measured_from_the_manifest_timestamps():
    record = {
        "a": {"uploaded_at": "2026-08-18T00:00:00Z", "size": 1_000_000},
        "b": {"uploaded_at": "2026-08-18T00:00:10Z", "size": 10_000_000},
        "c": {"uploaded_at": "2026-08-18T00:00:20Z", "size": 10_000_000},
    }
    # 20 MB landed across the 20 s the window spans; the first sample's bytes
    # landed before the window opened and are not counted.
    assert ar.observed_rate_bps(record) == pytest.approx(1_000_000.0)


def test_unparseable_timestamps_do_not_crash_the_rate():
    assert ar.observed_rate_bps({"a": {"uploaded_at": "nonsense", "size": 1}}) is None


# ---------------------------------------------------------------------------
# the lock — PID liveness, not mtime
# ---------------------------------------------------------------------------
def test_dead_holder_is_stale_immediately():
    """⚠️ A crashed run must not block the archive for the age ceiling. pid 2^31
    minus one is not a live process on any Windows box."""
    assert ar.lock_is_stale({"pid": 2147483646, "started_at": ar.now_iso()}) is True


def test_live_holder_is_not_stale_even_when_quiet():
    """⚠️ And the inverse, which an mtime-only check gets WRONG: a run uploading
    one 4 GB file touches nothing for an hour and is very much alive."""
    holder = {"pid": os.getpid(), "started_at": ar.now_iso()}
    assert ar.lock_is_stale(holder) is False


def test_live_holder_older_than_the_ceiling_is_stale():
    holder = {"pid": os.getpid(), "started_at": "2020-01-01T00:00:00Z"}
    assert ar.lock_is_stale(holder) is True


def test_garbage_pid_is_stale():
    assert ar.lock_is_stale({"pid": "not-a-pid", "started_at": ar.now_iso()}) is True


def test_lock_round_trip_and_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "LOCK_PATH", tmp_path / "audio_archive.lock")
    lock = ar.ArchiveLock().acquire()
    try:
        assert ar.LOCK_PATH.exists()
        lock.heartbeat(current_file="Author/Book.m4b", done_this_run=3)
        payload = json.loads(ar.LOCK_PATH.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()
        assert payload["current_file"] == "Author/Book.m4b"
        assert payload["done_this_run"] == 3
        # Single flight: a second acquire while this one is live must refuse.
        with pytest.raises(ar.ArchiveLockHeld):
            ar.ArchiveLock().acquire()
    finally:
        lock.release()
    assert not ar.LOCK_PATH.exists()


def test_manifest_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "MANIFEST_PATH", tmp_path / "audio_archive_manifest.json")
    files = {"Author/Book.m4b": ar.manifest_entry("Author/Book.m4b", {"size": 5, "mtime_ns": 7}, "abc")}
    failures = {"Author/Bad.m4b": {"error": "boom", "attempts": 2, "last_try": ar.now_iso()}}
    ar.write_manifest(files, failures)
    back_files, back_failures = ar.load_manifest()
    assert back_files == files
    assert back_failures == failures
    raw = json.loads(ar.MANIFEST_PATH.read_text(encoding="utf-8"))
    assert raw["bucket"] == ar.BUCKET
    assert raw["prefix"] == "archive/"
    assert "NOTHING MAY EVER EVICT" in raw["_comment"]


def test_corrupt_manifest_is_a_warning_not_a_crash(tmp_path, monkeypatch, capsys):
    """A truncated JSON file must not stop the archive from archiving. The worst
    it costs is re-uploading; refusing to run costs the backup."""
    path = tmp_path / "audio_archive_manifest.json"
    path.write_text('{"files": {"a": ', encoding="utf-8")
    monkeypatch.setattr(ar, "MANIFEST_PATH", path)
    assert ar.load_manifest() == ({}, {})
    assert "unreadable" in capsys.readouterr().out
