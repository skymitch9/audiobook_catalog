# app/core/universes.py
# The shared fictional-universe list, as this pipeline reads it.
#
# THE DATA IS NOT IN THIS REPO. It lives at catalog-platform/data/universes.json
# and is shared with bookbuddy/library_catalog. It is keyed on series + author,
# which both catalogs can compute, and the same series exists in both
# collections under different rows - often in only one of them. Do NOT copy it
# in here; a copy is how two lists drift.
#
# Shape follows app/core/catalog_overrides.py deliberately: a JSON file the
# build consults, loaded once at import, reloadable for tests, and a NO-OP when
# it is missing or malformed. That last rule is the important one - this
# pipeline runs unattended three times a day and must never die over reference
# data. It warns loudly instead, once, and carries on with no universes.
#
# library_catalog makes the OPPOSITE choice and FAILS ITS BUILD, because a
# Worker bundled with an empty list would answer "no universe" to everything
# forever and look like a data problem months later. Two failure modes, two
# answers. Do not "fix" one to match the other.
#
# THIS IS ONE OF TWO IMPLEMENTATIONS of the lookup. The other is
# library_catalog/packages/universes/src/lookup.ts. There is no shared runtime
# between a Python static build and a Cloudflare Worker, so there is no shared
# implementation - catalog-platform/data/universes.fixtures.json is the whole
# contract, and both repos run it. This estate has already shipped that class of
# bug once: resolve_author_link (Python) and _resolveAuthorFolder (JS) split
# author strings identically until they did not, and a promote failed silently.

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# The env var that overrides path discovery. Named in every failure message.
ENV_VAR = "CATALOG_PLATFORM_DIR"

# Relative to this repo's root, in the order they are tried. The second is the
# real layout on this machine (both repos sit under vs-code-repos/).
_CANDIDATES = (
    Path("..") / "catalog-platform",
    Path("..") / ".." / "catalog-platform",
    Path("..") / ".." / ".." / "catalog-platform",
)

SCHEMA_VERSION = 1

# Curly quotes -> straight. See _norm().
_CURLY_APOSTROPHES = re.compile("[‘’ʼ′]")
_CURLY_QUOTES = re.compile("[“”]")
_WHITESPACE = re.compile(r"\s+")


def _norm(value: Optional[str]) -> str:
    """
    Lowercase, fold curly quotes to straight, collapse whitespace, trim.

    The curly-apostrophe fold is LOAD-BEARING and not cosmetic. site/catalog.csv
    stores "The Frugal Wizard’s Handbook..." with U+2019, and that row is the
    single exclusion proving a series-level mapping cannot work. Miss the fold and
    the one row the whole design rests on silently resolves to The Cosmere.
    """
    if not value:
        return ""
    text = _CURLY_APOSTROPHES.sub("'", str(value))
    text = _CURLY_QUOTES.sub('"', text)
    return _WHITESPACE.sub(" ", text).strip().lower()


def find_platform_dir() -> Tuple[Optional[Path], List[str]]:
    """
    Locate the catalog-platform checkout.

    Returns (dir_or_None, paths_tried). Never raises - the caller decides how
    loud to be, and in this repo the answer is "loud, but keep going".
    """
    tried: List[str] = []

    from_env = os.environ.get(ENV_VAR)
    if from_env:
        candidate = Path(from_env).expanduser().resolve()
        tried.append(f"{ENV_VAR}={candidate}")
        if (candidate / "data" / "universes.json").is_file():
            return candidate, tried
        return None, tried

    for rel in _CANDIDATES:
        candidate = (REPO_ROOT / rel).resolve()
        tried.append(str(candidate))
        if (candidate / "data" / "universes.json").is_file():
            return candidate, tried

    return None, tried


def _empty() -> Dict[str, Any]:
    return {
        "loaded": False,
        "source": None,
        "universes": [],
        "series": {},
        "overrides": {},
        "exclusions": {},
        "canonical": {},
    }


def _index(doc: Dict[str, Any], source: Path) -> Dict[str, Any]:
    series: Dict[str, str] = {}
    overrides: Dict[str, str] = {}
    exclusions: Dict[str, str] = {}

    for u in doc.get("universes") or []:
        name = u.get("name")
        if not name:
            continue
        for s in u.get("series") or []:
            series[_norm(s)] = name
        for b in u.get("bookOverrides") or []:
            overrides[_norm(b.get("title"))] = name
        for b in u.get("bookExclusions") or []:
            exclusions[_norm(b.get("title"))] = name

    canonical = {k: v for k, v in (doc.get("canonicalNames") or {}).items() if not k.startswith("_")}

    return {
        "loaded": True,
        "source": source,
        "universes": [u.get("name") for u in (doc.get("universes") or []) if u.get("name")],
        "series": series,
        "overrides": overrides,
        "exclusions": exclusions,
        "canonical": canonical,
        "schemaVersion": doc.get("schemaVersion"),
        "document": doc,
    }


def _warn(message: str) -> None:
    print(f"[WARN] universes: {message}", file=sys.stderr)


def _load(path: Optional[Path] = None) -> Dict[str, Any]:
    """
    Read and index the shared list. Warns loudly and returns an empty index on
    every failure - a missing checkout, unreadable file, or malformed JSON.
    """
    if path is None:
        platform_dir, tried = find_platform_dir()
        if platform_dir is None:
            _warn(
                "cannot find the catalog-platform checkout, so NO universes are loaded. "
                "The catalog will still build and every book will report no universe.\n"
                "         It owns data/universes.json, the shared list this pipeline reads. "
                "There is no copy in this repo on purpose, because two copies drift.\n"
                "         Tried: " + "; ".join(tried) + "\n"
                f"         Fix: clone catalog-platform beside this repo, or set {ENV_VAR} to its root."
            )
            return _empty()
        path = platform_dir / "data" / "universes.json"

    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        _warn(f"{path} does not exist; no universes loaded.")
        return _empty()
    except json.JSONDecodeError as exc:
        _warn(f"{path} is not valid JSON ({exc}); no universes loaded. Fix it in catalog-platform.")
        return _empty()
    except OSError as exc:
        _warn(f"cannot read {path} ({exc}); no universes loaded.")
        return _empty()

    if doc.get("schemaVersion") != SCHEMA_VERSION:
        # Not fatal, on purpose. A shape change should be visible, not silent,
        # but it must not take a 3x-daily unattended build down.
        _warn(
            f"{path} is schemaVersion {doc.get('schemaVersion')!r} and this pipeline was written "
            f"against {SCHEMA_VERSION}. Reading it anyway; check app/core/universes.py."
        )

    return _index(doc, Path(path))


_DATA = _load()


def reload_universes(path: Optional[Path] = None) -> None:
    """Re-read the list. Used by tests and ad-hoc tooling."""
    global _DATA
    _DATA = _load(path)


def is_loaded() -> bool:
    return bool(_DATA["loaded"])


def source_path() -> Optional[Path]:
    return _DATA["source"]


def universe_names() -> List[str]:
    return list(_DATA["universes"])


def universe_for(title: Optional[str] = None, series: Optional[str] = None) -> Optional[str]:
    """
    Resolve one catalog row to a universe name, or None.

    The order is fixed by _lookup.order in the data file and pinned by the shared
    fixtures:

      1. an exclusion title match  -> None, stop
      2. an override title match   -> that universe
      3. a series match            -> that universe
      4. otherwise                 -> None

    EXCLUSIONS FIRST, so the answer never depends on which rule fires. The Frugal
    Wizard's Handbook and Lux - A Texas Reckoners Novel both sit beside titles
    that would otherwise sweep them in.

    Titles match EXACTLY after normalising - never prefix, never substring, which
    would make "Elantris" match "The Hope of Elantris".

    None is the ordinary answer, not an error. Most books are in no universe, and
    a guess is the one outcome this list exists to prevent.
    """
    key_title = _norm(title)
    if key_title and key_title in _DATA["exclusions"]:
        return None
    if key_title and key_title in _DATA["overrides"]:
        return _DATA["overrides"][key_title]
    key_series = _norm(series)
    if key_series and key_series in _DATA["series"]:
        return _DATA["series"][key_series]
    return None


def canonical_universe_name(name: Optional[str]) -> Optional[str]:
    """Fold a spelling onto the owner's. Unknown names return None - never a guess."""
    return _DATA["canonical"].get(_norm(name))


def report_coverage(rows: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """
    Print a one-line-per-universe summary of what the shared list matched.

    Deliberately reports and writes NOTHING ELSE. Surfacing universes in the
    catalog - a CSV column, anything on the site - is a separate job, and adding
    a column here would change every generated page. This exists so the
    dependency is exercised on every build: a list nobody reads is a list that
    breaks quietly.
    """
    counts: Dict[str, int] = {}
    total = 0
    for row in rows:
        total += 1
        name = universe_for(title=row.get("title"), series=row.get("series"))
        if name:
            counts[name] = counts.get(name, 0) + 1

    if not _DATA["loaded"]:
        print(f"[INFO] Universes: none loaded (see the warning above); {total} rows unclassified.")
        return counts

    matched = sum(counts.values())
    where = _DATA["source"]
    print(f"[INFO] Universes: {len(_DATA['universes'])} loaded from {where}")
    print(f"[INFO] Universes: {matched} of {total} rows in a universe")
    for name in _DATA["universes"]:
        if counts.get(name):
            print(f"[INFO]   {name}: {counts[name]}")
    return counts
