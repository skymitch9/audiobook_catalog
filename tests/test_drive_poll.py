"""app/tools/drive_poll.py — the Drive-side reactive trigger (2026-08-26).

Owner ask: *"rip it down right away — users expect books fast."* A book dropped
straight into a Drive author folder was invisible to both existing watchers
(one sees local disk, one sees a Firestore button) and waited up to 8 hours for
STEP 0b. This module watches the Drive Changes API instead.

⚠️ **Nothing here touches Drive, Firestore, the pipeline, or the network.** The
Drive client is a hand-built fake that records what was asked of it; the
``drive_pull.py`` subprocess and the Firestore enqueue are stubbed. The one
thing that IS real is the persisted state file, in tmp_path — because the page
token's advance-vs-hold behaviour is the whole safety property.

The properties pinned, in the order they matter:

  1. **A deferred or failed tick does NOT advance the page token.** This is the
     one that loses books if it regresses: advancing past changes we never
     acted on drops them permanently, silently, and the 8h self-heal is the
     only thing that would ever notice.
  2. A pull that finds nothing genuinely new queues NO run (our own STEP 4
     uploads raise change events too).
  3. The first tick baselines and fires nothing.
  4. Both kill switches stand it down.
"""

from __future__ import annotations

import json

import pytest

from app.core import pipeline_lock as pl
from app.tools import drive_poll as dp

FOLDER_MIME = "application/vnd.google-apps.folder"
PARENT = "parent-folder-id"
AUTHOR_FOLDER = "author-folder-id"
LIBRARY_IDS = {PARENT, AUTHOR_FOLDER}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeChanges:
    def __init__(self, pages, start_token="tok-0"):
        self._pages = list(pages)      # list of response dicts
        self._start = start_token
        self.list_calls: list[str] = []

    def getStartPageToken(self):  # noqa: N802 — mirrors the Google client
        return _Exec({"startPageToken": self._start})

    def list(self, **kw):
        self.list_calls.append(kw.get("pageToken"))
        return _Exec(self._pages.pop(0) if self._pages else {"newStartPageToken": "tok-end"})


class _Exec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _FakeService:
    def __init__(self, pages=(), start_token="tok-0"):
        self._changes = _FakeChanges(pages, start_token)

    def changes(self):
        return self._changes


def _change(name, parents=(AUTHOR_FOLDER,), *, removed=False, trashed=False, folder=False):
    return {
        "removed": removed,
        "fileId": f"id-{name}",
        "file": {
            "id": f"id-{name}", "name": name,
            "mimeType": FOLDER_MIME if folder else "audio/mp4",
            "trashed": trashed, "parents": list(parents),
        },
    }


@pytest.fixture
def poll(tmp_path, monkeypatch):
    """Isolate every seam: state file, tick lock, pipeline lock, env."""
    monkeypatch.setattr(dp, "STATE_PATH", tmp_path / "drive_poll_state.json")
    monkeypatch.setattr(dp, "TICK_LOCK_PATH", tmp_path / "drive_poll.lock")
    monkeypatch.setattr(dp, "NOTICE_PATH", tmp_path / "notice.txt")
    monkeypatch.setattr(pl, "LOCK_PATH", tmp_path / "pipeline.lock")
    monkeypatch.setattr(dp, "library_folder_ids", lambda service: set(LIBRARY_IDS))
    monkeypatch.delenv("DRIVE_POLL_ENABLED", raising=False)
    monkeypatch.delenv("DRIVE_PULL_ENABLED", raising=False)
    # last_poll defaults to 0, so the throttle never blocks a test's first tick.
    return tmp_path


def _wire(monkeypatch, service, *, pulled=0, rc=0):
    """Point the module at a fake Drive and record pull/enqueue calls."""
    calls: dict = {"pull": 0, "enqueued": []}
    monkeypatch.setattr(dp, "drive_service", lambda: service)

    def _pull():
        calls["pull"] += 1
        return rc, pulled

    monkeypatch.setattr(dp, "run_drive_pull", _pull)
    monkeypatch.setattr(dp, "enqueue_run_now", lambda reason: calls["enqueued"].append(reason) or True)
    return calls


def _state(poll_dir):
    return json.loads((dp.STATE_PATH).read_text(encoding="utf-8"))


def _seed_token(token="tok-start"):
    dp.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    dp._save_state({**dp._EMPTY_STATE, "page_token": token})


# ---------------------------------------------------------------------------
# The pure filter
# ---------------------------------------------------------------------------


def test_new_book_files_keeps_only_library_book_arrivals():
    changes = [
        _change("Real Book.m4b"),                              # keep
        _change("Novel.epub"),                                 # keep — all formats
        _change("Elsewhere.m4b", parents=("some-other-id",)),  # not in the library
        _change("Notes.txt"),                                  # not a book format
        _change("Author Name", folder=True),                   # a folder
        _change("Deleted.m4b", removed=True),                  # removed
        _change("Binned.m4b", trashed=True),                   # trashed
        _change("Copy of Real Book.m4b"),                      # plan_pull refuses copies
        _change("Real Book (1).m4b"),                          # ditto
    ]
    assert dp.new_book_files(changes, LIBRARY_IDS) == ["Real Book.m4b", "Novel.epub"]


def test_new_book_files_tolerates_a_change_with_no_file():
    """The Changes feed can carry an entry with no `file` (a permission change
    on something we cannot see). It must not raise."""
    assert dp.new_book_files([{"removed": False, "fileId": "x"}], LIBRARY_IDS) == []


def test_a_series_volume_is_not_mistaken_for_a_copy():
    """Rule 3 of app/core/drive_pull: a bare trailing number is a VOLUME. This
    reuses that module's is_copy_name rather than re-deciding here, and this
    test is what keeps the reuse honest."""
    assert dp.new_book_files([_change("Summoner 2.m4b")], LIBRARY_IDS) == ["Summoner 2.m4b"]


def test_list_changes_pages_to_exhaustion():
    """A machine that was off for a day has several pages waiting; stopping at
    the first would leave the rest unseen forever, because the next tick starts
    from the token we saved."""
    service = _FakeService(pages=[
        {"changes": [_change("A.m4b")], "nextPageToken": "p2"},
        {"changes": [_change("B.m4b")], "newStartPageToken": "tok-final"},
    ])
    changes, token = dp.list_changes(service, "tok-start")
    assert [c["file"]["name"] for c in changes] == ["A.m4b", "B.m4b"]
    assert token == "tok-final"
    assert service.changes().list_calls == ["tok-start", "p2"]


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------


def test_first_tick_baselines_and_fires_nothing(poll, monkeypatch):
    """The pre-existing state of Drive is not news — same rule as fs_watcher."""
    service = _FakeService(start_token="tok-baseline")
    calls = _wire(monkeypatch, service)

    assert dp.poll_once() == 0

    assert calls["pull"] == 0 and calls["enqueued"] == []
    assert _state(poll)["page_token"] == "tok-baseline"


def test_no_changes_does_nothing_but_advance(poll, monkeypatch):
    _seed_token()
    service = _FakeService(pages=[{"changes": [], "newStartPageToken": "tok-1"}])
    calls = _wire(monkeypatch, service)

    assert dp.poll_once() == 0

    assert calls["pull"] == 0 and calls["enqueued"] == []
    assert _state(poll)["page_token"] == "tok-1"


def test_a_new_m4b_pulls_and_queues_a_run(poll, monkeypatch):
    """The headline path: a book dropped straight into Drive is pulled and the
    pipeline is asked to run, minutes after it lands instead of hours."""
    _seed_token()
    service = _FakeService(pages=[
        {"changes": [_change("The Eye of the World.m4b")], "newStartPageToken": "tok-1"},
    ])
    calls = _wire(monkeypatch, service, pulled=1)

    assert dp.poll_once() == 0

    assert calls["pull"] == 1
    assert len(calls["enqueued"]) == 1
    reason = calls["enqueued"][0]
    assert "1 book(s) pulled" in reason
    assert "The Eye of the World.m4b" in reason, "the run's origin must be legible on the panel"
    assert _state(poll)["page_token"] == "tok-1"
    assert _state(poll)["last_pulled"] == 1


def test_changes_that_were_our_own_upload_queue_no_run(poll, monkeypatch):
    """STEP 4's uploads raise change events too. A change is a CANDIDATE; the
    pull is the authority. pulled == 0 means nothing was genuinely new, and
    waking the pipeline for it would fire a needless run every 8h cycle."""
    _seed_token()
    service = _FakeService(pages=[
        {"changes": [_change("Already Here.m4b")], "newStartPageToken": "tok-1"},
    ])
    calls = _wire(monkeypatch, service, pulled=0)

    assert dp.poll_once() == 0

    assert calls["pull"] == 1, "the pull still runs — it is what decides"
    assert calls["enqueued"] == [], "but nothing is new, so nothing is queued"
    assert _state(poll)["page_token"] == "tok-1"


def test_non_book_changes_advance_without_pulling(poll, monkeypatch):
    _seed_token()
    service = _FakeService(pages=[
        {"changes": [_change("Notes.txt"), _change("Folder", folder=True)],
         "newStartPageToken": "tok-1"},
    ])
    calls = _wire(monkeypatch, service)

    assert dp.poll_once() == 0

    assert calls["pull"] == 0 and calls["enqueued"] == []
    assert _state(poll)["page_token"] == "tok-1"


# ---------------------------------------------------------------------------
# ⚠️ The token-hold property — the one that loses books if it regresses
# ---------------------------------------------------------------------------


def test_lock_held_defers_and_does_not_advance_the_token(poll, monkeypatch, capsys):
    """DEFER, DON'T COLLIDE. A run in flight is mutating the library and may be
    mid-upload, so this tick must not pull. Critically it must ALSO not advance
    the page token: advancing past changes nobody acted on drops the drop this
    module exists to catch, and nothing would ever report it."""
    _seed_token("tok-start")
    pl.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    pl.LOCK_PATH.write_text("held by a run", encoding="utf-8")

    service = _FakeService(pages=[
        {"changes": [_change("Fresh Drop.m4b")], "newStartPageToken": "tok-1"},
    ])
    calls = _wire(monkeypatch, service, pulled=1)

    assert dp.poll_once() == 0

    assert calls["pull"] == 0, "must not pull while a run holds the lock"
    assert calls["enqueued"] == []
    assert _state(poll)["page_token"] == "tok-start", (
        "the token MUST stay put so the next tick re-sees these changes"
    )
    out = capsys.readouterr().out
    assert "DEFERRED" in out
    assert "token was NOT advanced" in out, "the deferral must be a NAMED line, not silence"


def test_a_failed_pull_does_not_advance_the_token(poll, monkeypatch):
    """Same property, second route in: a pull that exits nonzero has not dealt
    with the changes, so they must be re-seen."""
    _seed_token("tok-start")
    service = _FakeService(pages=[
        {"changes": [_change("Fresh Drop.m4b")], "newStartPageToken": "tok-1"},
    ])
    calls = _wire(monkeypatch, service, pulled=0, rc=2)

    assert dp.poll_once() == 0

    assert calls["pull"] == 1
    assert calls["enqueued"] == []
    assert _state(poll)["page_token"] == "tok-start"


def test_dry_run_reports_and_advances_nothing(poll, monkeypatch, capsys):
    _seed_token("tok-start")
    service = _FakeService(pages=[
        {"changes": [_change("Fresh Drop.m4b")], "newStartPageToken": "tok-1"},
    ])
    calls = _wire(monkeypatch, service, pulled=1)

    assert dp.poll_once(dry_run=True) == 0

    assert calls["pull"] == 0 and calls["enqueued"] == []
    assert _state(poll)["page_token"] == "tok-start"
    assert "DRY-RUN" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Standing it down
# ---------------------------------------------------------------------------


def test_kill_switch_stands_it_down(poll, monkeypatch):
    monkeypatch.setenv("DRIVE_POLL_ENABLED", "0")
    calls = _wire(monkeypatch, _FakeService())
    assert dp.poll_once() == 0
    assert calls["pull"] == 0
    assert not dp.STATE_PATH.exists(), "a disabled poll writes no state"


def test_drive_pull_kill_switch_also_stands_it_down(poll, monkeypatch):
    """STEP 0b's switch. Watching Drive for things to pull while pulling is off
    would fire a pull that does nothing, 96 times a day — and would let one
    switch silently override another."""
    monkeypatch.setenv("DRIVE_PULL_ENABLED", "0")
    calls = _wire(monkeypatch, _FakeService())
    assert dp.poll_once() == 0
    assert calls["pull"] == 0


def test_the_cadence_throttles_a_too_frequent_task(poll, monkeypatch):
    """A mis-registered 1-minute task must not hammer the Changes API — the
    documented 15-minute default is true even if the schedule says otherwise."""
    dp._save_state({**dp._EMPTY_STATE, "page_token": "tok-start", "last_poll": dp._now()})
    called = {"n": 0}

    def _svc():
        called["n"] += 1
        return _FakeService()

    monkeypatch.setattr(dp, "drive_service", _svc)
    assert dp.poll_once() == 0
    assert called["n"] == 0, "inside the interval the tick returns before touching Drive"


def test_a_throttled_tick_SAYS_it_was_throttled(poll, monkeypatch, capsys):
    """A tick that does nothing must say why.

    ⚠️ This was the ONE silent exit in the module. Every other do-nothing path
    already names itself — both kill switches, missing Drive auth, no changes,
    pulled 0 — and the self-throttle returned 0 with no output at all. Reading
    the log, "throttled, 11 minutes to go" was indistinguishable from "the Task
    Scheduler entry is not running", and those have completely different fixes.
    """
    dp._save_state({**dp._EMPTY_STATE, "page_token": "tok-start", "last_poll": dp._now()})
    monkeypatch.setattr(dp, "drive_service", lambda: _FakeService())

    assert dp.poll_once() == 0
    out = capsys.readouterr().out
    assert "throttled" in out
    # The two numbers a reader needs: the floor, and how long until the next
    # real tick. A bare "throttled" would not tell them whether to wait or fix.
    assert str(dp.POLL_MINUTES) in out
    assert "next in" in out


def test_the_throttle_line_speaks_ONCE_PER_WINDOW_and_not_every_tick(poll, monkeypatch, capsys):
    """⚠️ The rate limit is the whole reason this is safe to log at all.

    Task Scheduler runs this ~96 times a day. A line on every throttled tick
    would be the "unconfigured machine writes 96 identical lines" problem
    _notice() already exists to prevent — and the fix for that would be to make
    it silent again.
    """
    dp._save_state({**dp._EMPTY_STATE, "page_token": "tok-start", "last_poll": dp._now()})
    monkeypatch.setattr(dp, "drive_service", lambda: _FakeService())

    dp.poll_once()
    capsys.readouterr()                      # drop the first, expected line
    for _ in range(5):
        assert dp.poll_once() == 0
    assert "throttled" not in capsys.readouterr().out


def test_a_NEW_throttled_window_speaks_again(poll, monkeypatch, capsys):
    """Silence must not become permanent. The marker is keyed on `last_poll`,
    so the next real poll re-arms the line — otherwise a machine that throttled
    once would never mention it again."""
    dp._save_state({**dp._EMPTY_STATE, "page_token": "tok-start", "last_poll": dp._now()})
    monkeypatch.setattr(dp, "drive_service", lambda: _FakeService())
    dp.poll_once()
    capsys.readouterr()

    # A real poll happened: same shape the tick writes, a fresh `last_poll`.
    state = _state(poll)
    state["last_poll"] = dp._now() + 1
    dp._save_state(state)

    assert dp.poll_once() == 0
    assert "throttled" in capsys.readouterr().out


def test_the_throttle_line_NEVER_advances_the_page_token(poll, monkeypatch):
    """Property 1 of this file's docstring, applied to the new branch: the
    throttle now WRITES state, which it never did before, so the token has to
    be asserted intact. Advancing past changes nobody acted on drops books
    permanently and silently."""
    dp._save_state({**dp._EMPTY_STATE, "page_token": "tok-start", "last_poll": dp._now()})
    monkeypatch.setattr(dp, "drive_service", lambda: _FakeService())

    assert dp.poll_once() == 0
    assert _state(poll)["page_token"] == "tok-start"


def test_an_old_state_file_without_the_marker_still_works(poll, monkeypatch, capsys):
    """`throttle_logged_for` did not exist before 2026-08-26. A state file
    written by the old version must not crash the tick or stay silent."""
    dp.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    dp.STATE_PATH.write_text(
        json.dumps({"page_token": "tok-start", "last_poll": dp._now(), "last_pulled": 0}),
        encoding="utf-8",
    )
    monkeypatch.setattr(dp, "drive_service", lambda: _FakeService())

    assert dp.poll_once() == 0
    assert "throttled" in capsys.readouterr().out


def test_a_tick_already_in_flight_is_skipped(poll, monkeypatch):
    """A tick that pulls a 400 MB book can outlive the poll interval."""
    dp.TICK_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    dp.TICK_LOCK_PATH.write_text("1234", encoding="utf-8")
    calls = _wire(monkeypatch, _FakeService())
    assert dp.poll_once() == 0
    assert calls["pull"] == 0


def test_unreadable_state_re_baselines_rather_than_crashing(poll, monkeypatch):
    dp.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    dp.STATE_PATH.write_text("{not json", encoding="utf-8")
    service = _FakeService(start_token="tok-fresh")
    calls = _wire(monkeypatch, service)

    assert dp.poll_once() == 0

    assert calls["pull"] == 0, "a re-baseline must not fire on the pre-existing tree"
    assert _state(poll)["page_token"] == "tok-fresh"


def test_no_drive_auth_is_idle_not_a_crash(poll, monkeypatch):
    monkeypatch.setattr(dp, "drive_service", lambda: None)
    assert dp.poll_once() == 0
    assert not dp.STATE_PATH.exists()


# ---------------------------------------------------------------------------
# run_drive_pull's PULL_JSON parsing (subprocess stubbed)
# ---------------------------------------------------------------------------


def test_run_drive_pull_reads_the_pull_json_line(monkeypatch):
    class _Proc:
        returncode = 0
        stdout = 'noise\nPULL_JSON {"enforced": true, "pulled": 3, "toPull": 3}\nmore noise\n'
        stderr = ""

    monkeypatch.setattr(dp.subprocess, "run", lambda *a, **k: _Proc())
    assert dp.run_drive_pull() == (0, 3)


def test_run_drive_pull_treats_a_missing_or_bad_summary_as_zero(monkeypatch):
    """No PULL_JSON, or an unparseable one, must mean 'nothing pulled' — never
    an exception and never an optimistic guess that queues a pointless run."""
    class _NoJson:
        returncode = 0
        stdout = "Pulled 5 file(s).\n"
        stderr = ""

    class _BadJson:
        returncode = 0
        stdout = "PULL_JSON {oops\n"
        stderr = ""

    monkeypatch.setattr(dp.subprocess, "run", lambda *a, **k: _NoJson())
    assert dp.run_drive_pull() == (0, 0)
    monkeypatch.setattr(dp.subprocess, "run", lambda *a, **k: _BadJson())
    assert dp.run_drive_pull() == (0, 0)


def test_run_drive_pull_timeout_is_a_failure_not_a_hang(monkeypatch):
    def _boom(*a, **k):
        raise dp.subprocess.TimeoutExpired(cmd="drive_pull.py", timeout=1)

    monkeypatch.setattr(dp.subprocess, "run", _boom)
    rc, pulled = dp.run_drive_pull()
    assert rc != 0 and pulled == 0, "a timeout must hold the token, not advance it"


def test_enqueue_without_a_token_reports_and_returns_false(monkeypatch, tmp_path):
    """A pulled book with no way to request a run must say so, not pretend."""
    monkeypatch.setattr(dp, "NOTICE_PATH", tmp_path / "notice.txt")
    monkeypatch.setenv("PIPELINE_TRIGGER_TOKEN", "")
    assert dp.enqueue_run_now("2 books") is False
