"""F3 (2026-09-07) — the ONE atomic JSON writer, and both pipeline callers.

WHAT THESE TESTS ARE FOR, AND WHAT THEY ARE NOT
-----------------------------------------------
The residual F3 closed is a POWER-LOSS residual, not a crash residual:
`os.replace` alone already survived a `SIGKILL`. So the two things worth
pinning are the two things that were actually missing, and both are asserted
by OBSERVATION of what the writer did, not by reading the source:

1. **`fsync` is called** — the durability half. Pinned by spying on
   `os.fsync`, because there is no way to observe a lost buffered write from
   a test without cutting the machine's power.
2. **The temp name is UNIQUE** — two concurrent writers must not collide on
   one fixed `ingest_state.tmp`. Pinned by capturing the temp path the writer
   actually opened and asserting it is not the old fixed name.

Plus the properties that must not regress: the target is never observed
half-written, a failed write leaves the PREVIOUS file intact and no `.tmp`
litter behind, and each caller's on-disk format is unchanged (byte-comparable
to what it wrote before the refactor — `indent=1` for the ingest files,
`indent=2` for the upload manifest).

⚠️ NOT covered here: `load_state()`'s behaviour on a corrupt file. It still
raises, deliberately — see `app/core/atomic_json.py`'s header and the F3
section of `docs/TODO.md`. Nothing in this file touches the LIVE state file;
every test writes into `tmp_path`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.core import ingest_queue, ingest_queue_summary
from app.core.atomic_json import write_json_atomic


# ---------------------------------------------------------------------------
# the helper itself
# ---------------------------------------------------------------------------

def test_writes_the_payload_and_creates_missing_parents(tmp_path: Path):
    target = tmp_path / "deep" / "nested" / "state.json"
    write_json_atomic(target, {"books": {"a": 1}})
    assert json.loads(target.read_text(encoding="utf-8")) == {"books": {"a": 1}}


def test_fsyncs_before_the_replace(tmp_path: Path, monkeypatch):
    """THE DURABILITY GAP F3 CLOSED. tmp+replace survives a kill; it does not
    survive a power cut, because the rename can reach the disk before the
    data blocks do. Only `fsync` orders those two, and the only way to observe
    it from a test is to watch for the call."""
    order: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def spy_fsync(fd):
        order.append("fsync")
        return real_fsync(fd)

    def spy_replace(src, dst):
        order.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(os, "replace", spy_replace)
    write_json_atomic(tmp_path / "s.json", {"x": 1})

    assert order == ["fsync", "replace"], (
        "fsync must happen while the file is still open, BEFORE the rename - "
        f"observed {order}"
    )


def test_temp_name_is_unique_not_the_fixed_dot_tmp(tmp_path: Path, monkeypatch):
    """THE COLLISION GAP. `path.with_suffix('.tmp')` gave every writer in a
    directory the same temp name; `--status`, `--requeue-ocr` and the nightly
    fire are separate PROCESSES, so the single-flight lock does not separate
    them."""
    opened: list[str] = []
    real_mkstemp = __import__("tempfile").mkstemp

    def spy_mkstemp(**kwargs):
        fd, name = real_mkstemp(**kwargs)
        opened.append(name)
        return fd, name

    monkeypatch.setattr("app.core.atomic_json.tempfile.mkstemp", spy_mkstemp)

    target = tmp_path / "ingest_state.json"
    write_json_atomic(target, {"a": 1})
    write_json_atomic(target, {"a": 2})

    assert len(opened) == 2
    assert opened[0] != opened[1], "two writes must not share a temp name"
    for name in opened:
        assert Path(name).parent == tmp_path, "temp must sit beside the target"
        assert name != str(target.with_suffix(".tmp")), (
            "the fixed pre-F3 temp name is exactly what this replaced"
        )


def test_a_failed_write_keeps_the_previous_file_and_leaves_no_litter(
    tmp_path: Path, monkeypatch
):
    target = tmp_path / "state.json"
    write_json_atomic(target, {"good": True})
    before = target.read_bytes()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("app.core.atomic_json.json.dump", boom)
    with pytest.raises(OSError):
        write_json_atomic(target, {"good": False})

    assert target.read_bytes() == before, "the previous file must survive intact"
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"], (
        "a failed write must not leave a temp file behind"
    )


def test_the_target_is_never_observed_half_written(tmp_path: Path, monkeypatch):
    """The atomicity property, asserted from OUTSIDE: at the moment the data
    has been dumped but the swap has not happened, the target still holds the
    old content in full."""
    target = tmp_path / "state.json"
    write_json_atomic(target, {"version": 1})
    seen = {}
    real_replace = os.replace

    def peek(src, dst):
        seen["mid_write"] = Path(dst).read_text(encoding="utf-8")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", peek)
    write_json_atomic(target, {"version": 2})

    assert json.loads(seen["mid_write"]) == {"version": 1}
    assert json.loads(target.read_text(encoding="utf-8")) == {"version": 2}


# ---------------------------------------------------------------------------
# save_state() — ingest_state.json
# ---------------------------------------------------------------------------

def test_save_state_round_trips_through_load_state(tmp_path: Path):
    path = tmp_path / "ingest_state.json"
    state = {"version": 1, "books": {"a-book": {"status": "done"}}, "runs": []}
    ingest_queue.save_state(state, path)
    assert ingest_queue.load_state(path) == state


def test_save_state_keeps_indent_1_and_unescaped_non_ascii(tmp_path: Path):
    """⚠️ FORMAT IS A CONTRACT HERE, not a style choice. `ingest_state.json`
    is 1,244 rows a human reads via `--status`; re-indenting it would rewrite
    the entire file on the next save. `ensure_ascii=False` keeps titles
    legible rather than `\\uXXXX`-escaped."""
    path = tmp_path / "ingest_state.json"
    ingest_queue.save_state({"books": {"x": {"title": "Café Ω"}}}, path)
    text = path.read_text(encoding="utf-8")

    assert "Café Ω" in text, "non-ASCII titles must stay unescaped"
    assert '\n "books": {' in text, f"expected indent=1, got:\n{text}"


def test_save_state_goes_through_the_shared_writer(tmp_path: Path, monkeypatch):
    """Pins the DEDUPLICATION, not just the behaviour: if someone re-inlines a
    tmp+replace here, the two copies drift again and only one gets the next
    durability fix. That is the bug F3's second half actually was."""
    calls = []
    monkeypatch.setattr(
        ingest_queue, "write_json_atomic",
        lambda p, d, **kw: calls.append((p, d, kw)),
    )
    ingest_queue.save_state({"books": {}}, tmp_path / "s.json")
    assert calls and calls[0][2] == {"indent": 1, "ensure_ascii": False}


def test_save_state_is_durable(tmp_path: Path, monkeypatch):
    """The end-to-end version of the fsync test, through the real caller —
    this is the writer that runs once per book, 1,244 times so far."""
    synced = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])
    ingest_queue.save_state({"books": {}}, tmp_path / "ingest_state.json")
    assert synced, "save_state must fsync - the pre-F3 version did not"


# ---------------------------------------------------------------------------
# write_queue_summary() — queue_summary.json
# ---------------------------------------------------------------------------

def test_write_queue_summary_writes_and_keeps_its_format(tmp_path: Path):
    path = tmp_path / "queue_summary.json"
    summary = {"total": 3, "lanes": {"epub": 1, "audiobook": 2}}
    ingest_queue_summary.write_queue_summary(summary, path)

    assert json.loads(path.read_text(encoding="utf-8")) == summary
    assert '\n "total": 3' in path.read_text(encoding="utf-8"), "indent=1"


def test_write_queue_summary_is_durable(tmp_path: Path, monkeypatch):
    synced = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])
    ingest_queue_summary.write_queue_summary({"total": 0}, tmp_path / "q.json")
    assert synced, "write_queue_summary must fsync - the pre-F3 version did not"


def test_write_queue_summary_still_never_raises(tmp_path: Path, monkeypatch):
    """⚠️ THE SWALLOW MUST SURVIVE THE REFACTOR. The shared helper raises by
    design; this caller catching it is the whole "failing to write a REPORTING
    artefact must never stop an ingest run" contract. Moving the write into a
    helper that raises is exactly how that guarantee gets lost by accident."""
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(ingest_queue_summary, "write_json_atomic", boom)
    ingest_queue_summary.write_queue_summary({"total": 0}, tmp_path / "q.json")  # no raise


def test_write_queue_summary_leaves_the_previous_file_on_failure(
    tmp_path: Path, monkeypatch
):
    path = tmp_path / "queue_summary.json"
    ingest_queue_summary.write_queue_summary({"total": 1}, path)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("app.core.atomic_json.json.dump", boom)
    ingest_queue_summary.write_queue_summary({"total": 999}, path)

    assert json.loads(path.read_text(encoding="utf-8")) == {"total": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["queue_summary.json"]


# ---------------------------------------------------------------------------
# the third caller — sync_to_drive, whose copy this was lifted from
# ---------------------------------------------------------------------------

def test_sync_to_drive_adapter_keeps_indent_2(tmp_path: Path):
    """`upload_manifest.json` was written with `indent=2` and must stay that
    way: the helper's default is 1 (the ingest files' format), so a caller
    that forgot to pass its own would silently rewrite a different file's
    whole shape on the next save."""
    import scripts.sync_to_drive as sync

    path = tmp_path / "upload_manifest.json"
    sync._atomic_write_json(path, {"a/b.m4b": {"drive_file_id": "x"}})
    text = path.read_text(encoding="utf-8")

    assert json.loads(text) == {"a/b.m4b": {"drive_file_id": "x"}}
    assert '\n  "a/b.m4b"' in text, f"expected indent=2, got:\n{text}"
