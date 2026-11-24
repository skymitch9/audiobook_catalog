"""Run one function in a CHILD process that is killed at a hard deadline.

Why this exists (incident 2026-10-19): Firestore DNS failures began 12:31 and
one call inside a watcher tick never returned. The ``.bat`` launchers append
with ``>>`` and Task Scheduler refuses a new instance while one is running, so
ONE hung call blocked every later tick for hours. Worse, after the owner
allowed a kill, the python process reported ``HasExited: True`` while its
teardown stayed stuck (a pending network I/O) and it STILL held the log file
open — only a reboot released it.

⚠️ WHY A CHILD PROCESS AND NOT A THREAD OR A FUTURE. A thread cannot be
abandoned honestly: ``concurrent.futures`` joins its workers at interpreter
exit, and grpc/httplib2 register their own exit work, so a "timed out" future
still keeps the process alive — the very wedge this module removes. A child
process CAN be abandoned: the parent kills it, stops waiting, and exits.

⚠️ WHY TEMP FILES AND NOT PIPES. ``subprocess.run(timeout=)`` on Windows kills
the child and then calls ``communicate()`` with NO timeout, which waits for the
pipes to close — and a child whose teardown is stuck (the 2026-10-19 shape)
never closes them. Output goes to temp files instead, and the child's stdin is
DEVNULL, so the child never inherits the ``.bat``'s ``>>`` log handle either:
even a zombie child cannot hold the watcher's log open.

Contract: ``fn`` must be a module-level function whose arguments and return
value are JSON-serialisable. The child imports it fresh by module and name, so
it runs exactly the code the parent would have run.

Tests: ``IN_CHILD = False`` runs ``fn`` in-process (with the same JSON round
trip, so the two modes cannot drift). ``tests/conftest.py`` sets that for every
test, so no test can reach the real network through a child that its
monkeypatches do not reach; the tests that prove the deadline switch it back on.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.config import PROJECT_ROOT

IN_CHILD = True

# The one line in the child's stdout that carries the result. Everything else
# the child prints is relayed to the parent's stdout (i.e. its log).
RESULT_MARKER = "BOUNDED_RESULT "

# How long to wait for a KILLED child to be reaped before abandoning it. A
# child stuck in kernel I/O may never be reaped; the parent must not wait on it.
REAP_WAIT_S = 10

_CHILD_SRC = (
    "import importlib, json, sys\n"
    "mod_name, fn_name, args_path = sys.argv[1], sys.argv[2], sys.argv[3]\n"
    "with open(args_path, encoding='utf-8') as fh:\n"
    "    args = json.load(fh)\n"
    "fn = getattr(importlib.import_module(mod_name), fn_name)\n"
    "result = fn(*args['args'], **args['kwargs'])\n"
    "sys.stdout.flush()\n"
    f"print({RESULT_MARKER!r} + json.dumps(result), flush=True)\n"
)


@dataclass(frozen=True)
class BoundedResult:
    """What happened. ``ok`` is True only when the function returned."""

    ok: bool
    value: Any = None
    timed_out: bool = False
    error: str = ""          # one line: why it is not ok
    output: str = ""         # what the child printed (result line removed)


def _relay(text: str) -> None:
    """Pass the child's own prints through to the parent's log, unchanged."""
    text = text.rstrip()
    if text:
        print(text, flush=True)


def _in_process(fn: Callable, args: tuple, kwargs: dict) -> BoundedResult:
    try:
        value = fn(*args, **kwargs)
        return BoundedResult(True, json.loads(json.dumps(value)))
    except Exception as e:  # same shape a child crash reports
        return BoundedResult(False, error=f"{type(e).__name__}: {e}")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _module_name(fn: Callable) -> str:
    """The importable name of ``fn``'s module.

    ⚠️ Every watcher runs as ``python -m app.tools.<name>``, so a function
    defined there reports ``__module__ == "__main__"`` — and a child told to
    import ``__main__`` gets its own empty ``-c`` script. The real name is on
    the main module's spec."""
    name = fn.__module__
    if name == "__main__":
        spec = getattr(sys.modules.get("__main__"), "__spec__", None)
        if spec is not None and spec.name:
            return spec.name
    return name


def _kill(proc: subprocess.Popen) -> None:
    """Kill, then wait a SHORT time to reap; abandon if it will not go."""
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=REAP_WAIT_S)
    except subprocess.TimeoutExpired:
        pass


def call(fn: Callable, *args, timeout: float, python: str | None = None,
         relay: bool = True, **kwargs) -> BoundedResult:
    """Run ``fn(*args, **kwargs)`` in a child under ``python`` (default: this
    interpreter) and give up after ``timeout`` seconds. Never raises; never
    blocks past ``timeout + REAP_WAIT_S``.

    ``relay`` prints the child's own output into this process's stdout, so a
    child's log lines land where the tick's lines land.
    """
    if not IN_CHILD:
        return _in_process(fn, args, kwargs)

    tmp = Path(tempfile.mkdtemp(prefix="bounded_"))
    args_path, out_path, err_path = tmp / "args.json", tmp / "out.txt", tmp / "err.txt"
    try:
        args_path.write_text(json.dumps({"args": list(args), "kwargs": kwargs}), encoding="utf-8")
    except (TypeError, ValueError) as e:
        return BoundedResult(False, error=f"arguments are not JSON-serialisable: {e}")

    cmd = [python or sys.executable, "-c", _CHILD_SRC, _module_name(fn), fn.__name__, str(args_path)]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    with open(out_path, "wb") as out, open(err_path, "wb") as err:
        try:
            proc = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT), env=env, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=err)
        except OSError as e:
            return BoundedResult(False, error=f"could not start the child: {e}")
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(proc)
            rc = None

    stdout, stderr = _read(out_path), _read(err_path)
    kept = [ln for ln in stdout.splitlines() if not ln.startswith(RESULT_MARKER)]
    output = "\n".join(kept + [ln for ln in stderr.splitlines() if ln.strip()])
    if relay:
        _relay(output)
    _cleanup(tmp)

    if rc is None:
        return BoundedResult(False, timed_out=True, output=output,
                             error=f"no answer after {timeout:g}s — the call was abandoned and its process killed")
    results = [ln[len(RESULT_MARKER):] for ln in stdout.splitlines() if ln.startswith(RESULT_MARKER)]
    if rc != 0 or not results:
        last = (stderr.strip().splitlines() or ["(no output)"])[-1][:300]
        return BoundedResult(False, output=output, error=f"child exited {rc}: {last}")
    try:
        return BoundedResult(True, json.loads(results[-1]), output=output)
    except json.JSONDecodeError as e:
        return BoundedResult(False, output=output, error=f"unreadable result: {e}")


def _cleanup(tmp: Path) -> None:
    """Best effort: a killed-but-unreaped child may still hold the files."""
    for p in tmp.glob("*"):
        try:
            p.unlink()
        except OSError:
            pass
    try:
        tmp.rmdir()
    except OSError:
        pass
