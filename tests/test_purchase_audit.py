"""app/tools/purchase_audit.py — the 15-minute Audible purchase audit (2026-09-05).

Owner ask: purchases were discovered only by the 8-hourly pipeline's
acquisition stage, so a book bought at 08:05 could wait until 16:00 to be
found. He chose *"15 min with back-off"*.

⚠️ **Nothing here contacts Audible, Docker, Firestore or Task Scheduler.** The
``auto_acquire`` subprocess, the ``schtasks`` query and the run-request door are
all stubbed; the one thing that IS real is the persisted state file, in
tmp_path, because the back-off is a property of that file.

The properties pinned, in the order they matter:

  1. **A DEFERRED tick changes nothing** — not the interval, not
     ``last_attempt``, not the error count. A deferral is not a failure and not
     a completed tick; treating it as either would either back off for a reason
     Audible never gave, or burn the window a real tick was owed.
  2. **The two single-flight checks are both live**, and the task-status one is
     the load-bearing one: the 8h run's acquisition stage takes NO lock, so the
     pipeline lock reads "free" during exactly the window this tick must not
     enter.
  3. **A download queues a pipeline run.** The file lands in the container
     books dir, which no watcher watches — without the request it waits ~8 h.
  4. **An ``[audible-cli] export failed`` line backs off even on exit code 0**,
     because the audit silently falls back to a stale list in that case.
  5. Back-off walks 15 → 30 → 60 and caps; a clean tick resets it.
  6. ``--dry-run`` downloads nothing, queues nothing and does not move the
     back-off.
"""

from __future__ import annotations

import json

import pytest

from app.core import pipeline_lock as pl
from app.tools import purchase_audit as pa


@pytest.fixture
def tick(tmp_path, monkeypatch):
    """Isolate every seam: state file, tick lock, notice, pipeline lock, env."""
    monkeypatch.setattr(pa, "STATE_PATH", tmp_path / "purchase_audit_state.json")
    monkeypatch.setattr(pa, "TICK_LOCK_PATH", tmp_path / "purchase_audit.lock")
    monkeypatch.setattr(pa, "NOTICE_PATH", tmp_path / "notice.txt")
    monkeypatch.setattr(pl, "LOCK_PATH", tmp_path / "pipeline.lock")
    monkeypatch.delenv("PURCHASE_AUDIT_ENABLED", raising=False)
    # Default: the 8h task is idle. Individual tests override.
    monkeypatch.setattr(pa, "sync_task_running", lambda: False)
    return tmp_path


def _wire(monkeypatch, outcome: pa.TickOutcome):
    """Point the module at a canned audit result and record run requests."""
    calls: dict = {"audits": 0, "download_arg": [], "requested": []}

    def _audit(download=True):
        calls["audits"] += 1
        calls["download_arg"].append(download)
        return outcome

    monkeypatch.setattr(pa, "run_purchase_audit", _audit)
    monkeypatch.setattr(
        pa.pipeline_requests, "request_run",
        lambda reason, **kw: calls["requested"].append((reason, kw.get("source"))) or True,
    )
    return calls


def _state() -> dict:
    return json.loads(pa.STATE_PATH.read_text(encoding="utf-8"))


def _seed(**kw) -> None:
    pa.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    pa._save_state({**pa._EMPTY_STATE, **kw})


# ---------------------------------------------------------------------------
# classify() — the pure reader of auto_acquire's output
# ---------------------------------------------------------------------------


def test_classify_clean_run_is_ok_and_says_nothing_was_new():
    out = ("[1/3] fetching fresh library lists via audible-cli...\n"
           "  1042 items across profiles - container not needed\n"
           "newest 50 purchases in audible-cli exports vs catalog: 0 missing\n"
           "RESULT: library is current - nothing to download.\n")
    got = pa.classify(0, out)
    assert got.ok and got.downloaded == 0 and got.failed == 0
    assert "0 new" in got.summary


def test_classify_counts_downloads_and_keeps_the_titles():
    out = ("DOWNLOADED: The Way of Kings -> the-way-of-kings.m4b\n"
           "DOWNLOADED: Rhythm of War -> rhythm-of-war.m4b\n"
           "RESULT: 2 downloaded, 0 failed - the sync step ingests downloads.\n")
    got = pa.classify(0, out)
    assert got.ok and got.downloaded == 2 and got.failed == 0
    assert got.titles[0].startswith("The Way of Kings")
    assert "2 new purchase(s)" in got.summary


def test_classify_treats_an_audible_cli_export_failure_as_a_FAILING_tick_on_exit_0():
    """⚠️ THE ONE THE EXIT CODE DOES NOT CARRY.

    audit_new_purchases falls back to the container's books.json when every
    profile's export fails, so the run exits 0 having audited a list that may
    be days old. "0 missing" then means "we could not ask Audible" — which is
    the throttle/expired-auth condition back-off exists for.
    """
    out = ("[1/3] fetching fresh library lists via audible-cli...\n"
           "  [audible-cli] export failed for skylar: 429 Too Many Requests\n"
           "newest 50 purchases in ...books.json vs catalog: 0 missing\n"
           "RESULT: library is current - nothing to download.\n")
    got = pa.classify(0, out)
    assert got.ok is False
    assert "audible-cli" in got.summary and "stale" in got.summary


def test_classify_treats_a_failed_download_as_failing():
    out = ("DOWNLOADED: Book One -> one.m4b\n"
           "FAILED: Book Two: activation bytes not found for samantha\n"
           "RESULT: 1 downloaded, 1 failed\n")
    got = pa.classify(1, out)
    assert got.ok is False and got.downloaded == 1 and got.failed == 1


def test_classify_treats_no_source_and_a_timeout_as_failing():
    assert pa.classify(2, "no audible-cli profiles and no books.json").ok is False
    assert pa.classify(None, "").ok is False
    assert "timed out" in pa.classify(None, "").summary


# ---------------------------------------------------------------------------
# Single-flight — both checks
# ---------------------------------------------------------------------------


def test_a_pipeline_run_in_flight_defers_and_changes_nothing(tick, monkeypatch):
    calls = _wire(monkeypatch, pa.TickOutcome(True, 0, 0, "0 new"))
    _seed(interval_minutes=15, last_attempt=0.0)
    pl.LOCK_PATH.write_text(json.dumps(
        {"pid": 999999, "host": "h", "trigger": "scheduled",
         "started_at": "2026-09-05T20:00:00+00:00"}), encoding="utf-8")

    assert pa.poll_once() == 0
    assert calls["audits"] == 0, "a deferred tick must not contact Audible"
    assert _state()["last_attempt"] == 0.0, "a deferral is not a completed tick"
    assert _state()["interval_minutes"] == 15, "a deferral is not a failure"
    assert _state()["consecutive_errors"] == 0


def test_the_8h_task_running_defers_even_though_the_pipeline_lock_is_free(tick, monkeypatch):
    """⚠️ THE LOAD-BEARING CHECK.

    sync_pipeline_8h.bat runs auto_acquire FIRST and sync_to_drive.py second;
    only the second takes app/core/pipeline_lock.py. So during the acquisition
    stage — the exact command this tick runs — the lock reads free. Without
    this check two audible-cli downloads of the same ASIN can race into the
    same directory.
    """
    calls = _wire(monkeypatch, pa.TickOutcome(True, 0, 0, "0 new"))
    monkeypatch.setattr(pa, "sync_task_running", lambda: True)
    _seed(interval_minutes=15, last_attempt=0.0)
    assert not pl.LOCK_PATH.exists()

    assert pa.poll_once() == 0
    assert calls["audits"] == 0
    assert _state()["last_attempt"] == 0.0


def test_an_unreadable_task_status_still_audits_and_says_so(tick, monkeypatch, capsys):
    """Unknown is not treated as running: failing closed would mean a machine
    whose schtasks answers oddly silently stops auditing forever."""
    calls = _wire(monkeypatch, pa.TickOutcome(True, 0, 0, "0 new — library is current"))
    monkeypatch.setattr(pa, "sync_task_running", lambda: None)
    _seed(last_attempt=0.0)

    assert pa.poll_once() == 0
    assert calls["audits"] == 1
    assert "could not read" in capsys.readouterr().out


def test_a_tick_lock_stops_a_second_tick(tick, monkeypatch):
    calls = _wire(monkeypatch, pa.TickOutcome(True, 0, 0, "0 new"))
    pa.TICK_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    pa.TICK_LOCK_PATH.write_text("1234", encoding="utf-8")

    assert pa.poll_once() == 0
    assert calls["audits"] == 0


# ---------------------------------------------------------------------------
# The hand-off
# ---------------------------------------------------------------------------


def test_a_download_queues_a_pipeline_run(tick, monkeypatch):
    """⚠️ audible_download.DEFAULT_OUT is <repo>/runtime/openaudible/books and
    AudiobookFsWatcher watches ROOT_DIR — different directories (measured
    2026-09-05). Only sync_to_drive.py's sort step reads the container dir, so
    without this request the download waits for the next 8-hourly run."""
    calls = _wire(monkeypatch, pa.TickOutcome(True, 1, 0, "found 1", ("Wind and Truth",)))
    _seed(last_attempt=0.0)

    assert pa.poll_once() == 0
    assert len(calls["requested"]) == 1
    reason, source = calls["requested"][0]
    assert source == "purchase-audit"
    assert "Wind and Truth" in reason
    assert _state()["last_downloaded"] == 1 and _state()["total_downloaded"] == 1


def test_a_quiet_tick_queues_nothing(tick, monkeypatch):
    calls = _wire(monkeypatch, pa.TickOutcome(True, 0, 0, "0 new — library is current"))
    _seed(last_attempt=0.0)

    assert pa.poll_once() == 0
    assert calls["requested"] == [], "a run must not be queued when nothing arrived"


# ---------------------------------------------------------------------------
# Back-off
# ---------------------------------------------------------------------------


def test_back_off_doubles_and_caps():
    assert pa.next_interval(15) == 30
    assert pa.next_interval(30) == 60
    assert pa.next_interval(60) == 60


def test_a_failing_tick_backs_off_and_a_clean_one_resets(tick, monkeypatch):
    _seed(last_attempt=0.0)
    _wire(monkeypatch, pa.TickOutcome(False, 0, 0, "FAILED — audible-cli said no"))
    assert pa.poll_once() == 0
    assert _state()["interval_minutes"] == 30
    assert _state()["consecutive_errors"] == 1

    _seed(interval_minutes=30, consecutive_errors=1, last_attempt=0.0)
    _wire(monkeypatch, pa.TickOutcome(False, 0, 0, "FAILED again"))
    assert pa.poll_once() == 0
    assert _state()["interval_minutes"] == 60
    assert _state()["consecutive_errors"] == 2

    _seed(interval_minutes=60, consecutive_errors=2, last_attempt=0.0)
    _wire(monkeypatch, pa.TickOutcome(True, 0, 0, "0 new — library is current"))
    assert pa.poll_once() == 0
    assert _state()["interval_minutes"] == 15, "a clean tick resets the cadence"
    assert _state()["consecutive_errors"] == 0
    assert _state()["last_error"] is None


def test_the_backed_off_interval_is_honoured_before_the_next_audit(tick, monkeypatch):
    """A backed-off tick must not contact Audible again on the next 15-minute
    fire — the Task Scheduler entry stays flat and the module holds the line."""
    calls = _wire(monkeypatch, pa.TickOutcome(True, 0, 0, "0 new"))
    _seed(interval_minutes=60, last_attempt=pa._now() - 20 * 60)  # 20 min ago

    assert pa.poll_once() == 0
    assert calls["audits"] == 0


def test_a_corrupt_interval_can_never_produce_a_zero_minute_cadence(tick):
    for bad in (0, -5, None, "banana", 99999):
        assert pa.BASE_MINUTES <= pa.interval_minutes({"interval_minutes": bad}) <= pa.MAX_MINUTES


def test_an_unreadable_state_file_starts_fresh_rather_than_raising(tick):
    pa.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    pa.STATE_PATH.write_text("{not json", encoding="utf-8")
    assert pa._load_state()["interval_minutes"] == pa.BASE_MINUTES


# ---------------------------------------------------------------------------
# Kill switch and dry run
# ---------------------------------------------------------------------------


def test_the_kill_switch_stands_it_down(tick, monkeypatch):
    calls = _wire(monkeypatch, pa.TickOutcome(True, 0, 0, "0 new"))
    monkeypatch.setenv("PURCHASE_AUDIT_ENABLED", "0")
    assert pa.poll_once() == 0
    assert calls["audits"] == 0


def test_dry_run_reports_downloads_nothing_and_leaves_the_back_off_alone(tick, monkeypatch):
    calls = _wire(monkeypatch, pa.TickOutcome(True, 0, 0, "2 new purchase(s) NOT downloaded"))
    _seed(interval_minutes=60, consecutive_errors=3, last_attempt=0.0)

    assert pa.poll_once(dry_run=True) == 0
    assert calls["download_arg"] == [False], "a dry run must pass --no-download"
    assert calls["requested"] == []
    assert _state()["interval_minutes"] == 60, "a dry run must not reset a back-off"
    assert _state()["consecutive_errors"] == 3
    assert _state()["last_attempt"] == 0.0


def test_run_purchase_audit_passes_the_8h_pipelines_own_flags(monkeypatch):
    """One canonical audit: the same command sync_pipeline_8h.bat runs."""
    seen: dict = {}

    class _Proc:
        returncode = 0
        stdout = "RESULT: library is current - nothing to download.\n"
        stderr = ""

    def _run(cmd, **kw):
        seen["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(pa.subprocess, "run", _run)
    got = pa.run_purchase_audit()
    assert got.ok
    assert seen["cmd"][1:] == ["-m", "app.tools.auto_acquire", "--notify", "--stop-after"]

    pa.run_purchase_audit(download=False)
    assert seen["cmd"][-1] == "--no-download"


def test_a_subprocess_timeout_is_a_failing_tick_not_a_crash(monkeypatch):
    def _boom(cmd, **kw):
        raise pa.subprocess.TimeoutExpired(cmd="auto_acquire", timeout=1)

    monkeypatch.setattr(pa.subprocess, "run", _boom)
    got = pa.run_purchase_audit()
    assert got.ok is False and "timed out" in got.summary


# ---------------------------------------------------------------------------
# The task-status read itself
# ---------------------------------------------------------------------------


def test_sync_task_running_parses_schtasks_and_never_writes(monkeypatch):
    """⚠️ Read-only, always. Nothing in this module may change, start or stop a
    scheduled task — this is the live pipeline machine."""
    seen: dict = {}

    class _Proc:
        returncode = 0
        stdout = "TaskName: \\AudiobookSyncPipeline\nStatus:  Running\nLast Result: 0\n"

    def _run(cmd, **kw):
        seen["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(pa.subprocess, "run", _run)
    assert pa.sync_task_running() is True
    assert seen["cmd"][1] == "/query"
    assert not any(a.lower() in ("/create", "/change", "/delete", "/run", "/end")
                   for a in seen["cmd"]), "this module may only QUERY Task Scheduler"


def test_sync_task_running_is_unknown_when_schtasks_fails(monkeypatch):
    def _boom(cmd, **kw):
        raise OSError("schtasks not found")

    monkeypatch.setattr(pa.subprocess, "run", _boom)
    assert pa.sync_task_running() is None
