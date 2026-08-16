"""Tests for the two additions to app/pipeline_status.py backing the
fine-grained manual pipeline controls (owner ask 2026-08-16, catalog-platform
/status Operations section):

  * start_step_run() — a standalone single-step run card, distinct from
    start_run()'s full 7-step pipeline scaffold.
  * force_upload_result() — the shelf-server force-upload's own status doc,
    deliberately separate from pipeline_status/current.

pipeline_status.py has no existing direct test file (every other pipeline
test stubs it out wholesale — see test_pipeline_single_flight_wiring.py's
_FakePStatus). These tests exercise the real module against a fake Firestore
client, the same _client()-monkeypatch seam the module's own docstring
describes ("Lazily build the Firestore client... Returns None if
unavailable").
"""

from __future__ import annotations

import pytest

from app import pipeline_status as pstatus


class _FakeDoc:
    def __init__(self, store: dict, key: tuple[str, str]):
        self._store = store
        self._key = key

    def set(self, payload):
        self._store[self._key] = payload


class _FakeCollection:
    def __init__(self, store: dict, name: str):
        self._store = store
        self._name = name

    def document(self, doc_id: str):
        return _FakeDoc(self._store, (self._name, doc_id))


class _FakeDB:
    def __init__(self):
        self.store: dict = {}

    def collection(self, name: str):
        return _FakeCollection(self.store, name)


@pytest.fixture(autouse=True)
def isolated_pstatus_state(monkeypatch):
    """Never let a test's write escape into the module's shared globals or
    (impossible here, since _client is stubbed, but for hygiene) reach a
    real Firestore project."""
    monkeypatch.setattr(pstatus, "_state", {})
    monkeypatch.setattr(pstatus, "_run_id", None)
    monkeypatch.setattr(pstatus, "_run_started", None)
    yield


def _fake_db(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(pstatus, "_client", lambda: db)
    monkeypatch.setattr(pstatus, "_last_write", 0.0)
    return db


# ---------------------------------------------------------------------------
# start_step_run()
# ---------------------------------------------------------------------------


def test_start_step_run_scaffolds_only_the_one_step(monkeypatch):
    db = _fake_db(monkeypatch)
    run_id = pstatus.start_step_run("upload", "Upload to Drive", "manual-step:upload")

    assert run_id  # a real id was minted
    doc = db.store[("pipeline_status", "current")]
    assert doc["state"] == "running"
    assert doc["trigger"] == "manual-step:upload"
    assert doc["stepKey"] == "upload"
    assert doc["stepLabel"] == "Upload to Drive"
    # The whole point: NOT the 7-entry STEPS scaffold — just this one, and
    # it starts already active (no separate step() call needed/possible).
    assert doc["steps"] == [{"key": "upload", "label": "Upload to Drive", "state": "active", "detail": ""}]


def test_start_step_run_then_finish_run_marks_only_that_step_done(monkeypatch):
    _fake_db(monkeypatch)
    pstatus.start_step_run("sort", "Sort books", "manual-step:sort")
    pstatus.step_detail("sort", "3 sorted, 1 companions filed")
    pstatus.set_summary(sorted=3)
    pstatus.finish_run("success")

    assert pstatus._state["state"] == "success"
    assert len(pstatus._state["steps"]) == 1
    assert pstatus._state["steps"][0] == {
        "key": "sort", "label": "Sort books", "state": "done",
        "detail": "3 sorted, 1 companions filed",
    }
    assert pstatus._state["summary"] == {"sorted": 3}
    # No OTHER step is ever mentioned — running 'sort' alone must never
    # claim 'audit'/'detect'/etc also ran.
    assert not any(k in str(pstatus._state) for k in ("Purchase audit", "Detect new books"))


def test_start_step_run_then_fail_run_marks_it_failed(monkeypatch):
    _fake_db(monkeypatch)
    pstatus.start_step_run("upload", "Upload to Drive", "manual-step:upload")
    try:
        raise RuntimeError("2 files failed")
    except RuntimeError as e:
        pstatus.fail_run(e)

    assert pstatus._state["state"] == "failed"
    assert "2 files failed" in pstatus._state["error"]
    assert pstatus._state["steps"][0]["state"] == "failed"


def test_start_step_run_writes_pipeline_runs_history_on_finish(monkeypatch):
    db = _fake_db(monkeypatch)
    run_id = pstatus.start_step_run("catalog", "Rebuild catalog", "manual-step:catalog")
    pstatus.finish_run("success")
    assert ("pipeline_runs", run_id) in db.store
    assert db.store[("pipeline_runs", run_id)]["trigger"] == "manual-step:catalog"


def test_start_step_run_never_raises_without_credentials(monkeypatch):
    monkeypatch.setattr(pstatus, "_client", lambda: None)
    # Must behave exactly like start_run(): the run id is minted locally
    # (never touches Firestore), and _push()'s own _client() check is what
    # makes the WRITE a no-op — so this must not raise, but a real id is
    # still returned, same contract start_run() already has.
    run_id = pstatus.start_step_run("audit", "Purchase audit", "manual-step:audit")
    assert run_id != ""


# ---------------------------------------------------------------------------
# force_upload_result()
# ---------------------------------------------------------------------------


def test_force_upload_result_writes_its_own_collection_not_pipeline_status(monkeypatch):
    db = _fake_db(monkeypatch)
    pstatus.force_upload_result(ok=True, configured=True, reachable=True, message="Pushed OK")

    assert ("shelf_upload_status", "current") in db.store
    assert ("pipeline_status", "current") not in db.store  # never touches the pipeline's own row
    doc = db.store[("shelf_upload_status", "current")]
    assert doc["ok"] is True
    assert doc["state"] == "success"
    assert doc["message"] == "Pushed OK"


@pytest.mark.parametrize(
    "ok,configured,reachable,expected_state",
    [
        (False, False, None, "not_configured"),
        (False, True, False, "unreachable"),
        (False, True, True, "failed"),
        (True, True, True, "success"),
    ],
)
def test_force_upload_result_state_classification(monkeypatch, ok, configured, reachable, expected_state):
    db = _fake_db(monkeypatch)
    pstatus.force_upload_result(ok=ok, configured=configured, reachable=reachable, message="x")
    assert db.store[("shelf_upload_status", "current")]["state"] == expected_state


def test_force_upload_result_never_raises_without_credentials(monkeypatch):
    monkeypatch.setattr(pstatus, "_client", lambda: None)
    # Must not raise — same "status backend outage never costs the caller"
    # contract as every other function here.
    pstatus.force_upload_result(ok=False, configured=False, reachable=None, message="not configured")


def test_force_upload_result_swallows_a_firestore_write_error(monkeypatch):
    class _ExplodingDB:
        def collection(self, name):
            raise RuntimeError("network down")

    monkeypatch.setattr(pstatus, "_client", lambda: _ExplodingDB())
    # Must not propagate — telemetry failures never break the caller.
    pstatus.force_upload_result(ok=True, configured=True, reachable=True, message="ok")
