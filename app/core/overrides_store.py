# app/core/overrides_store.py
# Read/write half of the catalog corrections layer.
#
# app/core/catalog_overrides.py READS scripts/catalog_overrides.json during a
# build and must never fail; this module WRITES it, and must never write
# something that build would silently mis-apply or that
# tests/test_catalog_overrides.py would reject.
#
# Everything here is pure data handling - no prompting, no tag reading, no
# printing - so the CLI (app/tools/edit_overrides.py) stays a thin shell and the
# rules below are testable on their own.
#
# THE ONE INVARIANT: save() validates before it writes and raises rather than
# write an invalid file. Every rule the tests enforce is checked here first, so
# a bad edit fails at the moment it is made, with a message naming the entry -
# not later, in CI, in a file nobody remembers editing.

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.core.catalog_overrides import CORRECTABLE_FIELDS, OVERRIDES_PATH

# Evidence keys that are not per-field notes. Everything else in an evidence
# block is expected to name a field in "set".
EVIDENCE_META_KEYS = ("tags_read", "filename_said", "sources", "note")

# Present on every entry the editor writes. Absent on some hand-written ones,
# so their absence is a warning, not an error.
EVIDENCE_RECOMMENDED = ("tags_read", "filename_said", "sources")


class OverridesError(Exception):
    """A refusal to write. The message names the entry and the broken rule."""


def _norm(s: Optional[str]) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", str(s)).strip().lower()


# --------------------------------------------------------------------------- #
# Load / save
# --------------------------------------------------------------------------- #


def load(path: Path = OVERRIDES_PATH) -> Dict[str, Any]:
    """
    Read the corrections file as-is, key order preserved.

    Unlike catalog_overrides._load() this does NOT swallow errors: the editor
    must refuse to touch a file it cannot parse, or it would overwrite the
    parts it failed to read.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise OverridesError(f"{path} does not exist")
    except json.JSONDecodeError as exc:
        raise OverridesError(f"{path} is not valid JSON ({exc}). Fix it by hand before editing.")
    if not isinstance(data, dict):
        raise OverridesError(f"{path} must hold a JSON object")
    data.setdefault("canonical_series", {})
    data.setdefault("overrides", [])
    return data


def dumps(data: Dict[str, Any]) -> str:
    """The file's exact on-disk formatting: 2-space indent, real UTF-8, LF, final newline."""
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def save(data: Dict[str, Any], path: Path = OVERRIDES_PATH) -> None:
    """
    Validate, then write atomically.

    Atomic because the build reads this file: a half-written file would be
    malformed, and catalog_overrides treats malformed as "no corrections at
    all" - every correction in the library would vanish from the next build
    without a single error message.
    """
    problems = [p for p in validate(data) if p.startswith("ERROR")]
    if problems:
        raise OverridesError("refusing to write an invalid corrections file:\n  " + "\n  ".join(problems))

    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the file's existing line endings. git is configured with
    # autocrlf=true here, so rewriting a CRLF working copy as LF leaves a file
    # git reports as modified with an empty diff - noise on a tracked file the
    # pipeline also commits.
    newline = "\n"
    try:
        if b"\r\n" in path.read_bytes():
            newline = "\r\n"
    except OSError:
        pass

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".catalog_overrides.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as f:
            f.write(dumps(data))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------- #
# Validation - every rule tests/test_catalog_overrides.py enforces, checked here
# first so the editor can never be the thing that breaks it.
# --------------------------------------------------------------------------- #


def _check_match(who: str, match: Any) -> List[str]:
    """Keying: never on filename alone. Filenames drift; ASIN and title+author survive."""
    if not isinstance(match, dict) or not match:
        return [f"ERROR {who}: no match block"]
    problems = []
    if not (match.get("asin") or (match.get("title") and match.get("author"))):
        problems.append(f"ERROR {who}: needs an asin, or a title AND an author (a file key alone drifts)")
    problems += [f"ERROR {who}: unknown match field {key!r}" for key in match if key not in ("asin", "title", "author", "file")]
    return problems


def _check_evidence(who: str, ev: Any, sets: Dict[str, Any]) -> List[str]:
    """One evidence key per corrected field. This is the rule the layer rests on."""
    if not isinstance(ev, dict) or not ev:
        return [f"ERROR {who}: no evidence block"]
    problems = [f"ERROR {who}: 'set' changes {f!r} but evidence says nothing about it" for f in sets if not ev.get(f)]
    problems += [
        f"WARN {who}: evidence mentions {key!r}, which 'set' does not change"
        for key in ev
        if key not in EVIDENCE_META_KEYS and key not in sets
    ]
    problems += [f"WARN {who}: evidence has no {key!r}" for key in EVIDENCE_RECOMMENDED if key not in ev]
    return problems


def _check_entry(who: str, entry: Dict[str, Any]) -> List[str]:
    problems = [
        f"WARN {who}: unknown entry field {key!r} - it will be ignored by the build"
        for key in entry
        if key not in ("book", "match", "set", "added", "updated", "evidence") and not key.startswith("_")
    ]
    problems += _check_match(who, entry.get("match"))

    sets = entry.get("set")
    if not isinstance(sets, dict) or not sets:
        return problems + [f"ERROR {who}: no 'set' block - an entry that changes nothing should be deleted"]
    problems += [
        f"ERROR {who}: {f!r} is not a correctable field ({', '.join(CORRECTABLE_FIELDS)})"
        for f in sets
        if f not in CORRECTABLE_FIELDS
    ]
    if not entry.get("added"):
        problems.append(f"ERROR {who}: no 'added' date")
    return problems + _check_evidence(who, entry.get("evidence"), sets)


def _check_sections(data: Dict[str, Any]) -> List[str]:
    problems: List[str] = []
    canon = data.get("canonical_series")
    if not isinstance(canon, dict):
        problems.append("ERROR canonical_series: must be an object")
    else:
        for variant, canonical in canon.items():
            if variant != _norm(variant):
                problems.append(f"ERROR canonical_series[{variant!r}]: keys are matched lowercased - use {_norm(variant)!r}")
            if not canonical or not isinstance(canonical, str):
                problems.append(f"ERROR canonical_series[{variant!r}]: needs a canonical spelling")

    # _unresolved keys on "item", not "subject". Easy to get wrong, and a test in
    # tests/test_catalog_overrides.py reads u["item"] directly - a wrong key is a
    # KeyError in the suite rather than a readable failure.
    unresolved = data.get("_unresolved", [])
    if not isinstance(unresolved, list):
        return problems + ["ERROR _unresolved: must be a list"]
    problems += [
        f"ERROR _unresolved: every record needs an 'item' key (got {sorted(u) if isinstance(u, dict) else u!r})"
        for u in unresolved
        if not isinstance(u, dict) or not u.get("item")
    ]
    return problems


def validate(data: Dict[str, Any]) -> List[str]:
    """
    Return a list of problems, each prefixed ERROR (blocks a write) or WARN.
    An empty list means the file is clean.
    """
    overrides = data.get("overrides")
    if not isinstance(overrides, list):
        return ["ERROR overrides: must be a list"]

    problems: List[str] = [] if overrides else ["WARN overrides: the list is empty"]
    seen: Dict[Tuple[str, str, str, str], int] = {}
    for i, entry in enumerate(overrides):
        if not isinstance(entry, dict):
            problems.append(f"ERROR entry #{i}: must be an object")
            continue
        who = describe(entry)
        problems += _check_entry(who, entry)

        # Two entries with the same match block: the second can never fire,
        # because find_override() returns the first hit.
        sig = match_signature(entry.get("match") or {})
        if sig in seen:
            problems.append(f"ERROR {who}: same match block as entry #{seen[sig]} - the second one can never fire")
        else:
            seen[sig] = i

    return problems + _check_sections(data)


def describe(entry: Dict[str, Any]) -> str:
    """A short human name for an entry, for messages."""
    m = entry.get("match") or {}
    if entry.get("book"):
        return str(entry["book"])
    if m.get("asin"):
        return f"asin {m['asin']}"
    bits = [str(m.get("title") or "?")]
    if m.get("author"):
        bits.append(f"by {m['author']}")
    if m.get("file"):
        bits.append(f"[{m['file']}]")
    return " ".join(bits)


# --------------------------------------------------------------------------- #
# Entries
# --------------------------------------------------------------------------- #


def build_match(
    asin: Optional[str] = None,
    title: Optional[str] = None,
    author: Optional[str] = None,
    file: Optional[str] = None,
) -> Dict[str, str]:
    """
    Build a match block by the documented preference:
      asin (survives rename AND retag) > title+author (survives rename) > file.

    ⚠️ EVERY field in a match block must match, so extra fields make an entry
    LESS durable, not more. An ASIN-keyed entry therefore carries the asin and
    nothing else: adding the title back would break it on the first retitle,
    which is exactly the drift the ASIN key exists to survive. `file` is a
    tiebreaker for ambiguous titles only, and never a key on its own.
    """
    if asin:
        return {"asin": str(asin).strip()}

    match: Dict[str, str] = {}
    if author:
        match["author"] = str(author).strip()
    if title:
        match["title"] = str(title).strip()
    if not (match.get("title") and match.get("author")):
        raise OverridesError(
            "cannot key this entry: it needs an ASIN, or both a title and an author. "
            "A filename alone is not a key - filenames get renamed by hand and by the sort scripts."
        )
    if file:
        match["file"] = str(file).strip()
    return match


def build_entry(
    match: Dict[str, str],
    sets: Dict[str, Optional[str]],
    why: Dict[str, str],
    tags_read: Optional[Dict[str, Any]] = None,
    filename_said: Optional[str] = None,
    sources: Optional[Iterable[str]] = None,
    note: Optional[str] = None,
    added: Optional[str] = None,
    book: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Assemble one evidence-bearing entry.

    `why` must carry one note per field in `sets`; that is the rule the whole
    layer rests on, so it is enforced here rather than left to the caller.
    """
    if not sets:
        raise OverridesError("nothing to set - an entry that changes nothing should not exist")
    bad = [f for f in sets if f not in CORRECTABLE_FIELDS]
    if bad:
        raise OverridesError(f"not correctable fields: {', '.join(sorted(bad))}. Allowed: {', '.join(CORRECTABLE_FIELDS)}")
    missing = [f for f in sets if not (why.get(f) or "").strip()]
    if missing:
        raise OverridesError(
            "every corrected field needs evidence saying why; missing for: " + ", ".join(sorted(missing))
        )

    evidence: Dict[str, Any] = {f: why[f].strip() for f in sets}
    evidence["tags_read"] = tags_read if tags_read is not None else {}
    evidence["filename_said"] = filename_said if filename_said is not None else ""
    evidence["sources"] = [s.strip() for s in (sources or []) if s and s.strip()]
    if note and note.strip():
        evidence["note"] = note.strip()

    entry: Dict[str, Any] = {}
    # A label, not a key. An ASIN-keyed entry is unreadable otherwise, and the
    # cure for that must not be putting the title back into the match block.
    if book:
        entry["book"] = book.strip()
    entry["match"] = dict(match)
    entry["set"] = {f: ("" if v is None else str(v)) for f, v in sets.items()}
    entry["added"] = added or date.today().isoformat()
    entry["evidence"] = evidence
    return entry


def match_signature(match: Dict[str, Any]) -> Tuple[str, str, str, str]:
    return (_norm(match.get("asin")), _norm(match.get("title")), _norm(match.get("author")), _norm(match.get("file")))


def find_entry(data: Dict[str, Any], match: Dict[str, Any]) -> Optional[int]:
    """Index of the entry with this exact match block, or None."""
    want = match_signature(match)
    for i, entry in enumerate(data.get("overrides") or []):
        if isinstance(entry, dict) and match_signature(entry.get("match") or {}) == want:
            return i
    return None


def entries_for(
    data: Dict[str, Any],
    asin: Optional[str] = None,
    title: Optional[str] = None,
    author: Optional[str] = None,
    filename: Optional[str] = None,
) -> List[int]:
    """
    Indices of every entry that WOULD match this book, in file order. The first
    is the one that actually fires. Used to spot an existing entry before
    writing a second one that could never take effect.
    """
    hits = []
    for i, entry in enumerate(data.get("overrides") or []):
        m = (entry or {}).get("match") or {}
        if m.get("asin") and _norm(m["asin"]) != _norm(asin):
            continue
        if m.get("title") and _norm(m["title"]) != _norm(title):
            continue
        if m.get("author") and not any(_norm(m["author"]) == _norm(p) for p in (author or "").split(",")):
            continue
        if m.get("file") and _norm(m["file"]) != _norm(filename):
            continue
        hits.append(i)
    return hits


def _merge_evidence(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fold a fresh evidence block into an existing one WITHOUT losing research.

    Amending a book's year must not delete the citation that settled its volume
    number last month, so `sources` is a union and an empty new value never
    overwrites a populated old one. `tags_read` is the exception: the new block
    is a real read of the file as it is now, so it replaces the stale one.
    """
    merged = {**old, **{k: v for k, v in new.items() if k not in EVIDENCE_META_KEYS}}

    sources = list(old.get("sources") or [])
    for url in new.get("sources") or []:
        if url not in sources:
            sources.append(url)
    if sources or "sources" in old or "sources" in new:
        merged["sources"] = sources

    for key in ("tags_read", "filename_said", "note"):
        candidate = new.get(key)
        if candidate:
            merged[key] = candidate
        elif key in old:
            merged[key] = old[key]
        elif key in new:
            merged[key] = candidate
    return merged


def upsert(data: Dict[str, Any], entry: Dict[str, Any], merge: bool = True) -> str:
    """
    Add the entry, or update the one with the same match block.

    merge=True keeps corrections already recorded for that book and their
    evidence, so fixing a narrator today does not silently drop the series fix
    made last week. Returns "added" or "updated".
    """
    idx = find_entry(data, entry["match"])
    if idx is None:
        data.setdefault("overrides", []).append(entry)
        return "added"

    if not merge:
        data["overrides"][idx] = entry
        return "updated"

    old = data["overrides"][idx]
    merged_set = {**(old.get("set") or {}), **entry["set"]}
    merged_ev = _merge_evidence(old.get("evidence") or {}, entry["evidence"])
    # Drop evidence for fields no longer being set, so validate() stays quiet.
    for key in list(merged_ev):
        if key not in EVIDENCE_META_KEYS and key not in merged_set:
            del merged_ev[key]
    merged: Dict[str, Any] = {}
    label = entry.get("book") or old.get("book")
    if label:
        merged["book"] = label
    merged["match"] = entry["match"]
    merged["set"] = merged_set
    merged["added"] = old.get("added") or entry["added"]
    if entry["added"] != merged["added"]:
        merged["updated"] = entry["added"]
    merged["evidence"] = merged_ev
    data["overrides"][idx] = merged
    return "updated"


def remove(data: Dict[str, Any], index: int) -> Dict[str, Any]:
    """Drop an entry by index and return it."""
    return data["overrides"].pop(index)


def simulate(
    data: Dict[str, Any],
    row: Dict[str, Optional[str]],
    path: Optional[Path] = None,
    asin: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """
    Run the REAL corrections layer against proposed data, without touching the
    real file, and put the layer back as it was.

    This is what lets the editor prove an entry works before writing it. An
    entry can be valid, well-evidenced and keyed on a value the build never
    sees - which looks like success everywhere except in the catalog.
    """
    from app.core import catalog_overrides as co

    fd, tmp = tempfile.mkstemp(prefix="overrides_sim_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(dumps(data))
        co.reload_overrides(Path(tmp))
        return co.apply_overrides(dict(row), path=path, asin=asin)
    finally:
        co.reload_overrides()
        Path(tmp).unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# The other sections
# --------------------------------------------------------------------------- #


def set_canonical_series(data: Dict[str, Any], variant: str, canonical: str) -> None:
    """
    Fold a variant spelling onto the canonical one, for every book.

    Keys are stored lowercased because that is how they are looked up; a
    capitalised key is dead weight that looks like it works.
    """
    if not variant or not canonical:
        raise OverridesError("canonical_series needs both a variant and a canonical spelling")
    canon = data.setdefault("canonical_series", {})
    canon[_norm(variant)] = canonical.strip()
    # Also fold the canonical form onto itself, so a later respelling of the
    # canonical name cannot orphan the books already spelled correctly.
    canon.setdefault(_norm(canonical), canonical.strip())


def add_unresolved(
    data: Dict[str, Any],
    item: str,
    question: str,
    where: str = "",
    status: str = "",
    raised: Optional[str] = None,
) -> None:
    """
    Record something deliberately NOT corrected because the answer is unknown.

    Keyed on `item` - tests/test_catalog_overrides.py reads u["item"] directly.
    """
    if not item or not question:
        raise OverridesError("_unresolved needs an 'item' and a 'question'")
    record = {
        "item": item.strip(),
        "where": where.strip(),
        "question": question.strip(),
        "status": status.strip()
        or "UNRESOLVED - deliberately not corrected. Guessing would look like a researched answer, and nobody re-checks a value once it is written down.",
        "raised": raised or date.today().isoformat(),
    }
    records = data.setdefault("_unresolved", [])
    for i, existing in enumerate(records):
        if isinstance(existing, dict) and _norm(existing.get("item")) == _norm(item):
            records[i] = record
            return
    records.append(record)
