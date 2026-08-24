"""
Unit tests for scripts/reclaim_drive_files.py — the safety-critical parts.

Does NOT touch live Drive. Covers two fixes:
  1. delete_file_from_drive TRASHES (recoverable) rather than files().delete()
     (permanent) — a wrong prior decision must not irreversibly destroy another
     user's file.
  2. download_file verifies the downloaded size against Drive's reported size
     before reporting success — because the reclaim path deletes the Drive copy
     only after a "successful" download, so a truncated download reported as
     success would take the only remaining copy with it.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from scripts.reclaim_drive_files import delete_file_from_drive, download_file


class _FakeExec:
    def __init__(self, store, kind, payload):
        self._store, self._kind, self._payload = store, kind, payload

    def execute(self):
        self._store.setdefault(self._kind, []).append(self._payload)
        return {}


class _FakeFiles:
    def __init__(self, store):
        self.store = store

    def update(self, fileId=None, body=None):
        return _FakeExec(self.store, "update", (fileId, body))

    def delete(self, fileId=None):
        return _FakeExec(self.store, "delete", fileId)

    def get_media(self, fileId=None):
        return object()  # opaque request handle; the fake downloader ignores it


class _FakeService:
    def __init__(self):
        self.store = {}
        self._files = _FakeFiles(self.store)

    def files(self):
        return self._files


class DeleteTrashesTestCase(unittest.TestCase):
    def test_delete_moves_to_trash_not_permanent(self):
        svc = _FakeService()
        ok = delete_file_from_drive(svc, "file123", "Book.m4b")
        self.assertTrue(ok)
        # It must call update(trashed=True), never the permanent delete().
        self.assertEqual(svc.store.get("delete", []), [])
        self.assertEqual(len(svc.store.get("update", [])), 1)
        file_id, body = svc.store["update"][0]
        self.assertEqual(file_id, "file123")
        self.assertEqual(body, {"trashed": True})


class _FakeDownloader:
    """Stand-in for MediaIoBaseDownload that writes a fixed payload to the file
    handle it is constructed with, then reports the download complete."""

    payload = b""

    def __init__(self, fh, request, chunksize=0):
        self._fh = fh

    def next_chunk(self):
        self._fh.write(type(self).payload)
        return (None, True)


def _patch_downloader(payload: bytes):
    _FakeDownloader.payload = payload
    return mock.patch(
        "googleapiclient.http.MediaIoBaseDownload", _FakeDownloader
    )


class DownloadSizeVerifyTestCase(unittest.TestCase):
    def test_size_mismatch_is_failure_and_removes_partial(self):
        svc = _FakeService()
        with TemporaryDirectory() as td, _patch_downloader(b"abc"):
            dest = Path(td) / "Book.m4b"
            ok = download_file(svc, "id", dest, expected_size=10)
            self.assertFalse(ok, "a short download must be reported as failure")
            self.assertFalse(dest.exists(), "partial file must be removed")

    def test_size_match_succeeds(self):
        svc = _FakeService()
        with TemporaryDirectory() as td, _patch_downloader(b"0123456789"):
            dest = Path(td) / "Book.m4b"
            ok = download_file(svc, "id", dest, expected_size=10)
            self.assertTrue(ok)
            self.assertTrue(dest.exists())
            self.assertEqual(dest.stat().st_size, 10)

    def test_zero_expected_size_skips_check(self):
        # Back-compat: callers that don't pass a size get the old behaviour.
        svc = _FakeService()
        with TemporaryDirectory() as td, _patch_downloader(b"whatever"):
            dest = Path(td) / "Book.m4b"
            ok = download_file(svc, "id", dest, expected_size=0)
            self.assertTrue(ok)
            self.assertTrue(dest.exists())


if __name__ == "__main__":
    unittest.main()
