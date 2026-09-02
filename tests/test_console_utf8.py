# tests/test_console_utf8.py
#
# KI-3 — the cp1252 console crash, killed mechanically.
#
# ⚠️ THE POINT OF THESE TESTS IS THE SUBPROCESS ONE. Everything else here is
# unit-level and could pass while the real bug stayed alive, because the bug
# only exists in a process whose stdout was opened with a cp1252 codec. So the
# load-bearing case starts a real interpreter with `PYTHONIOENCODING=cp1252`,
# prints the exact author name that killed the fourth incident (猫子), and
# checks it survives. It is negative-checked by its own sibling: the same
# subprocess WITHOUT `import app` must still die, or the test proves nothing.

from __future__ import annotations

import subprocess
import sys
import unittest

from app.core.console import force_utf8_stdio

CJK_AUTHOR = "猫子"  # 猫子 — the author that killed set_author_images.py


class _NoReconfigureStream:
    """What pytest's capture objects look like: a stream with no reconfigure."""

    def __init__(self, encoding):
        self.encoding = encoding
        self.calls = []


class _FakeStream(_NoReconfigureStream):
    def __init__(self, encoding, raises=False):
        super().__init__(encoding)
        self._raises = raises

    def reconfigure(self, **kwargs):
        if self._raises:
            raise ValueError("cannot reconfigure a detached stream")
        self.calls.append(kwargs)
        self.encoding = kwargs.get("encoding", self.encoding)


class ForceUtf8StdioUnit(unittest.TestCase):
    def _run_with(self, stdout, stderr):
        real = (sys.stdout, sys.stderr)
        sys.stdout, sys.stderr = stdout, stderr
        try:
            return force_utf8_stdio()
        finally:
            sys.stdout, sys.stderr = real

    def test_cp1252_streams_are_reconfigured_with_replace(self):
        out, err = _FakeStream("cp1252"), _FakeStream("cp1252")
        self.assertEqual(self._run_with(out, err), 2)
        for stream in (out, err):
            self.assertEqual(stream.calls,
                             [{"encoding": "utf-8", "errors": "replace"}])

    def test_errors_replace_not_strict(self):
        # ⚠️ Load-bearing. `errors="strict"` would move the crash rather than
        # remove it, and every one of the four incidents died BETWEEN setup and
        # cleanup, where a crash costs the most.
        out = _FakeStream("cp1252")
        self._run_with(out, _FakeStream("utf-8"))
        self.assertEqual(out.calls[0]["errors"], "replace")

    def test_a_stream_already_utf8_is_left_alone(self):
        for encoding in ("utf-8", "UTF-8", "utf8", "UTF-8-SIG"):
            with self.subTest(encoding=encoding):
                out = _FakeStream(encoding)
                self._run_with(out, _FakeStream("utf-8"))
                self.assertEqual(out.calls, [])

    def test_a_stream_that_cannot_reconfigure_is_survived(self):
        # pytest's capture objects have no `reconfigure`. Nothing safe to do,
        # so nothing is done — and nothing raises.
        out = _NoReconfigureStream("cp1252")
        self.assertEqual(self._run_with(out, _FakeStream("utf-8")), 0)

    def test_a_stream_whose_reconfigure_raises_is_survived(self):
        # ⚠️ A module imported for its side effect must never be able to break
        # every entry point in the repo.
        out = _FakeStream("cp1252", raises=True)
        self.assertEqual(self._run_with(out, _FakeStream("utf-8")), 0)


class ForceUtf8StdioLive(unittest.TestCase):
    """The real thing: a real interpreter, a real cp1252 pipe, a real name."""

    def _run(self, code):
        return subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**_clean_env(), "PYTHONIOENCODING": "cp1252"},
        )

    def test_importing_app_makes_the_cjk_author_printable(self):
        done = self._run(f"import app\nprint('AUTHOR {CJK_AUTHOR} OK')")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("OK", done.stdout)

    def test_negative_check_without_the_guard_it_still_dies(self):
        # ⚠️ Without this, the test above could pass because the environment
        # was already UTF-8 and prove nothing at all.
        done = self._run(f"print('AUTHOR {CJK_AUTHOR} OK')")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("UnicodeEncodeError", done.stderr)


def _clean_env():
    import os
    env = dict(os.environ)
    env.pop("PYTHONIOENCODING", None)
    env["PYTHONPATH"] = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
    return env


if __name__ == "__main__":
    unittest.main()
