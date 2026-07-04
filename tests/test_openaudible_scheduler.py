"""Tests for the OpenAudible Quick Sync scheduler and the upload age gate.

Written as unittest.TestCase so run_tests.py (stdlib unittest discovery, used
by CI) collects them; pytest also runs these fine.
"""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from scripts import openaudible_scheduler as scheduler


class SchedulerTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)
        self.status_path = self.tmp_path / "status.json"
        self.command_path = self.tmp_path / "commands.json"
        self._patch(scheduler, "STATUS_PATH", self.status_path)
        self._patch(scheduler, "COMMAND_PATH", self.command_path)
        self._patch(scheduler, "_last_wait_reason", None)

    def _patch(self, obj, name, value):
        original = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(setattr, obj, name, original)

    def _write_status(self, status):
        self.status_path.write_text(json.dumps(status), encoding="utf-8")


class QueueQuickSyncTests(SchedulerTestCase):
    def test_writes_atomic_command(self):
        self._write_status({"quit": False, "queues": {}})

        self.assertTrue(scheduler.queue_quick_sync())
        self.assertEqual(
            json.loads(self.command_path.read_text(encoding="utf-8")), ["Sync_Quick"]
        )
        self.assertFalse(self.command_path.with_suffix(".json.tmp").exists())

    def test_waits_when_not_ready(self):
        cases = {
            "quitting": {"quit": True, "queues": {}},
            "busy": {"quit": False, "queues": {"Downloading": ["book"]}},
        }
        for label, status in cases.items():
            with self.subTest(label):
                self._write_status(status)
                self.assertFalse(scheduler.queue_quick_sync())
                self.assertFalse(self.command_path.exists())

    def test_waits_on_fresh_pending_command(self):
        self._write_status({"quit": False, "queues": {}})
        self.command_path.write_text(json.dumps(["Sync_Quick"]), encoding="utf-8")

        self.assertFalse(scheduler.queue_quick_sync())

    def test_replaces_stale_pending_command(self):
        self._write_status({"quit": False, "queues": {}})
        self.command_path.write_text(json.dumps(["Old_Command"]), encoding="utf-8")
        stale = time.time() - scheduler.STALE_COMMAND_SECONDS - 60
        os.utime(self.command_path, (stale, stale))

        self.assertTrue(scheduler.queue_quick_sync())
        self.assertEqual(
            json.loads(self.command_path.read_text(encoding="utf-8")), ["Sync_Quick"]
        )


class EnvIntTests(unittest.TestCase):
    def test_blank_and_invalid_fall_back_to_default(self):
        for raw in ("", "  ", "abc"):
            with self.subTest(repr(raw)):
                os.environ["_SCHEDULER_TEST_INT"] = raw
                self.addCleanup(os.environ.pop, "_SCHEDULER_TEST_INT", None)
                self.assertEqual(scheduler._env_int("_SCHEDULER_TEST_INT", 42), 42)

    def test_valid_value_is_used(self):
        os.environ["_SCHEDULER_TEST_INT"] = "7"
        self.addCleanup(os.environ.pop, "_SCHEDULER_TEST_INT", None)
        self.assertEqual(scheduler._env_int("_SCHEDULER_TEST_INT", 42), 7)


class DetectNewBooksAgeGateTests(unittest.TestCase):
    def test_skips_recent_files(self):
        from app import config
        from scripts import sync_to_drive

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_path = Path(tmp.name)

        old_file = tmp_path / "Author" / "ready.m4b"
        recent_file = tmp_path / "Author" / "converting.m4b"
        old_file.parent.mkdir(parents=True)
        old_file.write_bytes(b"ready")
        recent_file.write_bytes(b"in progress")
        aged = time.time() - 600
        os.utime(old_file, (aged, aged))

        original_root = config.ROOT_DIR
        original_age = sync_to_drive.MIN_FILE_AGE_SECONDS
        config.ROOT_DIR = Path(tmp_path)
        sync_to_drive.MIN_FILE_AGE_SECONDS = 300
        self.addCleanup(setattr, config, "ROOT_DIR", original_root)
        self.addCleanup(setattr, sync_to_drive, "MIN_FILE_AGE_SECONDS", original_age)

        self.assertEqual(sync_to_drive.detect_new_books({}), [old_file])


if __name__ == "__main__":
    unittest.main()
