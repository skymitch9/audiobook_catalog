"""Tests for app/tools/fs_watcher.py — the reactive pipeline trigger
(docs/TODO.md "⚡ REACTIVE PIPELINE", owner design agreed 2026-08-16).

Covers, with temp dirs and a fake clock (never the real library, never a
real pipeline run — subprocess.call is always stubbed):
  * scan exclusions (zzzz_Books_to_be_Converted, non-book extensions)
  * baseline initialization never fires on a pre-existing tree
  * settle: a stable file fires after SETTLE_SECONDS; a still-growing file
    resets its clock and does not
  * validity: a byte-stable file that never parses blocks the fire until
    INVALID_GIVEUP_SECONDS, is then abandoned into the baseline
  * coalescing: a burst of arrivals produces exactly ONE run
  * single-flight interplay: pipeline lock held -> no fire, pending
    persists; lock released after a foreign run -> re-baseline, no fire;
    and the scheduled defer machinery defers around a 'reactive' holder
  * cooldown, removal/folder-structure triggers, fail-streak stand-down
  * the fired command is sync_to_drive.py with PIPELINE_TRIGGER=reactive
"""

from __future__ import annotations

import json
import os
import zipfile
from types import SimpleNamespace

import pytest

from app.core import pipeline_lock as pl
from app.core import pipeline_schedule as sched
from app.tools import fs_watcher as fsw


@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / "books"
    root.mkdir()
    monkeypatch.setattr(fsw, "WATCH_ROOT", root)
    monkeypatch.setattr(fsw, "STATE_PATH", tmp_path / "fs_watcher_state.json")
    monkeypatch.setattr(fsw, "TICK_LOCK_PATH", tmp_path / "fs_watcher.lock")
    monkeypatch.setattr(fsw, "NOTICE_PATH", tmp_path / "fs_watcher_notice.txt")
    monkeypatch.setattr(fsw, "LOG_PATH", tmp_path / "pipeline_8h.log")
    monkeypatch.setattr(pl, "LOCK_PATH", tmp_path / "pipeline.lock")

    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(fsw, "_now", lambda: clock["t"])

    calls: list[SimpleNamespace] = []
    rc = {"value": 0}

    def fake_call(cmd, **kw):
        calls.append(SimpleNamespace(cmd=cmd, env=kw.get("env") or {}, kw=kw))
        return rc["value"]

    monkeypatch.setattr(fsw.subprocess, "call", fake_call)
    # Validity defaults to True; tests that exercise the real _file_valid use
    # the captured genuine implementation.
    genuine_file_valid = fsw._file_valid
    monkeypatch.setattr(fsw, "_file_valid", lambda p: True)

    return SimpleNamespace(
        root=root,
        clock=clock,
        calls=calls,
        rc=rc,
        tmp=tmp_path,
        monkeypatch=monkeypatch,
        genuine_file_valid=genuine_file_valid,
    )


def _add_book(root, rel, size=64):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def _init_baseline(env):
    """First tick snapshots the tree; assert it never fires."""
    assert fsw.poll_once() == 0
    assert env.calls == []


def _settle(env, seconds=None):
    env.clock["t"] += (seconds if seconds is not None else fsw.SETTLE_SECONDS + 1)


def _state():
    return json.loads(fsw.STATE_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


def test_scan_excludes_staging_pile_and_non_book_files(env):
    _add_book(env.root, "Author One/Book.m4b")
    _add_book(env.root, "Author One/notes.txt")  # not a book extension
    _add_book(env.root, "zzzz_Books_to_be_Converted/part01.m4b")
    _add_book(env.root, "zzzz_Books_to_be_Converted/deeper/part02.m4b")
    snap = fsw._scan()
    assert set(snap["files"]) == {"Author One/Book.m4b"}
    assert snap["dirs"] == ["Author One"]  # the staging pile is invisible


def test_scan_returns_none_when_root_missing(env):
    fsw.WATCH_ROOT.rmdir()
    assert fsw._scan() is None
    # ...and a tick treats that as idle, never as "everything was deleted"
    assert fsw.poll_once() == 0
    assert env.calls == []


# ---------------------------------------------------------------------------
# Baseline init / settle / fire
# ---------------------------------------------------------------------------


def test_first_tick_baselines_preexisting_tree_without_firing(env):
    _add_book(env.root, "Author/Old Book.m4b")
    _init_baseline(env)
    # Nothing changed -> later ticks stay quiet too
    _settle(env)
    assert fsw.poll_once() == 0
    assert env.calls == []


def test_new_settled_book_fires_reactive_run(env):
    _init_baseline(env)
    _add_book(env.root, "New Author/New Book.m4b")
    assert fsw.poll_once() == 0  # first sighting: clock starts, no fire
    assert env.calls == []
    _settle(env)
    assert fsw.poll_once() == 0
    assert len(env.calls) == 1
    call = env.calls[0]
    assert call.env.get("PIPELINE_TRIGGER") == "reactive"
    assert call.cmd[1].endswith("sync_to_drive.py")
    # after a clean run: pending cleared, baseline includes the new book
    st = _state()
    assert st["pending"] == {}
    assert "New Author/New Book.m4b" in st["baseline"]["files"]


def test_growing_file_resets_its_settle_clock(env):
    _init_baseline(env)
    book = _add_book(env.root, "Author/Big Book.m4b", size=10)
    fsw.poll_once()
    _settle(env)
    # still copying: signature changed since last tick
    book.write_bytes(b"y" * 500)
    os.utime(book, ns=(book.stat().st_atime_ns, book.stat().st_mtime_ns + 10**9))
    assert fsw.poll_once() == 0
    assert env.calls == []  # NOT ingested truncated — the whole point
    _settle(env)
    assert fsw.poll_once() == 0
    assert len(env.calls) == 1  # fires only once it stopped changing


def test_coalescing_burst_of_books_is_one_run(env):
    _init_baseline(env)
    for i in range(8):
        _add_book(env.root, f"Author {i}/Book {i}.m4b")
    fsw.poll_once()
    _settle(env)
    fsw.poll_once()
    assert len(env.calls) == 1  # eight arrivals, ONE run


def test_late_arrival_holds_the_whole_batch(env):
    _init_baseline(env)
    _add_book(env.root, "A/one.m4b")
    fsw.poll_once()
    _settle(env, fsw.SETTLE_SECONDS - 5)
    _add_book(env.root, "B/two.m4b")  # arrives late, not yet settled
    assert fsw.poll_once() == 0
    _settle(env, 10)  # one.m4b is now well past settle; two.m4b is not
    assert fsw.poll_once() == 0
    assert env.calls == []  # the unsettled newcomer holds the batch
    _settle(env)
    fsw.poll_once()
    assert len(env.calls) == 1


def test_removal_and_folder_change_fire_without_media_validation(env):
    book = _add_book(env.root, "Author/Book.m4b")
    _init_baseline(env)
    book.unlink()
    (env.root / "Author").rename(env.root / "Renamed Author")
    fsw.poll_once()
    _settle(env)
    fsw.poll_once()
    assert len(env.calls) == 1


# ---------------------------------------------------------------------------
# Validity — the second settle signal
# ---------------------------------------------------------------------------


def test_file_valid_real_epub_and_garbage(env, tmp_path):
    good = tmp_path / "good.epub"
    with zipfile.ZipFile(good, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
    bad = tmp_path / "bad.epub"
    bad.write_bytes(b"this is not a zip")
    garbage_m4b = tmp_path / "bad.m4b"
    garbage_m4b.write_bytes(b"\x00" * 128)  # mutagen cannot open this
    genuine = env.genuine_file_valid
    assert genuine(good) is True
    assert genuine(bad) is False
    assert genuine(garbage_m4b) is False
    assert genuine(tmp_path / "missing.m4b") is False  # never raises


def test_invalid_media_blocks_then_is_abandoned_loudly(env):
    env.monkeypatch.setattr(fsw, "_file_valid", lambda p: False)
    _init_baseline(env)
    _add_book(env.root, "Author/corrupt.m4b")
    fsw.poll_once()
    _settle(env)
    assert fsw.poll_once() == 0
    assert env.calls == []  # byte-stable but unparseable: refuse to ingest
    # ...but it must not block the watcher forever:
    env.clock["t"] += fsw.INVALID_GIVEUP_SECONDS + 1
    assert fsw.poll_once() == 0
    assert env.calls == []  # abandoned, not fired
    st = _state()
    assert st["pending"] == {}
    assert "Author/corrupt.m4b" in st["baseline"]["files"]  # folded into baseline
    # a subsequent VALID arrival still triggers normally
    env.monkeypatch.setattr(fsw, "_file_valid", lambda p: True)
    _add_book(env.root, "Author/fine.m4b")
    fsw.poll_once()
    _settle(env)
    fsw.poll_once()
    assert len(env.calls) == 1


# ---------------------------------------------------------------------------
# Single-flight / foreign-run interplay
# ---------------------------------------------------------------------------


def test_lock_held_defers_watcher_and_completion_rebaselines(env):
    _init_baseline(env)
    _add_book(env.root, "Author/Book.m4b")
    fsw.poll_once()
    _settle(env)
    lock = pl.acquire("scheduled")  # a foreign run is in flight (our live pid)
    try:
        assert fsw.poll_once() == 0
        assert env.calls == []  # never fire on top of another run
        assert _state()["pending"] != {}  # ...but the delta is NOT forgotten
    finally:
        lock.release()
    # the foreign run completed — it already ingested the tree, so the
    # watcher re-baselines instead of firing a redundant run
    assert fsw.poll_once() == 0
    assert env.calls == []
    st = _state()
    assert st["pending"] == {}
    assert "Author/Book.m4b" in st["baseline"]["files"]
    assert st["foreign_run"] is None


FAKE_PID = 987654  # never a real pid: liveness is always monkeypatched below


def _write_foreign_lock(env, alive: bool):
    """A foreign run's lock with a fake pid whose liveness the test controls —
    never the machine's real pids."""
    from datetime import datetime, timezone

    pl.LOCK_PATH.write_text(json.dumps({
        "pid": FAKE_PID, "host": "TESTHOST", "trigger": "scheduled",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")
    liveness = {"alive": alive}
    env.monkeypatch.setattr(pl, "pid_alive", lambda pid: liveness["alive"] if pid == FAKE_PID else False)
    return liveness


def test_live_foreign_holder_is_observed_exactly_as_before(env, capsys):
    _init_baseline(env)
    _add_book(env.root, "Author/Book.m4b")
    fsw.poll_once()
    _settle(env)
    _write_foreign_lock(env, alive=True)
    assert fsw.poll_once() == 0
    assert fsw.poll_once() == 0
    assert env.calls == []
    st = _state()
    assert st["pending"] != {}
    assert st["foreign_run"].startswith(f"pid {FAKE_PID} on TESTHOST")
    out = capsys.readouterr().out
    assert out.count("observing, not evaluating") == 1
    assert "leftover" not in out


def test_dead_holder_with_lock_present_is_not_in_flight(env, capsys):
    """2026-11-23: a killed run's lock stays on disk. It must not freeze the
    watcher — and the watcher must not delete it (acquire() reclaims)."""
    _init_baseline(env)
    _write_foreign_lock(env, alive=False)
    _add_book(env.root, "Author/Book.m4b")
    fsw.poll_once()
    _settle(env)
    assert fsw.poll_once() == 0
    assert len(env.calls) == 1  # fired: the leftover is not a run in flight
    assert pl.LOCK_PATH.exists()  # left for the child's acquire() to reclaim
    out = capsys.readouterr().out
    assert out.count("pipeline lock is a leftover") == 1  # said once, not per tick
    assert f"pid {FAKE_PID} is not running" in out
    assert "observing" not in out
    assert _state()["foreign_run"] is None


def test_observed_run_that_died_keeps_pending_and_fires(env, capsys):
    """The incident itself: the watcher observed a live run, the run was
    killed (lock left, pid dead). Pending must survive and a run must fire
    after cooldown — never 're-baselining without firing'."""
    _init_baseline(env)
    _add_book(env.root, "Author/Book.m4b")
    fsw.poll_once()
    _settle(env)
    liveness = _write_foreign_lock(env, alive=True)
    fsw.poll_once()  # observing
    assert env.calls == []
    liveness["alive"] = False  # killed mid-run; lock stays on disk
    fsw.poll_once()
    out = capsys.readouterr().out
    assert "did NOT finish" in out
    assert "re-baselining" not in out
    st = _state()
    assert "f:Author/Book.m4b" in st["pending"]
    assert "Author/Book.m4b" not in st["baseline"]["files"]
    assert st["foreign_run"] is None
    assert st["fail_streak"] == 1  # the one shared fail-streak model
    assert env.calls == []  # cooling down, as after our own failed child
    fsw.poll_once()
    assert env.calls == [] and _state()["fail_streak"] == 1  # counted once, not per tick
    pl.LOCK_PATH.unlink()  # then a human moves the leftover aside (the literal incident)
    fsw.poll_once()
    assert "re-baselining" not in capsys.readouterr().out
    assert "f:Author/Book.m4b" in _state()["pending"]
    env.clock["t"] += fsw.COOLDOWN_MIN * 60 + 1
    fsw.poll_once()
    assert len(env.calls) == 1
    assert _state()["pending"] == {} and _state()["fail_streak"] == 0


def test_observed_run_that_released_its_lock_still_rebaselines(env, capsys):
    _init_baseline(env)
    _add_book(env.root, "Author/Book.m4b")
    fsw.poll_once()
    _settle(env)
    _write_foreign_lock(env, alive=True)
    fsw.poll_once()  # observing
    pl.LOCK_PATH.unlink()  # the run's finally released it; its pid then exits
    env.monkeypatch.setattr(pl, "pid_alive", lambda pid: False)
    fsw.poll_once()
    assert env.calls == []
    st = _state()
    assert st["pending"] == {}
    assert "Author/Book.m4b" in st["baseline"]["files"]
    assert "completed — re-baselining without firing" in capsys.readouterr().out


def test_scheduled_defer_machinery_defers_around_a_reactive_holder(env, tmp_path):
    """The other direction: while OUR reactive run holds the single-flight
    lock, the true 8h slot must defer-not-skip around it — the existing
    pipeline_schedule state machine, exercised against a 'reactive' holder."""
    env.monkeypatch.setattr(sched, "DEFER_MARKER_PATH", tmp_path / "defer.json")

    class _FakeStatus:
        def __getattr__(self, name):
            return lambda *a, **k: ""

    env.monkeypatch.setattr(sched, "pstatus", _FakeStatus())
    reactive_lock = pl.acquire("reactive")
    env.monkeypatch.setattr(sched.time, "sleep", lambda s: reactive_lock.release())
    ran = []
    assert sched.run_with_defer(lambda: ran.append(True)) == "ran"
    assert ran == [True]


def test_tick_lock_prevents_overlapping_ticks(env):
    fsw.TICK_LOCK_PATH.write_text("123", encoding="utf-8")
    _add_book(env.root, "Author/Book.m4b")
    assert fsw.poll_once() == 0
    assert not fsw.STATE_PATH.exists()  # tick never ran (no baseline written)


# ---------------------------------------------------------------------------
# Cooldown and failure stand-down
# ---------------------------------------------------------------------------


def test_cooldown_spaces_out_back_to_back_fires(env):
    _init_baseline(env)
    _add_book(env.root, "A/one.m4b")
    fsw.poll_once()
    _settle(env)
    fsw.poll_once()
    assert len(env.calls) == 1
    _add_book(env.root, "B/two.m4b")  # second batch right behind the first
    fsw.poll_once()
    _settle(env)
    fsw.poll_once()
    assert len(env.calls) == 1  # settled, but inside the cooldown window
    env.clock["t"] += fsw.COOLDOWN_MIN * 60 + 1
    fsw.poll_once()
    assert len(env.calls) == 2


def test_fail_streak_stands_down_to_self_heal(env):
    env.rc["value"] = 1  # every fired run exits nonzero (e.g. lost lock race)
    _init_baseline(env)
    _add_book(env.root, "Author/Book.m4b")
    fsw.poll_once()
    _settle(env)
    for attempt in range(fsw.MAX_FAIL_STREAK):
        fsw.poll_once()
        env.clock["t"] += fsw.COOLDOWN_MIN * 60 + 1
    assert len(env.calls) == fsw.MAX_FAIL_STREAK
    st = _state()
    assert st["pending"] == {}  # stood down; the 8h self-heal run owns it
    assert st["fail_streak"] == 0
    fsw.poll_once()
    assert len(env.calls) == fsw.MAX_FAIL_STREAK  # no more retries


# ---------------------------------------------------------------------------
# Config sanity — the agreed design numbers
# ---------------------------------------------------------------------------


def test_design_thresholds():
    assert fsw.SETTLE_SECONDS == 60  # "size+mtime stable for ~60s"
    assert fsw.COOLDOWN_MIN == 10  # same band as the remote watcher
    assert "zzzz_books_to_be_converted" in fsw.EXCLUDED_DIR_NAMES
    assert ".m4b" in fsw.WATCH_EXTS and ".epub" in fsw.WATCH_EXTS
