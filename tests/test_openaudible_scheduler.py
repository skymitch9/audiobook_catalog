import json
import os
import time
from pathlib import Path

import pytest


def test_queue_quick_sync_writes_atomic_command(tmp_path, monkeypatch):
    from scripts import openaudible_scheduler as scheduler

    status_path = tmp_path / "status.json"
    command_path = tmp_path / "commands.json"
    status_path.write_text(json.dumps({"quit": False, "queues": {}}), encoding="utf-8")
    monkeypatch.setattr(scheduler, "STATUS_PATH", status_path)
    monkeypatch.setattr(scheduler, "COMMAND_PATH", command_path)

    assert scheduler.queue_quick_sync() is True
    assert json.loads(command_path.read_text(encoding="utf-8")) == ["Sync_Quick"]
    assert not command_path.with_suffix(".json.tmp").exists()


@pytest.mark.parametrize(
    "status",
    [
        {"quit": True, "queues": {}},
        {"quit": False, "queues": {"Downloading": ["book"]}},
    ],
)
def test_queue_quick_sync_waits_when_not_ready(tmp_path, monkeypatch, status):
    from scripts import openaudible_scheduler as scheduler

    status_path = tmp_path / "status.json"
    command_path = tmp_path / "commands.json"
    status_path.write_text(json.dumps(status), encoding="utf-8")
    monkeypatch.setattr(scheduler, "STATUS_PATH", status_path)
    monkeypatch.setattr(scheduler, "COMMAND_PATH", command_path)

    assert scheduler.queue_quick_sync() is False
    assert not command_path.exists()


def test_detect_new_books_skips_recent_files(tmp_path, monkeypatch):
    from app import config
    from scripts import sync_to_drive

    old_file = tmp_path / "Author" / "ready.m4b"
    recent_file = tmp_path / "Author" / "converting.m4b"
    old_file.parent.mkdir(parents=True)
    old_file.write_bytes(b"ready")
    recent_file.write_bytes(b"in progress")
    os.utime(old_file, (time.time() - 600, time.time() - 600))

    monkeypatch.setattr(config, "ROOT_DIR", Path(tmp_path))
    monkeypatch.setattr(sync_to_drive, "MIN_FILE_AGE_SECONDS", 300)

    assert sync_to_drive.detect_new_books({}) == [old_file]
