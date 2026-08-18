# Pins the per-book progress tee in scripts/transcribe_audiobook.py, added
# 2026-08-18 so https://heygabi.ai/status/processing can show a REAL percentage
# for the book being transcribed.
#
# ⚠️ THE TWO THINGS THAT MUST NEVER BREAK, in priority order:
#
#   1. THE RELAY MUST NOT CHANGE WHAT ANYONE SEES OR COSTS A BOOK. The worker's
#      stdout used to be inherited and reach the log untouched; now this process
#      reads it on the way past. Every byte, in order, still gets through - and
#      a status write that fails must never propagate into a twenty-minute GPU
#      run. Those are the first tests below.
#   2. THE PERCENTAGE IS A MEASUREMENT. `transcribed span / container duration`,
#      the same ratio the truncation gate uses. The renderer draws a bar from it
#      and promises never to estimate one, so an elapsed-time figure must never
#      creep into this field.
#
# The `[whisper]` line format is copied verbatim from the worker's own printf
# in `_WORKER`, not paraphrased - a paraphrased fixture would pass while the
# real format silently stopped matching, which is the failure nobody sees.
import importlib.util
import io
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "transcribe_audiobook",
    Path(__file__).resolve().parents[1] / "scripts" / "transcribe_audiobook.py",
)
ta = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ta)


# The exact bytes the worker emits: "[whisper] %.2fh audio | %.1fmin wall |
# %.1fx rt | %d words"
PROGRESS_LINE = "[whisper] 1.25h audio | 12.3min wall | 61.0x rt | 18234 words"
MODEL_LINE = "[whisper] model loaded in 12.3s"


# ---------------------------------------------------------------------------
# parsing the worker's own line
# ---------------------------------------------------------------------------

def test_the_real_whisper_progress_line_parses():
    hit = ta.parse_whisper_progress(PROGRESS_LINE)
    assert hit == {
        "audio_hours_done": 1.25,
        "wall_minutes": 12.3,
        "realtime_factor": 61.0,
        "words": 18234,
    }


def test_the_line_still_parses_with_a_trailing_newline_and_leading_space():
    assert ta.parse_whisper_progress("  " + PROGRESS_LINE + "\r\n") is not None


def test_model_loading_is_not_progress():
    # ⚠️ It starts with "[whisper]" too. Loading a model is not transcribing a
    # book, and a 0% bar during model load would say the run had not started.
    assert ta.parse_whisper_progress(MODEL_LINE) is None


@pytest.mark.parametrize("line", [
    "",
    "DONE {\"meta\": 1}",
    "[transcribe] wav ready in 40s",
    "[whisper] 1.25h audio | 12.3min wall",          # truncated line
    "Traceback (most recent call last):",
])
def test_everything_else_is_not_progress(line):
    assert ta.parse_whisper_progress(line) is None


# ---------------------------------------------------------------------------
# the progress file
# ---------------------------------------------------------------------------

def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_percent_is_span_over_container_and_nothing_else(tmp_path):
    path = tmp_path / "transcribe_progress.json"
    ok = ta.write_progress(
        m4b=Path(r"C:\books\The Primal Hunter 12.m4b"), title="The Primal Hunter 12",
        container_s=72451.188, started_at="2026-08-18T19:37:33Z",
        audio_hours_done=5.00, wall_minutes=10.0, realtime_factor=30.0, words=1000,
        path=path, now="2026-08-18T19:47:33Z")
    assert ok
    rec = _read(path)
    # 5 h = 18,000 s of a 72,451.188 s container = 24.8%
    assert rec["percent"] == pytest.approx(24.8)
    assert rec["audio_seconds_done"] == pytest.approx(18000.0)
    assert rec["container_duration_s"] == pytest.approx(72451.188)
    assert rec["title"] == "The Primal Hunter 12"
    assert rec["started_at"] == "2026-08-18T19:37:33Z"
    assert rec["updated_at"] == "2026-08-18T19:47:33Z"
    # ⚠️ started_at and updated_at are two DIFFERENT clocks and the reader needs
    # both: one dates the run, the other dates the measurement.
    assert rec["started_at"] != rec["updated_at"]


def test_a_zero_or_missing_duration_yields_no_percent_rather_than_a_divide(tmp_path):
    path = tmp_path / "p.json"
    ta.write_progress(m4b="x.m4b", title="x", container_s=0, started_at="t",
                      audio_hours_done=1.0, path=path)
    # ⚠️ None, never 0. "We could not compute it" and "none of the book is done"
    # are different claims and the page words them differently.
    assert _read(path)["percent"] is None


def test_optional_fields_are_ABSENT_when_not_reported(tmp_path):
    path = tmp_path / "p.json"
    ta.write_progress(m4b="x.m4b", title="x", container_s=3600, started_at="t",
                      audio_hours_done=0.5, path=path)
    rec = _read(path)
    assert "wall_minutes" not in rec and "words" not in rec
    assert rec["percent"] == pytest.approx(50.0)


def test_the_write_is_atomic_and_leaves_no_tmp_behind(tmp_path):
    path = tmp_path / "p.json"
    ta.write_progress(m4b="x.m4b", title="x", container_s=3600, started_at="t",
                      audio_hours_done=0.1, path=path)
    assert path.exists()
    assert list(tmp_path.iterdir()) == [path], "the tmp file must be renamed, not left"


def test_a_failed_status_write_NEVER_raises(tmp_path):
    # ⚠️ THE MOST IMPORTANT TEST IN THIS FILE. A full disk or a locked file is a
    # reason for the status page to go quiet, never a reason to lose a book.
    unwritable = tmp_path / "no" / "such" / "dir" / "p.json"
    (tmp_path / "no").write_text("this is a FILE, so mkdir of a child must fail")
    assert ta.write_progress(m4b="x", title="x", container_s=1, started_at="t",
                             audio_hours_done=0.0, path=unwritable) is False


def test_clear_removes_it_and_is_safe_to_call_twice(tmp_path):
    path = tmp_path / "p.json"
    path.write_text("{}")
    ta.clear_progress(path)
    assert not path.exists()
    ta.clear_progress(path)   # must not raise on a file that is already gone


# ---------------------------------------------------------------------------
# the relay
# ---------------------------------------------------------------------------

class FakeProc:
    """A worker whose stdout is a fixed script of bytes."""

    def __init__(self, chunks, returncode=0):
        self.stdout = io.BytesIO(b"".join(chunks))
        self.returncode = returncode
        self.waited = False

    def wait(self):
        self.waited = True
        return self.returncode


def _popen_returning(proc):
    def popen(cmd, cwd=None, stdout=None):
        popen.seen = {"cmd": cmd, "cwd": cwd, "stdout": stdout}
        return proc
    return popen


def test_every_byte_is_relayed_unchanged_and_in_order(tmp_path, monkeypatch, capsysbinary):
    monkeypatch.setattr(ta, "PROGRESS_PATH", tmp_path / "p.json")
    lines = [
        MODEL_LINE.encode() + b"\n",
        PROGRESS_LINE.encode() + b"\n",
        # ⚠️ NON-ASCII, on purpose. The worker's DONE line carries an m4b path
        # and book titles contain curly apostrophes. Decoding and re-encoding
        # this on a cp1252 console is how a relay kills a finished book.
        'DONE {"source_m4b": "C:\\\\books\\\\Sorcerer\u2019s Stone.m4b"}\n'.encode("utf-8"),
    ]
    proc = FakeProc(lines)
    rc = ta.run_worker(["worker"], m4b="x.m4b", title="x", container_s=3600,
                       started_at="2026-08-18T19:00:00Z", popen=_popen_returning(proc))
    assert rc == 0
    assert proc.waited, "the child must be reaped, not left"
    out = capsysbinary.readouterr().out
    assert out == b"".join(lines), "the relay must be byte-for-byte, in order"


def test_only_progress_lines_write_a_file_and_the_last_one_wins(tmp_path, monkeypatch, capsysbinary):
    path = tmp_path / "p.json"
    monkeypatch.setattr(ta, "PROGRESS_PATH", path)

    proc = FakeProc([MODEL_LINE.encode() + b"\n"])
    ta.run_worker(["w"], m4b="x.m4b", title="x", container_s=3600,
                  popen=_popen_returning(proc))
    capsysbinary.readouterr()
    assert not path.exists(), "model loading must not publish progress"

    proc = FakeProc([
        b"[whisper] 0.25h audio | 1.0min wall | 15.0x rt | 100 words\n",
        b"[whisper] 0.50h audio | 2.0min wall | 15.0x rt | 200 words\n",
    ])
    ta.run_worker(["w"], m4b="x.m4b", title="Book", container_s=3600,
                  popen=_popen_returning(proc))
    capsysbinary.readouterr()
    rec = _read(path)
    assert rec["audio_hours_done"] == 0.5 and rec["percent"] == pytest.approx(50.0)


def test_a_stale_file_from_a_killed_run_is_cleared_before_the_next_one(tmp_path, monkeypatch, capsysbinary):
    # ⚠️ A progress file outliving its run claims a book is transcribing while
    # the GPU is idle. transcribe()'s finally clears it on every normal exit;
    # this covers the run that was killed outright and never reached one.
    path = tmp_path / "p.json"
    monkeypatch.setattr(ta, "PROGRESS_PATH", path)
    path.write_text(json.dumps({"title": "a book from a run that was killed"}))
    ta.run_worker(["w"], m4b="x.m4b", title="x", container_s=3600,
                  popen=_popen_returning(FakeProc([MODEL_LINE.encode() + b"\n"])))
    capsysbinary.readouterr()
    assert not path.exists()


def test_the_workers_exit_code_is_returned_unchanged(tmp_path, monkeypatch, capsysbinary):
    monkeypatch.setattr(ta, "PROGRESS_PATH", tmp_path / "p.json")
    rc = ta.run_worker(["w"], m4b="x", title="x", container_s=1,
                       popen=_popen_returning(FakeProc([b"boom\n"], returncode=9)))
    capsysbinary.readouterr()
    assert rc == 9, "a failing worker must still fail the transcription"


def test_stderr_is_NOT_piped(tmp_path, monkeypatch, capsysbinary):
    # ⚠️ Deliberate: an inherited stderr keeps tracebacks where they always
    # landed, and a second pipe nobody drains is a deadlock while this loop is
    # blocked reading the first.
    monkeypatch.setattr(ta, "PROGRESS_PATH", tmp_path / "p.json")
    popen = _popen_returning(FakeProc([b"x\n"]))
    ta.run_worker(["w"], m4b="x", title="x", container_s=1, popen=popen)
    capsysbinary.readouterr()
    assert "stderr" not in popen.seen
