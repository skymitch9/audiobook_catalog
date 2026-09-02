"""Make this interpreter's stdout/stderr survive a non-ASCII character.

KI-3, AND WHY THE `.bat` FIX WAS NECESSARY BUT NOT SUFFICIENT
------------------------------------------------------------
The recorded fix for KI-3 is `set PYTHONIOENCODING=utf-8` in the scheduled
wrappers, and measured 2026-09-02 that had ALREADY been applied to 7 of the 9
`.bat` files in this repo. It did not stop the fourth incident, because the
fourth incident was not a scheduled run:

    .venv\\Scripts\\python scripts/set_author_images.py --dry-run

typed at a PowerShell prompt, which died three quarters of the way through the
author list on an author named 猫子. No `.bat` file is on that path, so no
amount of fixing `.bat` files could ever have covered it.

⚠️ AND IT WAS DATA, NOT AN EMOJI. The first three incidents were our own
strings and could in principle have been fixed by not writing emoji. This one
came out of a book's tags. The library's own contents are now a trigger, so
"stop printing emoji" is not a fix and never will be — a CJK author name will
keep arriving whatever we do to our own source.

WHAT THIS DOES
--------------
`force_utf8_stdio()` is called once from `app/__init__.py`, so ANY process that
imports anything under `app.` — which is every script in this repo that touches
the catalogue — gets UTF-8 stdio whether or not a wrapper set the env var.

⚠️ `errors="replace"`, deliberately. The point is that a print can NEVER kill a
run between setup and cleanup, which is where every one of the four incidents
landed. A mangled glyph in a log is a cosmetic loss; a half-written run with no
record of where it stopped is not.

It is a no-op when:
  * the stream is already UTF-8 (a wrapper set `PYTHONIOENCODING`, or the
    console is a modern UTF-8 one) — nothing to do;
  * the stream has no `reconfigure` (pytest's capture objects, a pipe wrapped
    by something else) — nothing safe to do, so nothing is done.

⚠️ IT NEVER RAISES. A module imported for its side effect must not be able to
break every entry point in the repo, so the whole body is guarded. A failure
here leaves exactly the behaviour we had before it existed.
"""

from __future__ import annotations

import sys

__all__ = ["force_utf8_stdio"]


def _reconfigure(stream) -> bool:
    """UTF-8-ify one stream. True if it changed, False if it could not."""
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return False
    current = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
    if current in ("utf8", "utf8sig"):
        return False
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        return False
    return True


def force_utf8_stdio() -> int:
    """Reconfigure stdout/stderr to UTF-8. Returns how many streams changed."""
    changed = 0
    for name in ("stdout", "stderr"):
        try:
            stream = getattr(sys, name, None)
            if stream is not None and _reconfigure(stream):
                changed += 1
        except Exception:
            # See the module docstring: this can never be the thing that
            # breaks a run.
            pass
    return changed
