"""Retire a DUPLICATE CATALOG ROW without touching a single file.

WHAT THIS IS FOR
----------------
The household owns some books twice, as two different *audio editions* of the
same work: an Audible download and a Kickstarter/backer copy, a re-recording, a
dramatised second cut. Both are real files, both are worth keeping, and the
catalogue shows both as separate cards — which is right for two different books
and wrong for two editions of one.

Owner, 2026-09-02, about *Isles of the Emberdark*: **"Keep the audible one but
make sure all source files stay."** That sentence is the whole design: one
catalogue identity, every byte where it was.

🔴 THE ABSOLUTE CONSTRAINT — THIS MODULE NEVER TOUCHES A FILE
-------------------------------------------------------------
It takes a list of paths and returns a shorter list of paths. It does not
delete, move, rename, re-tag or upload anything, on local disk, on Google
Drive, or in the R2 `estate-audio` archive. The retired edition keeps its file,
its Drive copy, its archive key and its `upload_manifest.json` entry; it simply
stops producing a row in `site/catalog.csv`. Every downstream surface — the
site, the index push, the ebook manifest's "N audio editions", the ingest
queue — reads the catalogue, so one drop here is the whole change.

⚠️ WHY THIS IS NOT `app/core/file_dedupe.py`
--------------------------------------------
That module already removes duplicates and deliberately cannot do this one. It
knows two shapes, both keyed on the FILENAME: `Title (1).m4b` beside
`Title.m4b`, and the same filename in two author folders. Two editions of one
book have neither — `Isles of the Emberdark - A Cosmere Novel Secret Projects,
Book 5.m4b` and `Isles_of_the_Emberdark_by_Brandon_Sanderson.mp4` share no
string a machine should be allowed to fold. A rule loose enough to catch them
would fold real books; the estate has already paid for that lesson twice (the
five Space Knight volumes onto one twin key, and the fold that made "Space
Knight" and this very title collide in `app/library_link.py`). So the join is
not computed at all. It is WRITTEN DOWN, one entry per pair, by a person.

⚠️ WHY IT IS NOT `scripts/catalog_overrides.json` EITHER
--------------------------------------------------------
That layer corrects a row's FIELDS (`CORRECTABLE_FIELDS`); it has no concept of
a row that should not exist. Both Emberdark rows already carry override entries
and both are correct — the duplication is not a metadata error in either one.

THE POSTURE: ASSERT BOTH SIDES, AND FAIL BY KEEPING
---------------------------------------------------
Before dropping anything, every entry is asserted against the live library:
both files must be present in the walk, and each must still read back the
title, author, narrator and duration the table records for it. A row whose
live state has drifted is **REFUSED BY NAME** and BOTH editions stay in the
catalogue. There is no closest match, no lowercase-and-hope, no repair of a
stale row — the same discipline as `scripts/merge_abs_authors.py`.

⚠️ The failure direction is chosen, not accidental. A wrong refusal shows a
book twice, which is the status quo and visible. A wrong drop makes a book the
household owns vanish from its own catalogue, which is invisible — nobody
misses a card they never saw. So: **malformed table, missing file, drifted
metadata, ambiguous path → keep both.**

See `docs/info/catalog-twins.md` for the operating reference and
`app/tools/catalog_twins.py` for the dry-run-by-default CLI.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "TwinReport",
    "TwinProbe",
    "DEFAULT_TABLE_PATH",
    "ASSERTED_FIELDS",
    "load_table",
    "probe_file",
    "apply_catalog_twins",
]

DEFAULT_TABLE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "catalog_twins.json"

# ⚠️ The four fields every side of every entry must still read back.
#
# `duration_hhmm` is the load-bearing one and the reason it is in the list: two
# editions of one book agree on it to the minute (both Emberdark files are
# 16:53), so a value that has MOVED means the file behind this path is no longer
# the recording somebody looked at — a re-download, a different cut, a path
# reused for another book. Title/author/narrator catch a retag; duration catches
# a substitution, which is the one a retag check would sail past.
ASSERTED_FIELDS = ("title", "author", "narrator", "duration_hhmm")


class TwinProbe(dict):
    """What `probe_file` reads back. A plain dict of `ASSERTED_FIELDS`."""


class TwinReport:
    """What happened, in words the caller prints.

    ⚠️ `refused` is not an error channel that can be ignored: an entry that
    refuses leaves the duplicate on the site, so the line has to reach a human
    every build. `app/main.py` prints them as `[WARN]`.
    """

    def __init__(self) -> None:
        self.dropped: List[str] = []      # relative paths no longer catalogued
        self.refused: List[Tuple[str, str]] = []   # (entry label, why)
        self.entries: int = 0

    @property
    def applied(self) -> int:
        return len(self.dropped)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<TwinReport entries={self.entries} dropped={self.applied} refused={len(self.refused)}>"


def load_table(path: Path = DEFAULT_TABLE_PATH) -> Tuple[List[dict], Optional[str]]:
    """`(entries, problem)`.

    ⚠️ A missing or malformed table is `([], "<why>")`, never an exception and
    never a build failure — the fail-safe direction is "catalogue everything",
    which is what the site did before this module existed. The `problem` string
    exists so the no-op is still SAID OUT LOUD; a silent no-op is how a
    correction layer stops working and nobody notices for a month.
    """
    if not path.exists():
        return [], None  # absent on purpose is not a problem — no twins recorded
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — a broken table must not stop a build
        return [], f"{path.name} is not readable JSON ({exc}); no twins applied"
    if not isinstance(data, dict):
        return [], f"{path.name} is not a JSON object; no twins applied"
    entries = data.get("twins")
    if not isinstance(entries, list):
        return [], f"{path.name} has no 'twins' list; no twins applied"
    return [e for e in entries if isinstance(e, dict)], None


def probe_file(path: Path) -> TwinProbe:
    """Read the asserted fields off one file — and NOTHING ELSE.

    ⚠️ Deliberately not `app.metadata.extract_metadata`, for one reason:
    that function has SIDE EFFECTS. It writes the cover art out to disk
    (`_save_cover_for_file`) and scans the folder for companion files. Doing
    that for an edition we are about to drop would leave an orphan cover in
    `site/covers/` on every build, and this module's entire promise is that it
    writes nothing.

    ⚠️ It is also NOT a second derivation. The values come from the same two
    public helpers the catalogue itself uses — `derive_correctable_fields` and
    the corrections layer — so an assertion compares against exactly what the
    row WOULD have said. A private re-implementation here would drift from the
    build and start refusing valid entries, or worse, stop refusing invalid
    ones.
    """
    from mutagen.mp4 import MP4  # local: keeps this module importable in tests

    from app.core.catalog_overrides import apply_overrides
    from app.metadata import K_ASIN, derive_correctable_fields, get_tag_any, sec_to_hhmm

    audio = MP4(str(path))
    tags = audio.tags or {}
    duration = getattr(getattr(audio, "info", None), "length", None)
    corrected = apply_overrides(
        derive_correctable_fields(tags),
        path=path,
        asin=get_tag_any(tags, [K_ASIN]),
    )
    return TwinProbe(
        title=corrected.get("title") or "",
        author=corrected.get("author") or "",
        narrator=corrected.get("narrator") or "",
        duration_hhmm=sec_to_hhmm(int(duration) if duration else None),
    )


def _rel(path: Path, root: Path) -> str:
    """The path as the table spells it: forward slashes, relative to the root."""
    import os

    return os.path.relpath(str(path), str(root)).replace("\\", "/")


def _find(rel_wanted: str, index: Dict[str, List[Path]]) -> Tuple[Optional[Path], Optional[str]]:
    """`(path, problem)` — exactly one match, or a named refusal."""
    hits = index.get(rel_wanted.replace("\\", "/").casefold(), [])
    if not hits:
        return None, f"no file at {rel_wanted!r} in the walked library"
    if len(hits) > 1:
        return None, f"{len(hits)} files match {rel_wanted!r} — ambiguous, refusing"
    return hits[0], None


def _assert_side(label: str, path: Path, spec: dict, probe: Callable[[Path], TwinProbe]) -> Optional[str]:
    """`None` when every asserted field still reads back, else why not."""
    try:
        live = probe(path)
    except Exception as exc:  # noqa: BLE001 — an unreadable file is a refusal, not a crash
        return f"{label} {path.name!r} could not be read ({exc})"
    for field in ASSERTED_FIELDS:
        want = spec.get(field)
        if want is None:
            return f"{label} entry is missing the required {field!r} assertion"
        if str(live.get(field, "")).strip() != str(want).strip():
            return (
                f"{label} {path.name!r} has drifted: {field} reads "
                f"{live.get(field, '')!r}, the table says {want!r}"
            )
    return None


def apply_catalog_twins(
    files: Sequence[Path],
    root: Path,
    table_path: Path = DEFAULT_TABLE_PATH,
    probe: Callable[[Path], TwinProbe] = probe_file,
) -> Tuple[List[Path], TwinReport]:
    """Drop each entry's `retire` file from `files`, or refuse and keep both.

    `probe` is injected so tests run without mutagen and without touching the
    real library — the same reason `build_queue` takes a `pdf_classifier`.

    ⚠️ Every check below is a REFUSAL, not a repair, and every refusal keeps
    BOTH editions. In order:

    1. the table is readable and the entry names both sides;
    2. exactly one walked file matches each side's path (0 or 2+ → refuse);
    3. the two sides are different files (an entry that retires the survivor is
       a typo that would delete the book from the catalogue);
    4. both sides still read back all four `ASSERTED_FIELDS`;
    5. ⚠️ the two sides agree on `title` — that IS the duplication claim. An
       entry whose two files are no longer the same catalogue identity is
       asserting something that is not true any more, whatever its fields say.
    """
    report = TwinReport()
    entries, problem = load_table(table_path)
    if problem:
        report.refused.append(("<table>", problem))
        return list(files), report
    report.entries = len(entries)
    if not entries:
        return list(files), report

    index: Dict[str, List[Path]] = {}
    for f in files:
        index.setdefault(_rel(f, root).casefold(), []).append(f)

    drop: set = set()
    for entry in entries:
        label = str(entry.get("book") or entry.get("_label") or "<unlabelled entry>")
        survivor_spec = entry.get("survivor")
        retire_spec = entry.get("retire")
        if not isinstance(survivor_spec, dict) or not isinstance(retire_spec, dict):
            report.refused.append((label, "entry needs both a 'survivor' and a 'retire' block"))
            continue

        survivor_rel = str(survivor_spec.get("file") or "")
        retire_rel = str(retire_spec.get("file") or "")
        if not survivor_rel or not retire_rel:
            report.refused.append((label, "both sides must name a 'file'"))
            continue
        if survivor_rel.replace("\\", "/").casefold() == retire_rel.replace("\\", "/").casefold():
            report.refused.append((label, "the survivor and the retired file are the same path"))
            continue

        survivor, why = _find(survivor_rel, index)
        if why:
            report.refused.append((label, f"survivor: {why}"))
            continue
        retire, why = _find(retire_rel, index)
        if why:
            # ⚠️ NOT an error worth stopping for on its own: the retired file is
            # already absent from this walk (it may not be on this machine), so
            # the catalogue already shows one row. Say so; change nothing.
            report.refused.append((label, f"retired edition: {why} — nothing to drop"))
            continue

        why = _assert_side("survivor", survivor, survivor_spec, probe)
        if why:
            report.refused.append((label, why))
            continue
        why = _assert_side("retired edition", retire, retire_spec, probe)
        if why:
            report.refused.append((label, why))
            continue

        if str(survivor_spec.get("title", "")).strip() != str(retire_spec.get("title", "")).strip():
            report.refused.append((
                label,
                "the two sides no longer claim the same title — that IS the "
                "duplication claim, so this entry is refused rather than guessed at",
            ))
            continue

        drop.add(retire)
        report.dropped.append(_rel(retire, root))

    if not drop:
        return list(files), report
    return [f for f in files if f not in drop], report
