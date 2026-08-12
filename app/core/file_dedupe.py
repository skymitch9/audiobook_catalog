"""Reduce a walked library to one file per book.

Extracted out of ``app.main`` on 2026-08-12. Two reasons, and the second is the
one that matters:

1. ``main()`` tripped flake8's ``C901`` complexity ceiling at 17 against a limit
   of 15, failing Lint on every push — and Deploy with it, since Deploy depends
   on Lint. Eight of main's branches lived in this one block.
2. The standing rule for this estate is that an entrypoint stays an orchestrator
   and business logic lives in modules. Raising the ceiling would have silenced
   the warning while making the entrypoint worse; this makes it thinner.

⚠️ Behaviour is unchanged, deliberately. The same two passes run in the same
order with the same tie-breaks. Only the printing moved out — the caller reports,
so this stays testable without capturing stdout.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

__all__ = ["drop_numbered_duplicates", "dedupe_by_filename", "DedupeReport"]

# "Title (1).m4b" — what a download or a Drive sync leaves behind next to the
# original. Anchored at both ends so "Book (2) of Three.m4b" is not a match.
_NUMBERED = re.compile(r"^(.+?)\s*\(\d+\)(\.\w+)$")


class DedupeReport:
    """What the two passes removed. The caller decides how to say it."""

    def __init__(self, numbered: int = 0, duplicates: int = 0) -> None:
        self.numbered = numbered
        self.duplicates = duplicates

    @property
    def total(self) -> int:
        return self.numbered + self.duplicates


def drop_numbered_duplicates(files: list[Path]) -> tuple[list[Path], int]:
    """Drop ``Title (1).m4b`` **only when** ``Title.m4b`` is also present.

    ⚠️ The existence check is the whole point. A book legitimately named with a
    parenthesised number — and this library has real ones — must survive, so a
    numbered file is dropped only when the unnumbered original is right there.
    """
    names = {f.name for f in files}
    kept: list[Path] = []
    dropped = 0
    for f in files:
        m = _NUMBERED.match(f.name)
        if m and (m.group(1) + m.group(2)) in names:
            dropped += 1
            continue
        kept.append(f)
    return kept, dropped


def dedupe_by_filename(files: list[Path]) -> tuple[list[Path], int]:
    """One file per filename: the same .m4b in two author folders is a copy.

    ⚠️ Ties break on the LONGEST parent path, which is a deliberate choice and
    not an arbitrary one: a deeper or longer folder is the more specific
    attribution — ``Michael-Scott Earle/`` over a bare drop folder. It also made
    the 2026-08-11 cleanup safe, where the same book sat under both a pen name
    and a real name.
    """
    by_name: dict[str, list[Path]] = defaultdict(list)
    for f in files:
        by_name[f.name].append(f)

    kept: list[Path] = []
    dropped = 0
    for paths in by_name.values():
        if len(paths) == 1:
            kept.append(paths[0])
        else:
            kept.append(max(paths, key=lambda p: len(str(p.parent))))
            dropped += len(paths) - 1
    return kept, dropped


def dedupe_library(files: list[Path]) -> tuple[list[Path], DedupeReport]:
    """Both passes, in the order ``main()`` ran them.

    Numbered duplicates go first on purpose: removing ``Title (1).m4b`` before
    grouping by filename means the filename pass never has to choose between a
    file and its own numbered copy.
    """
    files, numbered = drop_numbered_duplicates(files)
    files, duplicates = dedupe_by_filename(files)
    return files, DedupeReport(numbered=numbered, duplicates=duplicates)
