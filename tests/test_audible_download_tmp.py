"""The downloader's temp-dir cleanup must remove READ-ONLY entries.

Incident 2026-11-15: OneDrive marked the `_dl_<ASIN>` temp dirs read-only,
Windows refused rmdir (WinError 5), and every retry of the same book died on
the leftover dir before contacting Audible.
"""

import os
import stat

import pytest

from app.tools.audible_download import _rmtree


def _make_readonly_tree(root):
    tmp = root / "_dl_B000TEST00"
    sub = tmp / "parts"
    sub.mkdir(parents=True)
    f = sub / "part1.aax"
    f.write_bytes(b"x")
    os.chmod(f, stat.S_IREAD)
    if os.name == "nt":
        # On Windows chmod toggles FILE_ATTRIBUTE_READONLY, which is what
        # makes rmdir fail. On POSIX a read-only DIR would block listing it,
        # which is a different fault from the one under test.
        os.chmod(sub, stat.S_IREAD)
        os.chmod(tmp, stat.S_IREAD)
    return tmp


def test_rmtree_removes_readonly_dirs_and_files(tmp_path):
    tmp = _make_readonly_tree(tmp_path)
    _rmtree(tmp)
    assert not tmp.exists()


def test_rmtree_ignore_errors_swallows_a_missing_dir(tmp_path):
    _rmtree(tmp_path / "never-existed", ignore_errors=True)


def test_rmtree_raises_on_a_missing_dir_by_default(tmp_path):
    with pytest.raises(OSError):
        _rmtree(tmp_path / "never-existed")
