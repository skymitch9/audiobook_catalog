"""Survey — and, on request, repair — series/volume tags across the whole library.

    PREPARED, NOT RUN.  As of 2026-08-11 this tool has never been executed against
    C:/Users/nbasl/OpenAudible/books. It was written to be run deliberately, later,
    starting with the survey.

The catalog reads series/volume from m4b tags (SRNM/SRSQ, then free-form, then
title parsing — see app/metadata.py). Much of this library was tagged by hand over
several years, so those tags carry hand-entry damage: blanks, one series spelled
four ways, a series field holding the book title. This tool finds that damage and,
separately and only when told to, fixes it at source.

Two halves, in this order, on purpose:

  1. SURVEY (the default, read-only).  "Here is what is blank or inconsistent
     across all 422 author folders."  That report is what makes the repair a
     decision rather than a leap.
  2. REPAIR (--commit).  Writes SRNM/SRSQ back into the files, after a backup.

Rules this tool will not break
------------------------------
* **Dry run by default.** Nothing is ever written without an explicit --commit.
* **It never invents a volume number.** A number is only written when it comes
  from the curated corrections file or from the file's own trkn atom, and only
  when nothing contradicts it. Contradictions are reported, never resolved.
* **Filenames are evidence, not truth.** Filename parsing feeds the report only.
  No value parsed from a filename is ever written to a tag. This is not caution
  for its own sake: across the Completionist Chronicles the trkn tags were right
  on all 14 books and the filenames were the damaged source.
* **Only SRNM and SRSQ are ever written.** Titles, authors, narrators, dates and
  cover art are never touched. Those are corrected in the catalog instead, via
  scripts/catalog_overrides.json, which needs no write to irreplaceable media.

Rollback story
--------------
These are purchased audiobooks and there is no re-download for the hand-made
files, so every write is reversible:

* Before the first byte is written, the complete prior value of every atom the
  run may touch is recorded — including which atoms were ABSENT, so a restore can
  delete them again — to ``output_files/tag_sweep/<stamp>/backup.jsonl``. The
  backup is flushed and fsynced and then read back and verified before any file
  is opened for writing. If the backup cannot be written or verified, the run
  aborts having changed nothing.
* Every write is verified by reopening the file and reading the values back. A
  file that does not read back correctly is restored immediately from its own
  in-memory backup record and reported as failed.
* A whole run is undone with ``--restore output_files/tag_sweep/<stamp>``.
* ``--safe-copy`` upgrades this from "tags are recoverable" to "the container
  cannot be damaged at all": each file is copied, the copy is tagged, and the
  copy replaces the original with an atomic ``os.replace``. It costs one extra
  copy of the largest file in free space and roughly doubles the IO. Without it,
  mutagen edits the metadata atom in place — the audio stream is not re-encoded,
  but a crash mid-write could still leave a torn container, and backup.jsonl
  restores tags, not a torn container.

Usage
-----
    python -m app.tools.audit_series_tags                      # survey, read-only
    python -m app.tools.audit_series_tags --author "Dakota Krout"
    python -m app.tools.audit_series_tags --plan               # show proposed writes
    python -m app.tools.audit_series_tags --plan --from-overrides-only
    python -m app.tools.audit_series_tags --commit --from-overrides-only --safe-copy
    python -m app.tools.audit_series_tags --restore output_files/tag_sweep/20260811_120000
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from mutagen.mp4 import MP4

from app.config import EXTS, OUTPUT_DIR, ROOT_DIR
from app.core.catalog_overrides import canonicalize_series, find_override
from app.core.index_utils import normalize_index
from app.metadata import (
    FREEFORM_HINTS,
    K_ARTIST,
    K_ASIN,
    K_DAY,
    K_INDEX_VENDOR,
    K_SERIES_VENDOR,
    K_TITLE,
    K_WRITER,
    get_freeform_by_suffix,
    get_tag_any,
)
from app.parsers.title import parse_series_and_index_from_title

K_ALBUM = "\xa9alb"
K_TRACK = "trkn"

# The only atoms this tool will ever write, and therefore the only atoms it needs
# to back up. Keep these two lists in step.
WRITABLE_ATOMS = (K_SERIES_VENDOR, K_INDEX_VENDOR)
BACKED_UP_ATOMS = (K_SERIES_VENDOR, K_INDEX_VENDOR, K_ALBUM, K_TRACK)


# --------------------------------------------------------------------------- #
# Issue codes
# --------------------------------------------------------------------------- #

ISSUES = {
    "NO_TAGS": "the file has no MP4 tag block at all",
    "SERIES_BLANK": "no series anywhere - not in SRNM, free-form or the album tag",
    "SERIES_ONLY_IN_ALBUM": "SRNM is empty but the album tag holds the series (recoverable)",
    "SERIES_IS_TITLE": "the series field holds the book title (the Uncapped defect)",
    "SERIES_SPELLING": "series name is a non-canonical spelling of a known series",
    "SERIES_VARIANTS_IN_LIBRARY": "this series is spelled more than one way across the library",
    "INDEX_BLANK": "a series is known but there is no volume number anywhere",
    "INDEX_ONLY_IN_TRACK": "SRSQ is empty but trkn holds the volume (recoverable)",
    "INDEX_CONFLICT": "two tag sources disagree about the volume - NEEDS A DECISION",
    "FILENAME_SERIES_DIFFERS": "the filename's series disagrees with the tag (tag preferred)",
    "FILENAME_INDEX_DIFFERS": "the filename's volume disagrees with the tag (tag preferred)",
    "DUPLICATE_VOLUME": "two books in one series claim the same volume - NEEDS A DECISION",
    "SERIES_GAP": "the series run has missing volumes (may simply be unowned books)",
    "CATALOG_SERIES_BLANK": "whatever the tags hold, the catalog ends up with no series",
}

# Issues that must never be auto-repaired — a human decides these.
NEEDS_DECISION = {"INDEX_CONFLICT", "DUPLICATE_VOLUME", "SERIES_IS_TITLE"}


# --------------------------------------------------------------------------- #
# Filename parsing — EVIDENCE ONLY. Nothing parsed here is ever written.
# --------------------------------------------------------------------------- #

_FN_PATTERNS = (
    # Dakota Krout - [The Completionist Chronicles - 11] - Thunderplump (Luke Daniels)
    re.compile(r"\[(?P<series>[^\]]+?)\s*-\s*(?P<idx>\d+(?:\.\d+)?)\]"),
    # Ritualist - Completionist Chronicles, Book 1
    re.compile(r"-\s*(?P<series>[^-(\[]+?),\s*Book\s*(?P<idx>\d+(?:\.\d+)?)", re.IGNORECASE),
    # Anima - A Divine Dungeon Series (Artorian's Archives, Book 6)
    re.compile(r"\((?P<series>[^)]+?),\s*Book\s*(?P<idx>\d+(?:\.\d+)?)\)", re.IGNORECASE),
)


def parse_filename(stem: str) -> Tuple[Optional[str], Optional[str]]:
    """Best-effort series/index from a filename stem. Evidence for the report only."""
    for pat in _FN_PATTERNS:
        m = pat.search(stem)
        if m:
            series = re.sub(r"\s{2,}", " ", m.group("series")).strip(" -–—:,")
            return (series or None, normalize_index(m.group("idx")))
    return (None, None)


# --------------------------------------------------------------------------- #
# Scanning
# --------------------------------------------------------------------------- #


def _track_number(tags: Dict[str, Any]) -> Optional[str]:
    """trkn is [(track, total)]. Return the track as a string, or None if 0/absent."""
    val = tags.get(K_TRACK)
    if not val:
        return None
    try:
        n = val[0][0]
    except Exception:
        return None
    return str(n) if n else None


def _looks_like_series(name: Optional[str]) -> bool:
    return bool(name and len(name.strip()) >= 3)


@dataclass
class Scan:
    path: Path
    author_folder: str
    # raw tags
    title: Optional[str] = None
    album: Optional[str] = None
    artist: Optional[str] = None
    narrator: Optional[str] = None
    year: Optional[str] = None
    asin: Optional[str] = None
    srnm: Optional[str] = None
    srsq: Optional[str] = None
    trkn: Optional[str] = None
    ff_series: Optional[str] = None
    ff_index: Optional[str] = None
    # derived
    catalog_series: Optional[str] = None
    catalog_index: Optional[str] = None
    file_series: Optional[str] = None
    file_index: Optional[str] = None
    override_series: Optional[str] = None
    override_index: Optional[str] = None
    issues: List[str] = field(default_factory=list)
    proposal: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    @property
    def rel(self) -> str:
        try:
            return str(self.path.relative_to(ROOT_DIR)).replace("\\", "/")
        except ValueError:
            return str(self.path)


def scan_file(path: Path) -> Scan:
    """Read one file. Pure read — opens the container, writes nothing."""
    s = Scan(path=path, author_folder=path.parent.name)
    try:
        audio = MP4(str(path))
    except Exception as exc:  # unreadable container
        s.error = f"{type(exc).__name__}: {exc}"
        s.issues.append("NO_TAGS")
        return s

    tags = audio.tags or {}
    if not tags:
        s.issues.append("NO_TAGS")

    s.title = get_tag_any(tags, [K_TITLE])
    s.album = get_tag_any(tags, [K_ALBUM])
    s.artist = get_tag_any(tags, [K_ARTIST])
    s.narrator = get_tag_any(tags, [K_WRITER])
    s.year = get_tag_any(tags, [K_DAY])
    s.asin = get_tag_any(tags, [K_ASIN])
    s.srnm = get_tag_any(tags, [K_SERIES_VENDOR])
    s.srsq = get_tag_any(tags, [K_INDEX_VENDOR])
    s.trkn = _track_number(tags)
    s.ff_series = get_freeform_by_suffix(tags, FREEFORM_HINTS["series"])
    s.ff_index = get_freeform_by_suffix(tags, FREEFORM_HINTS["series_index"])

    # What the catalog build currently produces, using the same precedence as
    # app/metadata.py (vendor tags, then free-form, then title parsing).
    series = s.srnm or s.ff_series
    index = s.srsq or (normalize_index(s.ff_index) if s.ff_index else None)
    if not series or not index:
        ts, ti = parse_series_and_index_from_title(s.title or "")
        series = series or ts
        index = index or (normalize_index(ti) if ti else None)
    s.catalog_series = canonicalize_series(series)
    s.catalog_index = index

    # Evidence, never written back.
    s.file_series, s.file_index = parse_filename(path.stem)

    entry = find_override(title=s.title, author=s.artist, filename=path.name, asin=s.asin)
    if entry:
        s.override_series = entry["set"].get("series")
        s.override_index = entry["set"].get("series_index")

    _classify(s)
    return s


def _classify(s: Scan) -> None:
    """Attach issue codes. Per-file only; cross-file checks run later."""
    tag_series = s.srnm or s.ff_series
    tag_index = s.srsq or s.ff_index

    if not tag_series:
        if _looks_like_series(s.album) and s.album != s.title:
            s.issues.append("SERIES_ONLY_IN_ALBUM")
        elif not s.catalog_series:
            s.issues.append("SERIES_BLANK")

    # The Uncapped signature: the series slot holds the book's own title.
    for candidate in (s.srnm, s.album):
        if candidate and s.title and candidate.strip().lower() == s.title.strip().lower():
            s.issues.append("SERIES_IS_TITLE")
            break

    if tag_series and canonicalize_series(tag_series) != tag_series:
        s.issues.append("SERIES_SPELLING")

    known_series = tag_series or s.album or s.override_series
    if known_series and not tag_index:
        if s.trkn:
            s.issues.append("INDEX_ONLY_IN_TRACK")
        else:
            s.issues.append("INDEX_BLANK")

    # Two tag sources disagreeing is a decision, not a repair.
    if s.srsq and s.trkn and normalize_index(s.srsq) != normalize_index(s.trkn):
        s.issues.append("INDEX_CONFLICT")

    if s.file_series and tag_series and canonicalize_series(s.file_series) != canonicalize_series(tag_series):
        s.issues.append("FILENAME_SERIES_DIFFERS")
    if s.file_index and (s.srsq or s.trkn):
        tagged = normalize_index(s.srsq or s.trkn or "")
        if tagged and s.file_index != tagged:
            s.issues.append("FILENAME_INDEX_DIFFERS")

    if not s.catalog_series and not s.override_series:
        s.issues.append("CATALOG_SERIES_BLANK")


# --------------------------------------------------------------------------- #
# Cross-file checks
# --------------------------------------------------------------------------- #


def _effective_index(s: Scan) -> str:
    """The volume this book will end up with: the correction if there is one."""
    idx = s.override_index if s.override_index is not None else s.catalog_index
    return str(idx or "")


def _flag(s: Scan, code: str) -> None:
    if code not in s.issues:
        s.issues.append(code)


def _group_by_series(scans: List[Scan]) -> Tuple[Dict[str, List[Scan]], Dict[str, set]]:
    """Bucket books by their final series name, collecting every spelling seen."""
    by_series: Dict[str, List[Scan]] = defaultdict(list)
    spellings: Dict[str, set] = defaultdict(set)
    for s in scans:
        name = s.override_series or s.catalog_series
        if not name:
            continue
        by_series[name].append(s)
        for raw in (s.srnm, s.ff_series, s.album, s.file_series):
            if raw and canonicalize_series(raw) == name:
                spellings[name].add(raw)
    return by_series, spellings


def _find_duplicates(group: List[Scan]) -> Dict[str, List[str]]:
    """Volumes claimed by more than one book. Reported, never auto-resolved."""
    seen: Dict[str, List[str]] = defaultdict(list)
    for s in group:
        idx = _effective_index(s)
        if idx:
            seen[idx].append(s.path.name)
    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    for s in group:
        if _effective_index(s) in dupes:
            _flag(s, "DUPLICATE_VOLUME")
    return dupes


def _find_gaps(group: List[Scan]) -> List[int]:
    """Missing whole numbers inside the run. Informational - may be unowned books."""
    wholes = sorted({int(float(i)) for s in group if re.fullmatch(r"\d+(?:\.0)?", (i := _effective_index(s)))})
    if len(wholes) < 2:
        return []
    return [n for n in range(wholes[0], wholes[-1] + 1) if n not in wholes]


def cross_check(scans: List[Scan]) -> Dict[str, Any]:
    """Duplicate volumes, gaps, and one series spelled several ways."""
    by_series, spellings = _group_by_series(scans)
    duplicates: Dict[str, Dict[str, List[str]]] = {}
    gaps: Dict[str, List[int]] = {}
    variants: Dict[str, List[str]] = {}

    for name, group in by_series.items():
        dupes = _find_duplicates(group)
        if dupes:
            duplicates[name] = dupes

        missing = _find_gaps(group)
        if missing:
            gaps[name] = missing

        if len(spellings[name]) > 1:
            variants[name] = sorted(spellings[name])
            for s in group:
                _flag(s, "SERIES_VARIANTS_IN_LIBRARY")

    return {"duplicate_volumes": duplicates, "series_gaps": gaps, "series_spelling_variants": variants}


# --------------------------------------------------------------------------- #
# Proposals — what a repair WOULD write
# --------------------------------------------------------------------------- #


def propose(s: Scan, from_overrides_only: bool = False) -> Optional[Dict[str, Any]]:
    """
    Decide the SRNM/SRSQ a repair would write, or None to leave the file alone.

    Precedence, and the reason for each rung:
      1. scripts/catalog_overrides.json — curated, evidence-bearing, cited.
      2. the album tag / the trkn atom — the file's own data, which the pipeline
         happens not to read. Only used when nothing contradicts it.
      3. the filename — NEVER. It is reported and otherwise ignored.

    Anything in NEEDS_DECISION suppresses the proposal for that field.
    """
    writes: Dict[str, str] = {}
    sources: Dict[str, str] = {}

    # --- series ---
    if s.override_series:
        if s.srnm != s.override_series:
            writes[K_SERIES_VENDOR] = s.override_series
            sources[K_SERIES_VENDOR] = "catalog_overrides.json"
    elif not from_overrides_only:
        if not s.srnm and "SERIES_IS_TITLE" not in s.issues:
            candidate = s.album or s.ff_series
            if _looks_like_series(candidate) and candidate != s.title:
                writes[K_SERIES_VENDOR] = canonicalize_series(candidate)
                sources[K_SERIES_VENDOR] = "album tag (©alb)"
        elif s.srnm and canonicalize_series(s.srnm) != s.srnm:
            writes[K_SERIES_VENDOR] = canonicalize_series(s.srnm)
            sources[K_SERIES_VENDOR] = "canonical_series normalisation"

    # --- volume ---
    if s.override_index:
        if s.srsq != s.override_index:
            writes[K_INDEX_VENDOR] = s.override_index
            sources[K_INDEX_VENDOR] = "catalog_overrides.json"
    elif not from_overrides_only:
        if not s.srsq and s.trkn and "INDEX_CONFLICT" not in s.issues:
            # The filename disagreeing does not veto the tag — the tag wins — but it
            # does mean a human should see it, so say so rather than writing quietly.
            if s.file_index and s.file_index != normalize_index(s.trkn):
                sources["_note"] = f"filename says {s.file_index}, trkn says {s.trkn}; wrote trkn, filename is suspect"
            writes[K_INDEX_VENDOR] = normalize_index(s.trkn)
            sources[K_INDEX_VENDOR] = "trkn atom"

    if not writes:
        return None

    # A NEEDS_DECISION issue blocks the write — unless every value came from the
    # corrections file, because that file IS the human decision, made once, with
    # its evidence and citations written down. Uncapped is exactly this case: the
    # album tag holds the title (SERIES_IS_TITLE), and the corrections file is
    # what resolves it.
    curated = all(src == "catalog_overrides.json" for atom, src in sources.items() if atom in writes)
    blocked = [] if curated else sorted(set(s.issues) & NEEDS_DECISION)
    return {"writes": writes, "sources": sources, "blocked_by": blocked, "curated": curated}


# --------------------------------------------------------------------------- #
# Backup and restore
# --------------------------------------------------------------------------- #


def _atom_snapshot(path: Path) -> Dict[str, Any]:
    """Everything needed to put this file's tags back exactly as they were."""
    audio = MP4(str(path))
    tags = audio.tags or {}
    present: Dict[str, Any] = {}
    absent: List[str] = []
    for atom in BACKED_UP_ATOMS:
        if atom in tags:
            val = tags[atom]
            present[atom] = [list(v) if isinstance(v, tuple) else v for v in val]
        else:
            absent.append(atom)
    st = path.stat()
    return {"path": str(path), "size": st.st_size, "mtime": st.st_mtime, "atoms": present, "absent": absent}


def write_backup(records: List[Dict[str, Any]], run_dir: Path) -> Path:
    """Write, fsync and verify the backup. Raises if it cannot be trusted."""
    run_dir.mkdir(parents=True, exist_ok=True)
    backup = run_dir / "backup.jsonl"
    with open(backup, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())

    # Read it back before trusting it.
    with open(backup, "r", encoding="utf-8") as f:
        read_back = [json.loads(line) for line in f if line.strip()]
    if len(read_back) != len(records):
        raise RuntimeError(f"backup verification failed: wrote {len(records)} records, read {len(read_back)}")
    return backup


def _apply_atoms(path: Path, atoms: Dict[str, Any], delete: Iterable[str] = (), safe_copy: bool = False) -> None:
    """Set/delete atoms on `path`. With safe_copy, tag a copy and atomically swap."""
    target = path
    tmp: Optional[Path] = None
    if safe_copy:
        tmp = path.with_suffix(path.suffix + ".sweeptmp")
        shutil.copy2(path, tmp)
        target = tmp

    audio = MP4(str(target))
    if audio.tags is None:
        audio.add_tags()
    for atom in delete:
        audio.tags.pop(atom, None)
    for atom, value in atoms.items():
        audio.tags[atom] = value if isinstance(value, list) else [value]
    audio.save()

    if tmp is not None:
        os.replace(tmp, path)


def restore_run(run_dir: Path, safe_copy: bool = False) -> int:
    """Undo a --commit run. Returns the number of files put back."""
    backup = Path(run_dir) / "backup.jsonl"
    if not backup.is_file():
        print(f"[FAIL] no backup.jsonl in {run_dir}", file=sys.stderr)
        return 1

    restored = failed = 0
    with open(backup, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            path = Path(rec["path"])
            if not path.is_file():
                print(f"  [missing] {path}")
                failed += 1
                continue
            try:
                atoms = {
                    atom: [tuple(v) if isinstance(v, list) else v for v in vals] if atom == K_TRACK else vals
                    for atom, vals in rec["atoms"].items()
                }
                _apply_atoms(path, atoms, delete=rec.get("absent", []), safe_copy=safe_copy)
                restored += 1
                print(f"  [restored] {path.name}")
            except Exception as exc:
                failed += 1
                print(f"  [FAIL] {path.name}: {exc}", file=sys.stderr)

    print(f"\nrestored {restored}, failed {failed}")
    return 0 if failed == 0 else 1


# --------------------------------------------------------------------------- #
# Repair
# --------------------------------------------------------------------------- #


def repair(scans: List[Scan], run_dir: Path, safe_copy: bool) -> int:
    """Write the proposals. Backup first, verify every write, roll back failures."""
    todo = [s for s in scans if s.proposal and not s.proposal["blocked_by"]]
    if not todo:
        print("nothing to write.")
        return 0

    print(f"backing up {len(todo)} files ...")
    records = [_atom_snapshot(s.path) for s in todo]
    backup = write_backup(records, run_dir)
    print(f"  backup verified: {backup}")
    print(f"  undo with: python -m app.tools.audit_series_tags --restore {run_dir}\n")

    by_path = {rec["path"]: rec for rec in records}
    written = failed = 0
    for s in todo:
        atoms = {atom: [value] for atom, value in s.proposal["writes"].items()}
        try:
            _apply_atoms(s.path, atoms, safe_copy=safe_copy)
            after = MP4(str(s.path)).tags or {}
            for atom, value in s.proposal["writes"].items():
                got = get_tag_any(after, [atom])
                if got != value:
                    raise RuntimeError(f"{atom} read back as {got!r}, expected {value!r}")
            written += 1
            summary = ", ".join(f"{a}={v!r}" for a, v in s.proposal["writes"].items())
            print(f"  [written] {s.path.name}  {summary}")
        except Exception as exc:
            failed += 1
            print(f"  [FAIL] {s.path.name}: {exc}", file=sys.stderr)
            rec = by_path[str(s.path)]
            try:
                _apply_atoms(s.path, rec["atoms"], delete=rec.get("absent", []), safe_copy=safe_copy)
                print("         rolled back this file from its backup record", file=sys.stderr)
            except Exception as rexc:
                print(f"         ROLLBACK ALSO FAILED: {rexc} — restore from {backup}", file=sys.stderr)

    print(f"\nwrote {written}, failed {failed}")
    return 0 if failed == 0 else 1


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def print_survey(scans: List[Scan], cross: Dict[str, Any], show_clean: bool = False) -> None:
    total = len(scans)
    counts = Counter(code for s in scans for code in set(s.issues))
    clean = sum(1 for s in scans if not s.issues)
    folders = len({s.author_folder for s in scans})

    print("=" * 100)
    print(f"SERIES / VOLUME TAG SURVEY   {total} files across {folders} author folders")
    print(f"library root: {ROOT_DIR}")
    print("=" * 100)
    print(f"\n  clean: {clean}   with at least one issue: {total - clean}\n")

    print(f"  {'issue':<32} {'files':>6}   what it means")
    print(f"  {'-' * 32} {'-' * 6}   {'-' * 52}")
    for code, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        flag = " *" if code in NEEDS_DECISION else "  "
        print(f"{flag}{code:<32} {counts[code]:>6}   {ISSUES.get(code, '')}")
    print("\n  * = must be decided by a human; --commit will never touch these.\n")

    if cross["series_spelling_variants"]:
        print("-" * 100)
        print("SERIES SPELLED MORE THAN ONE WAY")
        for name, variants in sorted(cross["series_spelling_variants"].items()):
            print(f"  {name}")
            for v in variants:
                print(f"      {v!r}")
        print()

    if cross["duplicate_volumes"]:
        print("-" * 100)
        print("DUPLICATE VOLUMES  - reported, never auto-resolved")
        for name, dupes in sorted(cross["duplicate_volumes"].items()):
            for idx, files in sorted(dupes.items()):
                print(f"  {name} #{idx}")
                for fn in files:
                    print(f"      {fn}")
        print()

    if cross["series_gaps"]:
        print("-" * 100)
        print("GAPS IN A SERIES RUN  - informational; may simply be books not owned")
        for name, missing in sorted(cross["series_gaps"].items()):
            print(f"  {name:<50} missing {missing}")
        print()

    print("-" * 100)
    print("PER-FILE DETAIL")
    for s in sorted(scans, key=lambda x: (x.author_folder.lower(), x.path.name.lower())):
        if not s.issues and not show_clean:
            continue
        print(f"\n  {s.author_folder} / {s.path.name}")
        print(f"      issues     : {', '.join(sorted(set(s.issues))) or 'none'}")
        print(f"      tags       : SRNM={s.srnm!r} SRSQ={s.srsq!r} album={s.album!r} trkn={s.trkn!r}")
        print(f"      filename   : series={s.file_series!r} vol={s.file_index!r}   (evidence only)")
        print(f"      catalog now: series={s.catalog_series!r} vol={s.catalog_index!r}")
        if s.override_series or s.override_index:
            print(f"      override   : series={s.override_series!r} vol={s.override_index!r}")
        if s.error:
            print(f"      ERROR      : {s.error}")


def print_plan(scans: List[Scan]) -> None:
    proposed = [s for s in scans if s.proposal]
    writable = [s for s in proposed if not s.proposal["blocked_by"]]
    blocked = [s for s in proposed if s.proposal["blocked_by"]]

    print("\n" + "=" * 100)
    print(f"PLAN  - {len(writable)} files would be written, {len(blocked)} blocked pending a decision")
    print("=" * 100)
    for s in writable:
        print(f"\n  {s.author_folder} / {s.path.name}")
        for atom, value in s.proposal["writes"].items():
            before = s.srnm if atom == K_SERIES_VENDOR else s.srsq
            print(f"      {atom}: {before!r} -> {value!r}   [{s.proposal['sources'].get(atom)}]")
        if s.proposal["sources"].get("_note"):
            print(f"      note: {s.proposal['sources']['_note']}")
    for s in blocked:
        print(f"\n  [BLOCKED] {s.author_folder} / {s.path.name}")
        print(f"      needs a decision: {', '.join(s.proposal['blocked_by'])}")
    print()


def report_payload(scans: List[Scan], cross: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "library_root": str(ROOT_DIR),
        "files_scanned": len(scans),
        "author_folders": len({s.author_folder for s in scans}),
        "issue_counts": dict(Counter(code for s in scans for code in set(s.issues))),
        "cross_file": cross,
        "files": [
            {
                "path": s.rel,
                "author_folder": s.author_folder,
                "issues": sorted(set(s.issues)),
                "tags": {"SRNM": s.srnm, "SRSQ": s.srsq, "album": s.album, "trkn": s.trkn, "title": s.title},
                "filename_evidence": {"series": s.file_series, "index": s.file_index},
                "catalog_now": {"series": s.catalog_series, "index": s.catalog_index},
                "override": {"series": s.override_series, "index": s.override_index},
                "proposal": s.proposal,
                "error": s.error,
            }
            for s in scans
            if s.issues or s.proposal
        ],
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def collect_files(root: Path, author: Optional[str], limit: Optional[int]) -> List[Path]:
    exts = set(EXTS)
    files = [p for p in sorted(root.rglob("*")) if p.is_file() and p.suffix.lower() in exts]
    files = [p for p in files if not p.name.startswith("Copy of ")]
    if author:
        needle = author.lower()
        files = [p for p in files if needle in p.parent.name.lower()]
    if limit:
        files = files[:limit]
    return files


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m app.tools.audit_series_tags",
        description="Survey (default) and optionally repair series/volume tags across the library.",
        epilog="Dry run by default. Nothing is written without --commit.",
    )
    p.add_argument("--plan", action="store_true", help="also show the exact tag writes a repair would make")
    p.add_argument("--commit", action="store_true", help="ACTUALLY WRITE TAGS. Backs up first; see --restore")
    p.add_argument("--restore", metavar="RUN_DIR", help="undo a previous --commit run from its backup.jsonl")
    p.add_argument(
        "--from-overrides-only",
        action="store_true",
        help="only write values sourced from scripts/catalog_overrides.json (the safest first sweep)",
    )
    p.add_argument("--safe-copy", action="store_true", help="tag a copy and atomically swap it in; costs space and IO")
    p.add_argument("--author", metavar="NAME", help="restrict to author folders containing NAME")
    p.add_argument("--series", metavar="NAME", help="restrict the report to one series")
    p.add_argument("--limit", type=int, metavar="N", help="stop after N files (use this for a first pass)")
    p.add_argument("--show-clean", action="store_true", help="include files with no issues in the per-file detail")
    p.add_argument("--report-json", metavar="PATH", help="where to write the machine-readable report")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.restore:
        return restore_run(Path(args.restore), safe_copy=args.safe_copy)

    root = Path(ROOT_DIR)
    if not root.is_dir():
        print(f"[FAIL] library root does not exist: {root}", file=sys.stderr)
        return 1

    files = collect_files(root, args.author, args.limit)
    if not files:
        print("no audio files matched.")
        return 0

    print(f"scanning {len(files)} files under {root} ...", file=sys.stderr)
    scans = [scan_file(p) for p in files]
    cross = cross_check(scans)

    if args.series:
        needle = args.series.lower()
        scans = [s for s in scans if needle in ((s.override_series or s.catalog_series or "").lower())]

    for s in scans:
        s.proposal = propose(s, from_overrides_only=args.from_overrides_only)

    print_survey(scans, cross, show_clean=args.show_clean)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = OUTPUT_DIR / "tag_sweep" / stamp
    report_path = Path(args.report_json) if args.report_json else run_dir / "report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report_payload(scans, cross), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nreport written: {report_path}")

    if args.plan or args.commit:
        print_plan(scans)

    if not args.commit:
        if any(s.proposal for s in scans):
            print("DRY RUN - nothing was written. Re-run with --commit to apply.\n")
        return 0

    if not args.from_overrides_only:
        # DISARMED 2026-08-11 by owner decision, after the full-library dry run.
        # The uncurated repair path proposed 128 writes of which 7 were plausible:
        # 108 would have written the book's own title into the series slot, and 91
        # would have written SRSQ=1 from a trkn that means "track 1 of 1" — three of
        # them over a filename volume that was demonstrably correct.
        #
        # Both bugs are real and are described in docs/info/catalog-corrections.md
        # §8.2. Neither is fixed. This refusal exists because the failure is silent
        # and the files are irreplaceable: a plausible-looking --commit is exactly
        # how this damages a library, and "the tool ran clean" is what it looks like.
        #
        # To revive it: fix the prefix guard (line ~408) and the trkn=1 rule
        # (line ~423), re-run --plan across the full library, and delete this block
        # only once the plan reads correctly.
        print("=" * 100)
        print("REFUSING TO COMMIT - the uncurated repair path is disarmed", file=sys.stderr)
        print("=" * 100)
        print(
            "\n  The full-library dry run (2026-08-11) proposed 128 writes.\n"
            "  Measured: 7 plausible, 108 that write the title into the series slot,\n"
            "  91 that write SRSQ=1 from a track number meaning 'track 1 of 1'.\n\n"
            "  Read docs/info/catalog-corrections.md section 8.2 before changing this.\n\n"
            "  --plan and the read-only survey still work, and are still useful.\n"
            "  --commit --from-overrides-only still works: the corrections file is\n"
            "  researched by hand, and that path is what wrote the 13 good files.\n",
            file=sys.stderr,
        )
        return 2

    print("=" * 100)
    print("COMMIT - writing tags to real audiobook files (curated entries only)")
    print("=" * 100)
    return repair(scans, run_dir, safe_copy=args.safe_copy)


if __name__ == "__main__":
    raise SystemExit(main())
