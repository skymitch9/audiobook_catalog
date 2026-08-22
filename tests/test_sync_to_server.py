"""Tests for scripts/sync_to_server.py — the standalone "force full upload to
the shelf server" control (owner ask 2026-08-16). The shelf server does not
exist yet, so what matters most here is that every degrade path is HONEST
(never claims success it did not verify) and that the single-flight lock is
taken, exactly like every pipeline step.
"""

from __future__ import annotations

import socket
import sys

import pytest

from app.core import pipeline_lock as pl
from scripts import sync_to_server as s2s


class _FakePStatus:
    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name):
        def record(*a, **k):
            self.calls.append((name, a, k))

        return record

    def calls_named(self, name):
        return [c for c in self.calls if c[0] == name]


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "LOCK_PATH", tmp_path / "pipeline.lock")
    fake_status = _FakePStatus()
    monkeypatch.setattr(s2s, "pstatus", fake_status)
    # Never let a test that forgets to set SHELF_SERVER_* accidentally pick
    # up real values from the machine's actual .env.
    for var in ("SHELF_SERVER_HOST", "SHELF_SERVER_PATH", "SHELF_SERVER_USER", "SHELF_SERVER_SSH_PORT"):
        monkeypatch.delenv(var, raising=False)
    yield fake_status


# ---------------------------------------------------------------------------
# get_config()
# ---------------------------------------------------------------------------


def test_unconfigured_when_both_unset():
    is_configured, host, path, user, port = s2s.get_config()
    assert is_configured is False


def test_unconfigured_when_only_host_set(monkeypatch):
    monkeypatch.setenv("SHELF_SERVER_HOST", "100.64.0.1")
    is_configured, *_ = s2s.get_config()
    assert is_configured is False


def test_configured_when_host_and_path_set(monkeypatch):
    monkeypatch.setenv("SHELF_SERVER_HOST", "100.64.0.1")
    monkeypatch.setenv("SHELF_SERVER_PATH", "/srv/shelf/library")
    is_configured, host, path, user, port = s2s.get_config()
    assert is_configured is True
    assert host == "100.64.0.1"
    assert path == "/srv/shelf/library"
    assert port == 22  # default


def test_port_defaults_on_garbage_value(monkeypatch):
    monkeypatch.setenv("SHELF_SERVER_HOST", "h")
    monkeypatch.setenv("SHELF_SERVER_PATH", "/p")
    monkeypatch.setenv("SHELF_SERVER_SSH_PORT", "not-a-number")
    _, _, _, _, port = s2s.get_config()
    assert port == 22


# ---------------------------------------------------------------------------
# reachable() — a real socket call, tested against guaranteed-closed ports.
# ---------------------------------------------------------------------------


def test_reachable_false_for_a_port_nothing_listens_on():
    # Port 1 on loopback: privileged, essentially never bound in a test env.
    assert s2s.reachable("127.0.0.1", 1, timeout=0.5) is False


def test_reachable_true_when_something_is_listening():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert s2s.reachable("127.0.0.1", port, timeout=1.0) is True
    finally:
        srv.close()


# ---------------------------------------------------------------------------
# run() — the honest-degrade contract
# ---------------------------------------------------------------------------


def test_run_not_configured_reports_plainly_never_claims_success():
    result = s2s.run()
    assert result.ok is False
    assert result.configured is False
    assert result.reachable is None
    assert "not been built yet" in result.message


def test_run_configured_but_unreachable(monkeypatch):
    monkeypatch.setenv("SHELF_SERVER_HOST", "192.0.2.1")  # TEST-NET-1, never routable
    monkeypatch.setenv("SHELF_SERVER_PATH", "/srv/shelf/library")
    monkeypatch.setattr(s2s, "reachable", lambda host, port, timeout=5.0: False)
    result = s2s.run()
    assert result.ok is False
    assert result.configured is True
    assert result.reachable is False
    assert "not reachable" in result.message


def test_run_reachable_but_rclone_missing(monkeypatch):
    monkeypatch.setenv("SHELF_SERVER_HOST", "h")
    monkeypatch.setenv("SHELF_SERVER_PATH", "/p")
    monkeypatch.setattr(s2s, "reachable", lambda host, port, timeout=5.0: True)
    monkeypatch.setattr(s2s.shutil, "which", lambda name: None)
    result = s2s.run()
    assert result.ok is False
    assert result.configured is True
    assert result.reachable is True
    assert "rclone is not installed" in result.message


def test_run_success_path_invokes_rclone_sync(monkeypatch):
    monkeypatch.setenv("SHELF_SERVER_HOST", "h")
    monkeypatch.setenv("SHELF_SERVER_PATH", "/srv/shelf/library")
    monkeypatch.setenv("SHELF_SERVER_USER", "justin")
    monkeypatch.setattr(s2s, "reachable", lambda host, port, timeout=5.0: True)
    monkeypatch.setattr(s2s.shutil, "which", lambda name: "/usr/bin/rclone")

    captured = {}

    class _FakeProc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(cmd, capture_output, text):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(s2s.subprocess, "run", fake_run)
    result = s2s.run()

    assert result.ok is True
    assert captured["cmd"][0] == "rclone"
    # ⚠️ THE DEFAULT IS `copy`, AND THAT IS THE WHOLE SAFETY PROPERTY.
    # `copy` adds what the shelf is missing and never deletes; `sync` makes the
    # shelf match this PC exactly, which means a wrong ROOT_DIR wipes it. This
    # was `sync` by default until 2026-08-22 and had simply never been run.
    assert captured["cmd"][1] == "copy"
    assert "sftp,host=h,user=justin,port=22" in captured["cmd"][3]
    assert "/srv/shelf/library" in captured["cmd"][3]
    assert "--dry-run" not in captured["cmd"]


def test_run_dry_run_passes_dry_run_to_rclone(monkeypatch):
    monkeypatch.setenv("SHELF_SERVER_HOST", "h")
    monkeypatch.setenv("SHELF_SERVER_PATH", "/p")
    monkeypatch.setattr(s2s, "reachable", lambda host, port, timeout=5.0: True)
    monkeypatch.setattr(s2s.shutil, "which", lambda name: "/usr/bin/rclone")

    captured = {}

    class _FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, capture_output, text):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(s2s.subprocess, "run", fake_run)
    s2s.run(dry_run=True)
    assert "--dry-run" in captured["cmd"]


def test_run_rclone_failure_is_reported_not_swallowed(monkeypatch):
    monkeypatch.setenv("SHELF_SERVER_HOST", "h")
    monkeypatch.setenv("SHELF_SERVER_PATH", "/p")
    monkeypatch.setattr(s2s, "reachable", lambda host, port, timeout=5.0: True)
    monkeypatch.setattr(s2s.shutil, "which", lambda name: "/usr/bin/rclone")

    class _FakeProc:
        returncode = 7
        stdout = ""
        stderr = "connection refused"

    monkeypatch.setattr(s2s.subprocess, "run", lambda cmd, capture_output, text: _FakeProc())
    result = s2s.run()
    assert result.ok is False
    assert result.returncode == 7
    assert "connection refused" in result.message


# ---------------------------------------------------------------------------
# run_locked() — the single-flight lock, defense in depth
# ---------------------------------------------------------------------------


def test_run_locked_takes_and_releases_the_pipeline_lock(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        s2s, "run",
        lambda dry_run=False, mirror=False: (
            seen.update(held=pl.LOCK_PATH.exists())
        ) or s2s.ShelfUploadResult(True, True, True, "ok"),
    )
    s2s.run_locked()
    assert seen["held"] is True
    assert not pl.LOCK_PATH.exists()


def test_run_locked_blocked_reports_and_raises(monkeypatch, isolated_env):
    fake_status = isolated_env
    held = pl.acquire("manual")
    try:
        with pytest.raises(pl.PipelineLockHeld):
            s2s.run_locked()
        blocked = fake_status.calls_named("blocked_run")
        assert len(blocked) == 1
        assert blocked[0][1][0] == "manual-force-upload"
    finally:
        held.release()


def test_run_locked_publishes_result(monkeypatch, isolated_env):
    monkeypatch.setattr(s2s, "run", lambda dry_run=False, mirror=False: s2s.ShelfUploadResult(False, False, None, "not configured"))
    s2s.run_locked()
    published = isolated_env.calls_named("force_upload_result")
    assert len(published) == 1
    assert published[0][2] == {"ok": False, "configured": False, "reachable": None, "message": "not configured"}


# ---------------------------------------------------------------------------
# main() CLI
# ---------------------------------------------------------------------------


def test_mirror_mode_uses_sync_and_the_default_does_not(monkeypatch):
    """⚠️ The one difference that DELETES, pinned from BOTH sides.

    Asserting only that the default is `copy` proves nothing about whether
    `--mirror` still reaches `sync`: it could be wired to nothing and this
    suite would stay green while the owner believed mirror mode existed.
    """
    monkeypatch.setenv("SHELF_SERVER_HOST", "box.example")
    monkeypatch.setenv("SHELF_SERVER_PATH", "/srv/shelf/library")
    monkeypatch.setenv("SHELF_SERVER_USER", "justin")
    monkeypatch.setattr(s2s, "reachable", lambda host, port, timeout=5.0: True)
    monkeypatch.setattr(s2s.shutil, "which", lambda name: "/usr/bin/rclone")

    class _FakeProc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    seen = []

    def fake_run(cmd, capture_output, text):
        seen.append(cmd)
        return _FakeProc()

    monkeypatch.setattr(s2s.subprocess, "run", fake_run)

    safe = s2s.run(mirror=False)
    hard = s2s.run(mirror=True)

    assert seen[0][1] == "copy", "the default must never delete"
    assert seen[1][1] == "sync", "--mirror must actually reach rclone sync"

    # ⚠️ And the message must say which one ran. "Pushed ..." reads the same
    # for both, and the difference between them is whether files were destroyed.
    assert "nothing on the shelf was deleted" in safe.message.lower()
    assert "nothing on the shelf was deleted" not in hard.message.lower()


def test_main_exits_zero_on_success(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["sync_to_server.py"])
    monkeypatch.setattr(s2s, "run_locked", lambda dry_run=False, mirror=False: s2s.ShelfUploadResult(True, True, True, "pushed"))
    assert s2s.main() == 0
    assert "pushed" in capsys.readouterr().out


def test_main_exits_nonzero_on_not_configured(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["sync_to_server.py"])
    monkeypatch.setattr(s2s, "run_locked", lambda dry_run=False, mirror=False: s2s.ShelfUploadResult(False, False, None, "not configured"))
    assert s2s.main() == 1
    assert "not configured" in capsys.readouterr().out


def test_main_exits_nonzero_when_locked(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["sync_to_server.py"])

    def raise_locked(dry_run=False, mirror=False):
        raise pl.PipelineLockHeld(pl.LockHolder(pid=1, host="h", trigger="manual", started_at="2026-01-01T00:00:00+00:00"))

    monkeypatch.setattr(s2s, "run_locked", raise_locked)
    assert s2s.main() == 1
