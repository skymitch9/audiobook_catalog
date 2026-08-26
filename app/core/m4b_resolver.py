"""Catalog TITLE -> the audio file on disk, through the pipeline's OWN index first.

⚠️ WHY THIS MODULE EXISTS. Measured 2026-08-25: 12 books on the GABI Knowledge
page read `transcription failed`, and every one of them was really this, from
`output_files/ingest_nightly.log`::

    FileNotFoundError: no .m4b under C:\\Users\\nbasl\\OpenAudible\\books
                       matches 'Space Knight Book 9'

The files were all present. The ingester was guessing a filename from a title,
and OpenAudible's filenames diverge from Audible's titles in at least five
measured shapes (docs/TODO.md, that day's table):

| catalog title | file on disk |
|---|---|
| `Space Knight Book 9` | `Michael-Scott Earle - [Space Knight - 9] - Space Knight Book 9 (narrators).m4b` |
| `Demonic Devourer: Book 2: Demonic Devourer Series` | `Demonic Devourer- Book 2.m4b` |
| `Phoebe Berman's Gonna Lose It: A Novel` | `Phoebe Berman's Gonna Lose It.m4b` |
| `Everything` | `Everything - Full Murderhobo, Book 3.m4b` |
| `City of Light: Wings of Justice` | `Wings of Justice - City of Light, Book 1.m4b` |

No filename heuristic can be trusted across that spread. But the pipeline
already KNOWS every book's path: the catalog build writes each row's
``cover_href`` as ``covers/<path relative to ROOT_DIR>/<file stem>.<ext>``
(``app/metadata.py:_save_cover_for_file``), which is an exact address for the
file. That index is tier 0 here, and it answers all six shapes above.

## The tiers, in order — and each one is narrower than the last

0. **INDEX** — the catalog row for this title, then
   ``book_lookup.locate_file`` (the estate's ONE row -> path function). Only
   its cover-addressed answer is accepted; see ``_index_path``.
1. **RAW normalised equality** — ``review_join.normalise_title`` on both sides.
   The colon-vs-dash case: Windows forbids ``:`` in a filename, so
   ``The Primal Hunter 9: A LitRPG Adventure`` is on disk as ``...9- A LitRPG
   Adventure.m4b``.
2. **TAIL STRIP** — one `` - `` segment at a time from the right, unique match
   only. OpenAudible names some files title-only while the queue title keeps a
   `` - Series, Book N`` tail (ACOTAR Part 1, the first real nightly book).
3. **FOLD equality** — ``clean_audiobook_title`` (the ONE title fold this repo
   has) then ``normalise_title``, applied to BOTH sides, plus numbers agree.
4. **FOLD containment** — the same fold, whole-word contiguous containment
   either way, plus numbers agree, plus the bare-title guard.

⚠️ **NEVER A SECOND NORMALISER.** The fold is
``normalise_title(clean_audiobook_title(x))`` — both functions already exist and
are ports of ``library_catalog``'s ``titles.ts``. The estate forbids a second
copy of a decision-making function; a resolver that invented its own stripping
rules would drift from the catalog it is trying to agree with.

⚠️ **A containment match may differ in words, never in NUMBERS.** Lifted from
``library_catalog/packages/core/src/matching.ts::numbersAgree``, whose header
carries the two production false positives that earned the rule. Here it is
what stops ``Space Knight Book 5`` resolving to ``Space Knight.m4b`` or to
``Space Knight, Book 2.m4b`` — a bare volume tail is identity, not boilerplate,
and transcribing the wrong book reports itself as SUCCESS.

⚠️ **A bare short title matches by the FOLD, never by substring alone.**
``Everything`` finds ``Everything - Full Murderhobo, Book 3.m4b`` at tier 3
because the fold strips the series tail off the FILE and the two folds are then
equal. It must never reach tier 4 and pick ``Everything Is Fine.m4b``, which
contains it word-for-word and is a different book. Hence
``_MIN_CONTAINMENT_WORDS``.

⚠️ **Several candidates left standing = REFUSE, with their names.** Never
"pick the longest" or "pick the first". Same stance as the tail-strip pass has
taken since 2026-08-18.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from app.config import EXTS, ROOT_DIR
from app.core.book_lookup import locate_file
from app.core.review_join import normalise_title
from app.library_link import clean_audiobook_title

__all__ = [
    "AmbiguousBookFile",
    "BookFileNotFound",
    "fold_title",
    "numbers_agree",
    "numbers_in",
    "resolve_book_file",
]

# The extensions the FILENAME tiers scan. Deliberately `.m4b` only, which is
# exactly what the nightly transcriber has always globbed — widening it here
# would change which files can become AMBIGUOUS, and that is not this change's
# job. Tier 0 goes through `locate_file`, which already honours the full
# `config.EXTS`, so an `.m4a` book still resolves through the index.
SCAN_EXTS: tuple = (".m4b",)

# Below this many words, a title does not take part in tier 4 containment: it
# must be matched by the fold (tier 3) or not at all. `Everything` is one word;
# every book whose title merely BEGINS with it would otherwise be a candidate.
# A title carrying a digit is exempt — a number is identity, and `numbers_agree`
# is then doing the discriminating work.
_MIN_CONTAINMENT_WORDS = 3

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


class BookFileNotFound(FileNotFoundError):
    """No file on disk answers this title through any tier."""


class AmbiguousBookFile(FileNotFoundError):
    """Several files answer this title and none of them is more right.

    ⚠️ A subclass of ``FileNotFoundError`` on purpose: every existing caller
    already treats "could not resolve" as that type, and an ambiguous title IS
    an unresolved one. The distinct class exists so a caller that wants to say
    *which* of the two happened can, without a string match on the message.
    """


# --------------------------------------------------------------------------
# the fold — one fold, both sides, no new normaliser
# --------------------------------------------------------------------------

def fold_title(raw: Optional[str]) -> str:
    """The repo's single title fold: strip Audible packaging, then normalise.

    ``clean_audiobook_title`` (app/library_link.py, port of ``titles.ts``)
    removes ``- Series, Book N``, ``(Part 1 of 2)``, ``Dramatized Adaptation``,
    ``: A Novel`` and friends; ``normalise_title`` (app/core/review_join.py,
    port of the same file) folds diacritics, case and punctuation and drops a
    leading article. Both already exist; nothing new is invented here.
    """
    return normalise_title(clean_audiobook_title(raw))


def numbers_in(folded: str) -> set:
    """Every number in a folded title, as strings. Port of matching.ts."""
    return set(_NUMBER_RE.findall(folded or ""))


def numbers_agree(a: str, b: str) -> bool:
    """True when two folded titles carry exactly the same numbers.

    ⚠️ THE RULE: a containment match may differ in words, never in numbers.
    ``matching.ts::numbersAgree``'s header has the measured false positives
    (`Tamer: King of Dinosaurs Book 11` matching the series-level row; `The
    Primal Hunter` matching `The Primal Hunter 10`). The audio version of the
    same bug is worse: it transcribes twenty GPU-minutes of the wrong book and
    files the transcript under the right book's name.
    """
    return numbers_in(a) == numbers_in(b)


def _words(folded: str) -> List[str]:
    return [w for w in (folded or "").split(" ") if w]


def _contains_words(hay: Sequence[str], needle: Sequence[str]) -> bool:
    """Whole-word CONTIGUOUS containment. `space knight` is in `space knight
    book 9`; `knight book` is too; `space book` is not.

    Contiguous, not set-membership: a bag-of-words test makes `City of Light`
    a match for any title sharing three common words, which is most of them.
    """
    n = len(needle)
    if n == 0 or n > len(hay):
        return False
    return any(list(hay[i:i + n]) == list(needle) for i in range(len(hay) - n + 1))


# --------------------------------------------------------------------------
# tier 0 — the index the catalog build itself produced
# --------------------------------------------------------------------------

def _index_path(row: Dict[str, str], root: Path) -> Optional[Path]:
    """The row's file, but ONLY when the answer came from ``cover_href``.

    ⚠️ ``locate_file`` is reused rather than reimplemented (one canonical
    row -> path function), but its *fallback* limb is a title guess of exactly
    the kind this module exists to replace: it accepts any stem in the author's
    folder that ``startswith(title[:40])``, which is how `Everything` would
    reach `Everything Is Fine`. So its answer is accepted only when it is the
    cover-ADDRESSED one — a post-condition, not a second copy of the decision.
    Anything else falls through to the fold tiers below, which have the number
    gate the fallback limb does not.
    """
    href = (row.get("cover_href") or "").strip()
    if not href:
        return None
    path = locate_file(row, root)
    if path is None:
        return None
    rel = Path(href)
    if rel.parts and rel.parts[0] == "covers":
        rel = Path(*rel.parts[1:])
    # ⚠️ Compare parent + STEM, never `with_suffix("")` — see locate_file's
    # docstring for the `J.L.Mullins…` case where pathlib reads an author's
    # initials as an extension.
    return path if (path.parent == root / rel.parent and path.stem == rel.stem) else None


def _rows_for_title(title: str, rows: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    """Catalog rows for this title, by the narrowest tier that matches.

    Exact string, then raw normalised, then the fold (with numbers agreeing).
    Stops at the first tier that produces anything, so a fold match can never
    outvote an exact one.
    """
    rows = list(rows or [])
    want_raw = normalise_title(title)
    want_fold = fold_title(title)

    exact = [r for r in rows if (r.get("title") or "") == title]
    if exact:
        return exact
    raw = [r for r in rows if normalise_title(r.get("title") or "") == want_raw]
    if raw:
        return raw
    if not want_fold:
        return []
    return [
        r for r in rows
        if fold_title(r.get("title") or "") == want_fold
        and numbers_agree(normalise_title(r.get("title") or ""), want_raw)
    ]


# --------------------------------------------------------------------------
# the resolver
# --------------------------------------------------------------------------

def _unique_or_refuse(title: str, hits: List[Path], tier: str) -> Optional[Path]:
    """One path -> it. Several -> refuse by name. None -> keep looking."""
    seen: List[Path] = []
    for p in hits:
        if p not in seen:
            seen.append(p)
    if len(seen) == 1:
        return seen[0]
    if len(seen) > 1:
        raise AmbiguousBookFile(
            f"title {title!r} matches {len(seen)} files by {tier} - refusing to guess: "
            + ", ".join(sorted(p.name for p in seen))
        )
    return None


def resolve_book_file(
    title: str,
    *,
    rows: Optional[Iterable[Dict[str, str]]] = None,
    root: Path = ROOT_DIR,
    files: Optional[Iterable[Path]] = None,
) -> Path:
    """Resolve a catalog title to its audio file. Raises, never returns None.

    ``rows`` is the catalog (``site/catalog.csv`` as dicts) — pass ``[]`` to
    skip tier 0 entirely. ``files`` overrides the on-disk scan (tests).

    Raises :class:`AmbiguousBookFile` when several candidates survive a tier and
    :class:`BookFileNotFound` when none does. Both are ``FileNotFoundError``.
    """
    root = Path(root)
    all_files = list(files) if files is not None else [
        p for p in root.rglob("*") if p.suffix.lower() in SCAN_EXTS
    ]

    # ---- tier 0: the index ------------------------------------------------
    if rows is None:
        rows = _load_rows()
    matched_rows = _rows_for_title(title, rows)
    if matched_rows:
        hit = _unique_or_refuse(
            title,
            [p for p in (_index_path(r, root) for r in matched_rows) if p is not None],
            "the catalog index (cover_href)",
        )
        if hit is not None:
            return hit

    # ---- tier 1: raw normalised equality ----------------------------------
    want_raw = normalise_title(title)
    for path in all_files:
        if normalise_title(path.stem) == want_raw:
            return path

    # ---- tier 2: strip ` - ` tail segments, rightmost first ---------------
    stripped = title
    while " - " in stripped:
        stripped = stripped.rsplit(" - ", 1)[0]
        key = normalise_title(stripped)
        if not key:
            break
        hits = [p for p in all_files if normalise_title(p.stem) == key]
        hit = _unique_or_refuse(title, hits, f"the stripped title {stripped!r}")
        if hit is not None:
            return hit

    # ---- tier 3: fold equality -------------------------------------------
    want_fold = fold_title(title)
    if want_fold:
        hits = [
            p for p in all_files
            if fold_title(p.stem) == want_fold and numbers_agree(fold_title(p.stem), want_fold)
        ]
        hit = _unique_or_refuse(title, hits, "the cleaned title fold")
        if hit is not None:
            return hit

    # ---- tier 4: fold containment, numbers agreeing -----------------------
    want_words = _words(want_fold)
    eligible = len(want_words) >= _MIN_CONTAINMENT_WORDS or bool(numbers_in(want_fold))
    if want_fold and eligible:
        hits = []
        for p in all_files:
            cand = fold_title(p.stem)
            if not cand or not numbers_agree(cand, want_fold):
                continue
            cand_words = _words(cand)
            if _contains_words(cand_words, want_words) or _contains_words(want_words, cand_words):
                hits.append(p)
        hit = _unique_or_refuse(title, hits, "the cleaned title fold + containment")
        if hit is not None:
            return hit

    raise BookFileNotFound(f"no .m4b under {root} matches {title!r}")


def _load_rows() -> List[Dict[str, str]]:
    """The catalog, loaded lazily so importing this module reads no files."""
    from app.core.ingest_queue import load_catalog
    return load_catalog()
