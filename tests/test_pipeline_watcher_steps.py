"""Tests for the fine-grained manual step / force-upload dispatch added to
app/tools/pipeline_watcher.py (owner ask 2026-08-16, catalog-platform
/status Operations section).

Covers:
  * PIPELINE_STEP_CHOICES stays in sync with scripts/sync_to_drive.py's
    STEP_CHOICES (the two are hardcoded separately by design — see that
    module's comment — so drift is caught here, not at runtime).
  * poll_once() discards a request naming an unknown step, exactly like a
    bad token or a stale timestamp.
  * poll_once() dispatches to the step-subprocess / force-upload-subprocess
    path instead of the full two-command pipeline when `step` is present.
  * The full-pipeline path (no `step` field) is completely unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.tools import pipeline_watcher as watcher
from scripts import sync_to_drive as sync


def test_pipeline_step_choices_matches_sync_to_drive():
    assert watcher.PIPELINE_STEP_CHOICES == set(sync.STEP_CHOICES)
    assert watcher.FORCE_UPLOAD_STEP not in sync.STEP_CHOICES


# ---------------------------------------------------------------------------
# poll_once() — fake Firestore harness
# ---------------------------------------------------------------------------


class _FakeDocRef:
    def __init__(self, store: dict, doc_id: str):
        self._store = store
        self._id = doc_id

    def delete(self):
        self._store.pop(self._id, None)


class _FakeDoc:
    def __init__(self, store: dict, doc_id: str, data: dict):
        self.id = doc_id
        self._data = data
        self.reference = _FakeDocRef(store, doc_id)

    def to_dict(self):
        return self._data


class _FakeQuery:
    def __init__(self, docs):
        self._docs = docs

    def limit(self, n):
        return self

    def stream(self):
        return list(self._docs)


class _FakeCollection:
    def __init__(self, store: dict):
        self._store = store

    def stream_docs(self):
        return [_FakeDoc(self._store, k, v) for k, v in list(self._store.items())]


class _FakeDB:
    def __init__(self, requests: dict[str, dict]):
        self._store = dict(requests)

    def collection(self, name):
        assert name == "pipeline_requests"
        col = _FakeCollection(self._store)
        return _FakeQuery(col.stream_docs())


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "LOCK_PATH", tmp_path / "watcher.lock")
    monkeypatch.setattr(watcher, "LAST_RUN_PATH", tmp_path / "last_run.txt")
    monkeypatch.setattr(watcher, "LOG_PATH", tmp_path / "pipeline_8h.log")
    monkeypatch.setattr(watcher, "_token", lambda: "a" * 32)
    yield


def _valid_request(step=None, **overrides):
    data = {
        "token": "a" * 32,
        "requestedAt": datetime.now(timezone.utc).isoformat(),
        "requestedBy": "estate-ops:owner@example.com",
    }
    if step is not None:
        data["step"] = step
    data.update(overrides)
    return data


def test_unknown_step_is_discarded_like_a_bad_token(monkeypatch):
    db = _FakeDB({"req1": _valid_request(step="not-a-real-step")})
    monkeypatch.setattr(watcher, "_client", lambda: db)
    monkeypatch.setattr(watcher, "_lock_held", lambda: False)
    monkeypatch.setattr(watcher, "_cooldown_remaining", lambda: 0.0)
    calls = []
    monkeypatch.setattr(watcher, "_run_pipeline", lambda: calls.append("full"))
    monkeypatch.setattr(watcher, "_run_pipeline_step", lambda step: calls.append(("step", step)))
    monkeypatch.setattr(watcher, "_run_force_upload", lambda: calls.append("force"))

    rc = watcher.poll_once()

    assert rc == 0
    assert calls == []  # discarded before ever running anything
    assert db._store == {}  # consumed (deleted) same as a bad token


def test_valid_step_request_dispatches_to_run_pipeline_step(monkeypatch):
    db = _FakeDB({"req1": _valid_request(step="upload")})
    monkeypatch.setattr(watcher, "_client", lambda: db)
    monkeypatch.setattr(watcher, "_lock_held", lambda: False)
    monkeypatch.setattr(watcher, "_cooldown_remaining", lambda: 0.0)
    calls = []
    monkeypatch.setattr(watcher, "_run_pipeline", lambda: calls.append("full"))
    monkeypatch.setattr(watcher, "_run_pipeline_step", lambda step: calls.append(("step", step)))
    monkeypatch.setattr(watcher, "_run_force_upload", lambda: calls.append("force"))

    rc = watcher.poll_once()

    assert rc == 0
    assert calls == [("step", "upload")]


def test_valid_force_upload_request_dispatches_to_run_force_upload(monkeypatch):
    db = _FakeDB({"req1": _valid_request(step=watcher.FORCE_UPLOAD_STEP)})
    monkeypatch.setattr(watcher, "_client", lambda: db)
    monkeypatch.setattr(watcher, "_lock_held", lambda: False)
    monkeypatch.setattr(watcher, "_cooldown_remaining", lambda: 0.0)
    calls = []
    monkeypatch.setattr(watcher, "_run_pipeline", lambda: calls.append("full"))
    monkeypatch.setattr(watcher, "_run_pipeline_step", lambda step: calls.append(("step", step)))
    monkeypatch.setattr(watcher, "_run_force_upload", lambda: calls.append("force"))

    rc = watcher.poll_once()

    assert rc == 0
    assert calls == ["force"]


def test_request_without_step_still_runs_the_full_pipeline_unchanged(monkeypatch):
    """Backward compatibility: the pre-existing admin-panel/estate-ops
    trigger never sends a `step` field, and must behave exactly as before."""
    db = _FakeDB({"req1": _valid_request()})
    monkeypatch.setattr(watcher, "_client", lambda: db)
    monkeypatch.setattr(watcher, "_lock_held", lambda: False)
    monkeypatch.setattr(watcher, "_cooldown_remaining", lambda: 0.0)
    calls = []
    monkeypatch.setattr(watcher, "_run_pipeline", lambda: calls.append("full"))
    monkeypatch.setattr(watcher, "_run_pipeline_step", lambda step: calls.append(("step", step)))
    monkeypatch.setattr(watcher, "_run_force_upload", lambda: calls.append("force"))

    rc = watcher.poll_once()

    assert rc == 0
    assert calls == ["full"]


def test_blank_step_string_treated_as_full_pipeline(monkeypatch):
    db = _FakeDB({"req1": _valid_request(step="")})
    monkeypatch.setattr(watcher, "_client", lambda: db)
    monkeypatch.setattr(watcher, "_lock_held", lambda: False)
    monkeypatch.setattr(watcher, "_cooldown_remaining", lambda: 0.0)
    calls = []
    monkeypatch.setattr(watcher, "_run_pipeline", lambda: calls.append("full"))
    monkeypatch.setattr(watcher, "_run_pipeline_step", lambda step: calls.append(("step", step)))
    monkeypatch.setattr(watcher, "_run_force_upload", lambda: calls.append("force"))

    rc = watcher.poll_once()
    assert calls == ["full"]


def test_lock_held_consumes_request_without_running_anything(monkeypatch):
    db = _FakeDB({"req1": _valid_request(step="sort")})
    monkeypatch.setattr(watcher, "_client", lambda: db)
    monkeypatch.setattr(watcher, "_lock_held", lambda: True)
    calls = []
    monkeypatch.setattr(watcher, "_run_pipeline_step", lambda step: calls.append(step))

    rc = watcher.poll_once()

    assert rc == 0
    assert calls == []
    assert db._store == {}  # still consumed — never retried


# ---------------------------------------------------------------------------
# _run_pipeline_step() / _run_force_upload() — subprocess argv + lock file
# ---------------------------------------------------------------------------


def test_run_pipeline_step_invokes_sync_to_drive_with_step_flag(monkeypatch, tmp_path):
    captured = {}

    def fake_call(cmd, cwd, env, stdout, stderr):
        captured["cmd"] = cmd
        captured["held_during_call"] = watcher.LOCK_PATH.exists()
        return 0

    monkeypatch.setattr(watcher.subprocess, "call", fake_call)
    rc = watcher._run_pipeline_step("catalog")

    assert rc == 0
    assert captured["held_during_call"] is True
    assert not watcher.LOCK_PATH.exists()  # released after
    cmd = captured["cmd"]
    assert cmd[-2:] == ["--step", "catalog"]
    assert "sync_to_drive.py" in cmd[1]


def test_run_force_upload_invokes_sync_to_server(monkeypatch):
    captured = {}

    def fake_call(cmd, cwd, env, stdout, stderr):
        captured["cmd"] = cmd
        return 3

    monkeypatch.setattr(watcher.subprocess, "call", fake_call)
    rc = watcher._run_force_upload()

    assert rc == 3
    cmd = captured["cmd"]
    assert "sync_to_server.py" in cmd[1]
    assert not watcher.LOCK_PATH.exists()
