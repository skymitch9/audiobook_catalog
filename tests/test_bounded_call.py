"""app/core/bounded_call.py — a call that never returns must not wedge a tick.

Incident 2026-10-19: Firestore DNS failures began 12:31, one call inside a
watcher tick never returned, and because the ``.bat`` appends with ``>>`` and
Task Scheduler refuses a second instance, ONE hung tick blocked every later
tick for hours.

These tests use REAL child processes (``IN_CHILD = True``) running functions
defined in this file, none of which touch the network. ``_hang`` is the call
that never returns: it records its pid and sleeps for an hour. The properties:

  1. the caller gets control back at the deadline, not when the call ends;
  2. the child is actually DEAD afterwards — abandoned, not left running;
  3. a normal call returns its value and relays what the child printed;
  4. a crash is a failure with a reason, never an exception in the caller;
  5. the watchers' own ticks (drive_poll, pipeline_requests) return when their
     network section hangs.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from app.core import bounded_call
from app.core import pipeline_lock as pl


def _hang(pid_path, *_rest):
    """The call that never returns. Writes its pid first so the test can
    prove the process is gone afterwards."""
    Path(pid_path).write_text(str(os.getpid()), encoding="utf-8")
    time.sleep(3600)
    return "unreachable"


def _echo(value):
    print(f"child says {value}")
    return {"got": value}


def _boom():
    raise RuntimeError("kaboom")


@pytest.fixture
def real_child(monkeypatch):
    monkeypatch.setattr(bounded_call, "IN_CHILD", True)
    monkeypatch.setattr(bounded_call, "REAP_WAIT_S", 5)


def _child_pid(pid_path: Path, deadline_s: float = 15.0) -> int:
    end = time.time() + deadline_s
    while time.time() < end:
        if pid_path.exists() and pid_path.read_text(encoding="utf-8").strip():
            return int(pid_path.read_text(encoding="utf-8"))
        time.sleep(0.1)
    raise AssertionError("the child never started")


def test_a_call_that_never_returns_is_abandoned_at_the_deadline(real_child, tmp_path):
    pid_path = tmp_path / "pid.txt"
    started = time.monotonic()
    got = bounded_call.call(_hang, str(pid_path), timeout=4)
    elapsed = time.monotonic() - started

    assert got.ok is False and got.timed_out is True
    assert "abandoned" in got.error
    assert elapsed < 4 + bounded_call.REAP_WAIT_S + 5
    assert pl.pid_alive(_child_pid(pid_path)) is False


def test_a_normal_call_returns_its_value_and_relays_the_childs_output(real_child, capsys):
    got = bounded_call.call(_echo, "hello", timeout=60)
    assert got.ok is True and got.value == {"got": "hello"}
    assert "child says hello" in capsys.readouterr().out


def test_a_crash_is_a_failure_with_a_reason_not_an_exception(real_child):
    got = bounded_call.call(_boom, timeout=60, relay=False)
    assert got.ok is False and got.timed_out is False
    assert "kaboom" in got.error


def test_in_process_mode_round_trips_json_so_the_modes_cannot_drift():
    # conftest.py leaves IN_CHILD False for every test.
    got = bounded_call.call(_echo, "x", timeout=1)
    assert got.ok and got.value == {"got": "x"}
    assert bounded_call.call(_boom, timeout=1).ok is False


def test_a_function_defined_under_dash_m_is_imported_by_its_real_name(monkeypatch):
    """Every watcher runs as `python -m app.tools.<name>`, where its own
    functions report __module__ == '__main__'."""
    import sys
    import types

    fake_main = types.ModuleType("__main__")
    fake_main.__spec__ = types.SimpleNamespace(name="app.tools.pipeline_watcher")
    monkeypatch.setitem(sys.modules, "__main__", fake_main)

    def f():
        return None

    f.__module__ = "__main__"
    assert bounded_call._module_name(f) == "app.tools.pipeline_watcher"


# ---------------------------------------------------------------------------
# The ticks themselves
# ---------------------------------------------------------------------------


def test_drive_poll_tick_returns_when_its_drive_probe_never_answers(real_child, tmp_path, monkeypatch):
    from app.tools import drive_poll as dp

    monkeypatch.setattr(dp, "STATE_PATH", tmp_path / "drive_poll_state.json")
    monkeypatch.setattr(dp, "TICK_LOCK_PATH", tmp_path / "drive_poll.lock")
    monkeypatch.setattr(dp, "NOTICE_PATH", tmp_path / "notice.txt")
    monkeypatch.setattr(pl, "LOCK_PATH", tmp_path / "pipeline.lock")
    monkeypatch.delenv("DRIVE_POLL_ENABLED", raising=False)
    monkeypatch.delenv("DRIVE_PULL_ENABLED", raising=False)
    monkeypatch.setattr(dp, "PROBE_TIMEOUT_S", 4)
    monkeypatch.setattr(dp, "probe_drive", _hang)
    pulls = []
    monkeypatch.setattr(dp, "run_drive_pull", lambda: pulls.append(1) or (0, 0))

    # The tick hands its page token to the probe; here the "token" is where
    # the hanging stand-in writes its pid.
    pid_path = tmp_path / "pid.txt"
    dp._save_state({**dp._EMPTY_STATE, "page_token": str(pid_path)})

    started = time.monotonic()
    assert dp.poll_once() == 0
    assert time.monotonic() - started < 4 + bounded_call.REAP_WAIT_S + 5

    state = json.loads(dp.STATE_PATH.read_text(encoding="utf-8"))
    assert state["page_token"] == str(pid_path), "a failed probe must NOT advance the token"
    assert state["last_poll"] > 0
    assert pulls == []
    assert not dp.TICK_LOCK_PATH.exists()
    assert pl.pid_alive(_child_pid(pid_path)) is False


def test_a_run_request_whose_firestore_write_never_answers_returns_false(real_child, tmp_path, monkeypatch):
    from app.core import pipeline_requests

    monkeypatch.setenv("PIPELINE_TRIGGER_TOKEN", "t" * 32)
    monkeypatch.setattr(pipeline_requests, "REQUEST_TIMEOUT_S", 4)
    monkeypatch.setattr(pipeline_requests, "add_request", _hang)
    lines = []
    pid_path = tmp_path / "pid.txt"

    started = time.monotonic()
    ok = pipeline_requests.request_run(str(pid_path), source="test", log=lines.append)
    assert ok is False
    assert time.monotonic() - started < 4 + bounded_call.REAP_WAIT_S + 5
    assert any("could not enqueue" in ln and "abandoned" in ln for ln in lines)
    assert pl.pid_alive(_child_pid(pid_path)) is False


def test_request_run_reports_missing_firestore_credentials_once(monkeypatch):
    from app.core import pipeline_requests

    monkeypatch.setenv("PIPELINE_TRIGGER_TOKEN", "t" * 32)
    monkeypatch.setattr(pipeline_requests, "add_request", lambda reason, source: "no-credentials")
    notices, lines = [], []
    assert pipeline_requests.request_run("r", source="s", log=lines.append, notice=notices.append) is False
    assert notices and "no Firestore credentials" in notices[0]
    assert lines == []
