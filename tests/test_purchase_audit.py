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

# The real extras runner, kept before the fixture stubs it out of tick tests.
_REAL_RUN_EXTRAS = pa._run_extras


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
    # The daily/weekly report-only extras contact Audible and PyPI; they have
    # their own tests below and are stubbed out of every tick test.
    monkeypatch.setattr(pa, "_run_extras", lambda state, outcome: None)
    monkeypatch.setattr(pa, "MISSING_REPORT_PATH", tmp_path / "missing_purchases_report.json")
    # Belt and braces: even a test that restores the real extras must not reach
    # Audible or PyPI. Tests that exercise an extra replace these themselves.
    from app.tools import audible_version_check as _avc
    from app.tools import missing_purchases_report as _mpr

    def _no_network(*a, **k):
        raise AssertionError("a purchase-audit test tried to reach the network")

    monkeypatch.setattr(_mpr, "collect", _no_network)
    monkeypatch.setattr(_avc, "check", _no_network)
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
    # ⚠️ "0 new" reads the same whether Audible answered or was never reached,
    # so the line names the list it diffed.
    assert "1042 items, fresh from audible-cli" in got.summary


def test_a_clean_looking_tick_off_the_container_list_says_which_list_it_was():
    lines = ["newest 50 purchases in C:\\...\\runtime\\openaudible\\books.json vs catalog: 0 missing"]
    assert "container's books.json" in pa._source_note(lines)


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


def test_classify_names_a_MISSING_audible_cli_apart_from_a_throttle():
    """⚠️ THE ONE THE FIRST LIVE TICK ACTUALLY HIT (2026-09-05 14:07).

    The .bat ran the .venv interpreter — following run_drive_poll.bat — and
    audible-cli 0.3.3 is installed in the Store Python 3.12 ONLY. The audit
    fell back to the container's books.json and exited 0 with "0 missing".

    Backing off 15 → 30 → 60 is the right answer to a throttle and a useless
    one here, so the two must not read the same in the log: an operator told
    "audible-cli said no" goes and looks at Audible, which is fine and
    innocent.
    """
    out = ("  [audible-cli] export failed for skylar: "
           "C:\\...\\.venv\\Scripts\\python.exe: No module named audible_cli\n"
           "newest 50 purchases in ...\\runtime\\openaudible\\books.json vs catalog: 0 missing\n"
           "RESULT: library is current - nothing to download.\n")
    got = pa.classify(0, out)
    assert got.ok is False
    assert "NOT INSTALLED" in got.summary
    assert "not a throttle" in got.summary
    assert "Audible was never" in got.summary, "it must say what the '0 missing' really meant"


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


def _foreign_lock(monkeypatch, *, alive: bool, hours_ago: float = 0.0) -> None:
    """A lock with a fake pid whose liveness the test controls (never a real
    pid of this machine). Liveness is now consulted — see
    pipeline_lock.lock_status() and the 2026-11-21 incident."""
    from datetime import datetime, timedelta, timezone

    started = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    pl.LOCK_PATH.write_text(json.dumps(
        {"pid": 999999, "host": "h", "trigger": "scheduled",
         "started_at": started.isoformat()}), encoding="utf-8")
    monkeypatch.setattr(pl, "pid_alive", lambda pid: alive and pid == 999999)
    # the fake holder is the same process that took the lock (no pid reuse)
    monkeypatch.setattr(pl, "process_start_time", lambda pid: started.timestamp() - 5)


def test_a_pipeline_run_in_flight_defers_and_changes_nothing(tick, monkeypatch, capsys):
    calls = _wire(monkeypatch, pa.TickOutcome(True, 0, 0, "0 new"))
    _seed(interval_minutes=15, last_attempt=0.0)
    _foreign_lock(monkeypatch, alive=True)

    assert pa.poll_once() == 0
    assert calls["audits"] == 0, "a deferred tick must not contact Audible"
    assert _state()["last_attempt"] == 0.0, "a deferral is not a completed tick"
    assert _state()["interval_minutes"] == 15, "a deferral is not a failure"
    assert _state()["consecutive_errors"] == 0
    assert "skipped-locked — a pipeline run is in flight (pid 999999" in capsys.readouterr().out


def test_a_dead_runs_leftover_lock_does_not_skip_the_audit(tick, monkeypatch, capsys):
    """⚠️ 2026-11-21: a reboot left the lock on disk and every 15-minute tick
    skipped for 7.5 hours. A dead holder is not a run in flight."""
    calls = _wire(monkeypatch, pa.TickOutcome(True, 0, 0, "0 new — library is current"))
    _seed(last_attempt=0.0)
    _foreign_lock(monkeypatch, alive=False)

    assert pa.poll_once() == 0
    assert calls["audits"] == 1
    out = capsys.readouterr().out
    assert "skipped-locked" not in out
    assert "pipeline lock is a leftover" in out and "pid 999999 is not running" in out
    assert pl.LOCK_PATH.exists(), "an observer never deletes the lock — acquire() reclaims it"


def test_a_live_holder_past_the_old_4h_ceiling_still_defers(tick, monkeypatch, capsys):
    """Review fix 2026-11-24: age alone no longer makes a LIVE run a leftover —
    auditing past a 5-hour-old live run is how two runs end up at once."""
    calls = _wire(monkeypatch, pa.TickOutcome(True, 0, 0, "0 new"))
    _seed(last_attempt=0.0)
    _foreign_lock(monkeypatch, alive=True, hours_ago=5)

    assert pa.poll_once() == 0
    assert calls["audits"] == 0
    assert "skipped-locked" in capsys.readouterr().out


def test_a_live_but_wedged_holder_past_the_safety_net_is_a_leftover(tick, monkeypatch, capsys):
    calls = _wire(monkeypatch, pa.TickOutcome(True, 0, 0, "0 new — library is current"))
    _seed(last_attempt=0.0)
    _foreign_lock(monkeypatch, alive=True, hours_ago=pl.WEDGED_LOCK_HOURS + 1)

    assert pa.poll_once() == 0
    assert calls["audits"] == 1
    assert "safety net" in capsys.readouterr().out


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


def test_a_scheduler_fire_exactly_one_interval_after_a_slow_tick_is_NOT_throttled(tick, monkeypatch):
    """⚠️ THE FIRST-DAY INCIDENT (2026-09-06). The 07:46 tick ran and took 18 s;
    the 08:01 fire measured 14.7 min and was throttled. The stamp was the END
    of the audit, so every second fire came up short by the audit's own length
    — half the promised cadence, and the log said "cadence floor" as if that
    were fine. The stamp is now the tick's START, and the floor carries a
    minute of slack for scheduler jitter."""
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(pa, "_now", lambda: clock["t"])

    def _slow_audit(download=True):
        clock["t"] += 18.0  # the audit itself takes 18 s
        return pa.TickOutcome(True, 0, 0, "0 new")

    monkeypatch.setattr(pa, "run_purchase_audit", _slow_audit)
    monkeypatch.setattr(pa.pipeline_requests, "request_run", lambda *a, **k: True)
    _seed(last_attempt=0.0)

    assert pa.poll_once() == 0
    assert _state()["last_attempt"] == 1_000_000.0, "stamped at the START, not the end"

    # The scheduler fires exactly BASE_MINUTES after the previous FIRE.
    clock["t"] = 1_000_000.0 + pa.BASE_MINUTES * 60
    assert pa.poll_once() == 0
    assert _state()["last_attempt"] == 1_000_000.0 + pa.BASE_MINUTES * 60, \
        "the second fire must run, not be throttled by its predecessor's duration"


def test_the_floor_still_holds_well_inside_the_interval(tick, monkeypatch):
    """Slack is a minute, not a licence: a fire 10 min after the last one is
    still throttled, so a mis-registered task cannot hammer Audible."""
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(pa, "_now", lambda: clock["t"])
    calls = _wire(monkeypatch, pa.TickOutcome(True, 0, 0, "0 new"))
    _seed(last_attempt=1_000_000.0 - 10 * 60)
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
    # ⚠️ Not sys.executable: the audit runs on the interpreter that HAS
    # audible-cli, which on this machine is not the one running the tests.
    assert seen["cmd"][0] == pa.AUDIT_PYTHON

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


# ---------------------------------------------------------------------------
# Daily full-library missing-purchases report (report only)
# ---------------------------------------------------------------------------

from app.tools import missing_purchases_report as mpr  # noqa: E402

_CLEAN = pa.TickOutcome(True, 0, 0, "0 new — library is current")


def _item(asin, title="T", date="2026-01-01", profile="skylar"):
    return {"date": date, "title": title, "asin": asin, "profile": profile}


def _wire_report(monkeypatch, items=None, *, ok=True, sent=True, error="boom"):
    """A canned full-library result and a recorded Discord."""
    calls = {"collect": 0, "posts": []}

    def _collect(top=mpr.FULL_TOP):
        calls["collect"] += 1
        calls["top"] = top
        if not ok:
            return {"ok": False, "error": error}
        return {"ok": True, "checked": 1077, "covered_all": True, "items": list(items or [])}

    monkeypatch.setattr(mpr, "collect", _collect)
    monkeypatch.setattr(pa, "_notify", lambda header, lines: calls["posts"].append((header, lines)) or sent)
    return calls


def test_the_first_report_writes_the_file_and_posts_once(tick, monkeypatch):
    calls = _wire_report(monkeypatch, [_item("B1", "Old Book"), _item("B2", "Older Book")])
    state = dict(pa._EMPTY_STATE)
    pa._daily_missing_report(state, _CLEAN)

    assert calls["collect"] == 1 and calls["top"] >= 5000
    report = json.loads(pa.MISSING_REPORT_PATH.read_text(encoding="utf-8"))
    assert [i["asin"] for i in report["items"]] == ["B1", "B2"]
    assert set(report["items"][0]) == {"date", "title", "asin", "profile"}
    assert report["date"] == state["missing_report_date"]
    assert len(calls["posts"]) == 1 and "2 purchase(s)" in calls["posts"][0][0]
    assert state["missing_report_posted"] == ["B1", "B2"]


def test_it_runs_at_most_once_per_calendar_day(tick, monkeypatch):
    calls = _wire_report(monkeypatch, [_item("B1")])
    state = dict(pa._EMPTY_STATE)
    pa._daily_missing_report(state, _CLEAN)
    pa._daily_missing_report(state, _CLEAN)
    assert calls["collect"] == 1


def test_an_UNCHANGED_standing_list_is_not_posted_again_the_next_day(tick, monkeypatch, capsys):
    calls = _wire_report(monkeypatch, [_item("B2"), _item("B1")])
    state = {**pa._EMPTY_STATE, "missing_report_date": "2000-01-01",
             "missing_report_posted": ["B1", "B2"]}
    pa._daily_missing_report(state, _CLEAN)

    assert calls["collect"] == 1
    assert calls["posts"] == []
    assert "unchanged" in capsys.readouterr().out
    assert state["missing_report_date"] != "2000-01-01"
    assert pa.MISSING_REPORT_PATH.exists(), "the local file is refreshed daily even when quiet"


def test_a_CHANGED_list_is_posted_and_the_new_title_is_marked(tick, monkeypatch):
    calls = _wire_report(monkeypatch, [_item("B1", "Standing"), _item("B9", "Fresh gap")])
    state = {**pa._EMPTY_STATE, "missing_report_date": "2000-01-01", "missing_report_posted": ["B1"]}
    pa._daily_missing_report(state, _CLEAN)

    header, lines = calls["posts"][0]
    assert "was 1" in header
    assert any("🆕" in ln and "Fresh gap" in ln for ln in lines)
    assert not any("🆕" in ln and "Standing" in ln for ln in lines)
    assert state["missing_report_posted"] == ["B1", "B9"]


def test_a_list_that_EMPTIED_says_so_once(tick, monkeypatch):
    calls = _wire_report(monkeypatch, [])
    state = {**pa._EMPTY_STATE, "missing_report_date": "2000-01-01", "missing_report_posted": ["B1"]}
    pa._daily_missing_report(state, _CLEAN)
    assert "every purchase is in the catalogue" in calls["posts"][0][0]
    assert state["missing_report_posted"] == []


def test_a_FAILED_post_is_not_recorded_so_it_is_said_next_time(tick, monkeypatch):
    _wire_report(monkeypatch, [_item("B1")], sent=False)
    state = dict(pa._EMPTY_STATE)
    pa._daily_missing_report(state, _CLEAN)
    assert state["missing_report_posted"] is None


def test_an_unposted_report_says_NEVER_POSTED_not_first_report(tick, monkeypatch, capsys):
    """While no webhook works, missing_report_posted stays None and every
    daily log said "changed (first report)" — wrong on both words."""
    _wire_report(monkeypatch, [_item("B1")], sent=False)
    state = {**pa._EMPTY_STATE, "missing_report_date": "2000-01-01"}
    pa._daily_missing_report(state, _CLEAN)
    out = capsys.readouterr().out
    assert "never posted before" in out
    assert "first report" not in out and "changed" not in out


def test_a_changed_list_still_says_changed_and_what_it_was(tick, monkeypatch, capsys):
    _wire_report(monkeypatch, [_item("B1"), _item("B2")])
    state = {**pa._EMPTY_STATE, "missing_report_date": "2000-01-01", "missing_report_posted": ["B1"]}
    pa._daily_missing_report(state, _CLEAN)
    assert "changed since last posted (was 1)" in capsys.readouterr().out


def test_verified_owned_titles_are_kept_in_the_report_file_not_posted(tick, monkeypatch, capsys):
    owned = {"date": "2024-05-01", "title": "Percy Jackson: The Demigod Files", "asin": "B002V1NINC",
             "profile": "skylar", "how": "owned_as", "file": "Rick Riordan\\Percy.m4b"}
    posts = []

    def _collect(top=mpr.FULL_TOP):
        return {"ok": True, "checked": 1077, "covered_all": True, "items": [_item("B1")],
                "verified_owned": [owned], "asin_index": "ASIN tag index: 1 ASINs from 1 files"}

    monkeypatch.setattr(mpr, "collect", _collect)
    monkeypatch.setattr(pa, "_notify", lambda header, lines: posts.append((header, lines)) or True)
    state = dict(pa._EMPTY_STATE)
    pa._daily_missing_report(state, _CLEAN)

    report = json.loads(pa.MISSING_REPORT_PATH.read_text(encoding="utf-8"))
    assert report["verified_owned"] == [owned]
    assert state["missing_report_posted"] == ["B1"]
    assert not any("Percy" in ln for ln in posts[0][1])
    assert "1 owned under another title, verified on Drive" in capsys.readouterr().out


def test_a_failed_ownership_check_is_a_failed_report_not_a_longer_list(tick, monkeypatch):
    calls = _wire_report(monkeypatch, ok=False,
                         error="ownership check unavailable: FileNotFoundError: upload manifest x not found")
    state = dict(pa._EMPTY_STATE)
    pa._daily_missing_report(state, _CLEAN)
    assert calls["posts"] == [] and not pa.MISSING_REPORT_PATH.exists()
    assert "ownership" in state["missing_report_error"]

def test_a_failed_report_writes_nothing_posts_nothing_and_waits_before_retrying(tick, monkeypatch, capsys):
    calls = _wire_report(monkeypatch, ok=False, error="no library rows exported for profile(s) paige")
    state = dict(pa._EMPTY_STATE)
    pa._daily_missing_report(state, _CLEAN)

    assert not pa.MISSING_REPORT_PATH.exists()
    assert calls["posts"] == []
    assert state["missing_report_date"] is None
    assert "paige" in state["missing_report_error"]
    assert "FAILED" in capsys.readouterr().out

    pa._daily_missing_report(state, _CLEAN)  # inside the retry window
    assert calls["collect"] == 1
    state["missing_report_attempt"] -= pa.MISSING_REPORT_RETRY_HOURS * 3600 + 1
    pa._daily_missing_report(state, _CLEAN)
    assert calls["collect"] == 2


def test_it_is_skipped_on_a_failing_tick_and_on_a_tick_that_downloaded(tick, monkeypatch):
    calls = _wire_report(monkeypatch, [_item("B1")])
    pa._daily_missing_report(dict(pa._EMPTY_STATE), pa.TickOutcome(False, 0, 0, "FAILED"))
    pa._daily_missing_report(dict(pa._EMPTY_STATE), pa.TickOutcome(True, 1, 0, "found 1", ("X",)))
    assert calls["collect"] == 0


def test_an_exploding_extra_never_fails_the_tick_or_moves_the_back_off(tick, monkeypatch, capsys):
    def _explode(state, outcome):
        raise RuntimeError("report blew up")

    monkeypatch.setattr(pa, "_run_extras", _REAL_RUN_EXTRAS)
    monkeypatch.setattr(pa, "_daily_missing_report", _explode)
    monkeypatch.setattr(pa, "_weekly_version_check", _explode)
    _wire(monkeypatch, _CLEAN)
    _seed(interval_minutes=30, consecutive_errors=1)

    assert pa.poll_once() == 0
    state = _state()
    assert state["interval_minutes"] == pa.BASE_MINUTES  # the clean tick still reset it
    assert state["consecutive_errors"] == 0
    assert "report blew up" in capsys.readouterr().out


def test_a_dry_run_never_runs_the_extras(tick, monkeypatch):
    seen = []
    monkeypatch.setattr(pa, "_run_extras", lambda state, outcome: seen.append(1))
    _wire(monkeypatch, _CLEAN)
    pa.poll_once(dry_run=True)
    assert seen == []


def test_a_real_tick_runs_the_extras_after_saving_its_own_state(tick, monkeypatch):
    seen = []

    def _extras(state, outcome):
        seen.append(_state()["last_result"])  # already on disk by now

    monkeypatch.setattr(pa, "_run_extras", _extras)
    _wire(monkeypatch, _CLEAN)
    pa.poll_once()
    assert seen == [_CLEAN.summary]


# ---------------------------------------------------------------------------
# Weekly PyPI version check (report only)
# ---------------------------------------------------------------------------

from app.tools import audible_version_check as avc  # noqa: E402


def _wire_versions(monkeypatch, result=None, *, sent=True, error=None):
    calls = {"checks": 0, "posts": []}

    def _check(fetch=None):
        calls["checks"] += 1
        if error:
            raise RuntimeError(error)
        return result

    monkeypatch.setattr(avc, "check", _check)
    monkeypatch.setattr(pa, "_notify", lambda header, lines: calls["posts"].append((header, lines)) or sent)
    return calls


_NEWER = {"audible": {"installed": "0.10.0", "latest": "0.10.0", "newer": False, "error": None},
          "audible-cli": {"installed": "0.3.3", "latest": "0.3.4", "newer": True, "error": None}}


def test_the_version_check_posts_a_new_release_once_and_logs_every_run(tick, monkeypatch, capsys):
    calls = _wire_versions(monkeypatch, _NEWER)
    state = dict(pa._EMPTY_STATE)
    pa._weekly_version_check(state, _CLEAN)
    assert len(calls["posts"]) == 1 and "0.3.4" in "\n".join(calls["posts"][0][1])
    assert state["version_reported"] == {"audible-cli": "0.3.4"}
    assert "0.3.3 -> 0.3.4 AVAILABLE" in capsys.readouterr().out

    state["version_check_at"] -= 8 * 86400  # next week, same release still out
    pa._weekly_version_check(state, _CLEAN)
    assert calls["checks"] == 2 and len(calls["posts"]) == 1
    assert "AVAILABLE" in capsys.readouterr().out, "it still LOGS every run"


def test_the_version_check_runs_at_most_once_a_week(tick, monkeypatch):
    calls = _wire_versions(monkeypatch, _NEWER)
    state = dict(pa._EMPTY_STATE)
    pa._weekly_version_check(state, _CLEAN)
    pa._weekly_version_check(state, _CLEAN)
    assert calls["checks"] == 1


def test_a_version_check_failure_is_logged_and_waits_for_next_week(tick, monkeypatch, capsys):
    calls = _wire_versions(monkeypatch, error="getaddrinfo failed")
    state = dict(pa._EMPTY_STATE)
    pa._weekly_version_check(state, _CLEAN)
    assert calls["posts"] == []
    assert state["version_check_at"] > 0
    assert "FAILED" in state["version_check_line"]
    assert "version check FAILED" in capsys.readouterr().out
    pa._weekly_version_check(state, _CLEAN)
    assert calls["checks"] == 1


def test_a_failed_version_post_is_not_recorded(tick, monkeypatch):
    _wire_versions(monkeypatch, _NEWER, sent=False)
    state = dict(pa._EMPTY_STATE)
    pa._weekly_version_check(state, _CLEAN)
    assert state["version_reported"] == {}


def test_the_new_state_fields_survive_a_save_and_load(tick):
    _seed(missing_report_posted=["B1"], version_reported={"audible": "1.0"}, version_check_at=5.0)
    got = pa._load_state()
    assert got["missing_report_posted"] == ["B1"]
    assert got["version_reported"] == {"audible": "1.0"}
    assert got["version_check_at"] == 5.0
