"""Plan a Drive → local pull without ever creating a duplicate.

The estate pushes local → Drive today (``scripts/sync_to_drive.py``); files that
someone else drops into Drive never come *down*, so they never ingest and never
reach the sites or R2. This module is the matcher for the missing pull step.

⚠️ Three rules, each earned by the 2026-08-24 duplicate incident — see
``docs`` / the DUPLICATES writeup. A pull that ignores any of them re-creates the
mess it exists to prevent:

1. **Match across ALL formats, not audio-only.** The old audit scanned only
   ``{.m4b,.m4a,.mp4}`` locally, so every ``.epub``/``.pdf`` already on disk read
   as "missing" — 170 false positives in one run.
2. **Never pull a ``Copy of …`` file.** The old audit matched by exact name, so
   ``Copy of X`` looked missing even with ``X`` right there, and it downloaded
   four duplicate copies (~1.3 GB).
3. **Keep volume numbers.** A book-identity key that strips a trailing number
   would fold a whole series (``Summoner 2`` … ``Summoner 13``) into one and hide
   the volumes. Only a *parenthesised* ``(N)`` copy-marker is stripped — the same
   conservative rule ``file_dedupe._NUMBERED`` already uses.

The matcher is also **format-class aware**: the same title as ``.epub`` and
``.m4b`` are two different things the household wants BOTH of, so an ebook is
never called "present" just because the audiobook is on disk.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

__all__ = ["ALL_EXTS", "is_copy_name", "match_key", "plan_pull", "PullPlan"]

# Every book format the pull understands — the fix for rule 1.
_AUDIO_EXTS = {".m4b", ".m4a", ".mp4", ".mp3", ".aax", ".flac"}
_TEXT_EXTS = {".epub", ".pdf", ".azw3", ".mobi"}
ALL_EXTS = _AUDIO_EXTS | _TEXT_EXTS

_COPY_PREFIX = re.compile(r"^\s*copy of\s+", re.IGNORECASE)
# A parenthesised copy marker at the very end of the stem: "Title (1)". Same
# anchor discipline as file_dedupe._NUMBERED so "Book (2) of Three" is safe.
_PAREN_COPY = re.compile(r"\s*\(\d+\)$")


def _fmt_class(name: str) -> str:
    """'audio' or 'text' — the household wants both, so they never match."""
    return "audio" if Path(name).suffix.lower() in _AUDIO_EXTS else "text"


def is_copy_name(name: str) -> bool:
    """A Drive-side duplicate we must never pull: ``Copy of X`` or ``X (1)``.

    ⚠️ This is rule 2. A bare trailing number (``Summoner 2``) is NOT a copy —
    that is a series volume (rule 3) and must be pullable.
    """
    return bool(_COPY_PREFIX.search(name) or _PAREN_COPY.search(Path(name).stem))


def match_key(name: str) -> str:
    """Book-identity key for cross-checking Drive against local.

    Strips a ``Copy of `` prefix and a trailing ``(N)`` marker, drops the
    extension, lowercases and removes non-alphanumerics — but **keeps digits**,
    so ``Summoner 2`` and ``Summoner`` stay distinct (rule 3). Suffixed with the
    format class so audio and text of the same title never collide.
    """
    stem = Path(name).stem
    stem = _COPY_PREFIX.sub("", stem)
    stem = _PAREN_COPY.sub("", stem)
    ident = re.sub(r"[^a-z0-9]+", "", stem.lower())
    return f"{ident}|{_fmt_class(name)}"


class PullPlan(NamedTuple):
    """The decision for one Drive listing. Counts + the actual names, so the
    caller can both report and act, and a dry-run can show its work."""

    to_pull: list[str]
    skipped_copies: list[str]   # 'Copy of …' / '(N)' — rule 2, never pulled
    skipped_present: list[str]  # a local file already matches the key
    ignored: list[str]          # not a known book format


def plan_pull(drive_names: list[str], local_names: list[str]) -> PullPlan:
    """Decide, for a flat list of Drive filenames, what to pull.

    ``local_names`` must be gathered across ALL formats (rule 1) or present ebooks
    will be mis-classified as missing. Pure and side-effect-free so it is fully
    testable without Drive or a filesystem.
    """
    local_keys = {match_key(n) for n in local_names if Path(n).suffix.lower() in ALL_EXTS}
    to_pull: list[str] = []
    skipped_copies: list[str] = []
    skipped_present: list[str] = []
    ignored: list[str] = []
    for n in drive_names:
        if Path(n).suffix.lower() not in ALL_EXTS:
            ignored.append(n)
        elif is_copy_name(n):
            skipped_copies.append(n)
        elif match_key(n) in local_keys:
            skipped_present.append(n)
        else:
            to_pull.append(n)
    return PullPlan(to_pull, skipped_copies, skipped_present, ignored)
