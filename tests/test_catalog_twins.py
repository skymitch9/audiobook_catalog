"""Guards for the curated twin table — `app/core/catalog_twins.py`.

WHAT IS BEING GUARDED, AND WHY IT NEEDS GUARDING
------------------------------------------------
This is the only layer in the repo that makes a book DISAPPEAR from the
catalogue. Every other correction changes what a row says; this one decides a
row should not exist. That asymmetry is the whole reason for these tests:

* a wrong REFUSAL shows a book twice — visible, and the status quo;
* a wrong DROP makes a book the household owns vanish from its own catalogue —
  invisible, because nobody misses a card they never saw.

So most of what follows asserts a refusal, not a success. Each one is a
different way the table can be wrong or the library can drift underneath it,
and every single one must end with BOTH editions still catalogued.

🔴 AND THE CONSTRAINT THAT OUTRANKS ALL OF THEM: no file is ever deleted,
moved, renamed or opened for writing. Owner, 2026-09-02: *"Keep the audible one
but make sure all source files stay."* `test_no_file_is_ever_touched` pins it
directly, against real files on disk in tmp_path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.catalog_twins import (
    ASSERTED_FIELDS,
    DEFAULT_TABLE_PATH,
    TwinProbe,
    apply_catalog_twins,
    load_table,
)

REPO = Path(__file__).resolve().parents[1]

SURVIVOR_REL = "Brandon Sanderson/Isles of the Emberdark - A Cosmere Novel Secret Projects, Book 5.m4b"
RETIRE_REL = "Brandon Sanderson/Isles_of_the_Emberdark_by_Brandon_Sanderson.mp4"

SURVIVOR_FIELDS = {
    "title": "Isles of the Emberdark",
    "author": "Brandon Sanderson",
    "narrator": "Kaleo Griffith, Jennifer Jill Araya",
    "duration_hhmm": "16:53",
}
RETIRE_FIELDS = {
    "title": "Isles of the Emberdark",
    "author": "Brandon Sanderson",
    "narrator": "Brandon Sanderson",
    "duration_hhmm": "16:53",
}


def _entry(survivor=None, retire=None, **over) -> dict:
    e = {
        "book": "Isles of the Emberdark - Brandon Sanderson",
        "survivor": {"file": SURVIVOR_REL, **SURVIVOR_FIELDS, **(survivor or {})},
        "retire": {"file": RETIRE_REL, **RETIRE_FIELDS, **(retire or {})},
    }
    e.update(over)
    return e


def _table(tmp_path: Path, *entries: dict) -> Path:
    p = tmp_path / "catalog_twins.json"
    p.write_text(json.dumps({"twins": list(entries)}), encoding="utf-8")
    return p


def _library(tmp_path: Path, *rels: str) -> tuple:
    """Real (empty) files on disk, so a test can prove they survive."""
    root = tmp_path / "books"
    made = []
    for rel in rels:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"not really audio")
        made.append(p)
    return root, made


def _probe(mapping: dict):
    """A probe that answers from a table instead of reading tags.

    ⚠️ Injected for the same reason `build_queue` takes a `pdf_classifier`:
    these tests must not need mutagen, must not read the real library, and must
    be able to simulate drift that would otherwise require re-tagging a 600 MB
    file.
    """
    def probe(path: Path) -> TwinProbe:
        try:
            return TwinProbe(mapping[path.name])
        except KeyError:  # pragma: no cover - a test wiring mistake
            raise AssertionError(f"probe asked about an unexpected file: {path.name}")
    return probe


def _both(over_survivor=None, over_retire=None):
    s = dict(SURVIVOR_FIELDS, **(over_survivor or {}))
    r = dict(RETIRE_FIELDS, **(over_retire or {}))
    return _probe({Path(SURVIVOR_REL).name: s, Path(RETIRE_REL).name: r})


# --------------------------------------------------------------------------- #
# The happy path — one row retires, and nothing else changes
# --------------------------------------------------------------------------- #
def test_the_retired_edition_leaves_the_catalogue_and_the_survivor_stays(tmp_path):
    root, files = _library(tmp_path, SURVIVOR_REL, RETIRE_REL, "Other/A Different Book.m4b")
    kept, report = apply_catalog_twins(
        files, root, table_path=_table(tmp_path, _entry()), probe=_both()
    )

    assert [p.name for p in kept] == [
        Path(SURVIVOR_REL).name,
        "A Different Book.m4b",
    ]
    assert report.applied == 1
    assert report.dropped == [RETIRE_REL]
    assert report.refused == []


def test_no_file_is_ever_touched(tmp_path):
    """🔴 THE ABSOLUTE CONSTRAINT, pinned against real bytes on disk.

    Owner, 2026-09-02: *"Keep the audible one but make sure all source files
    stay."* The retired edition keeps its file here, and by the same code path
    keeps its Drive copy, its R2 `estate-audio` archive key (which is THE
    BACKUP, not a cache) and its `upload_manifest.json` entry — none of which
    this module can reach, because it only ever returns a shorter list.
    """
    root, files = _library(tmp_path, SURVIVOR_REL, RETIRE_REL)
    before = {p: (p.exists(), p.read_bytes(), p.stat().st_mtime_ns) for p in files}

    apply_catalog_twins(files, root, table_path=_table(tmp_path, _entry()), probe=_both())

    for p, (existed, data, mtime) in before.items():
        assert p.exists() is existed, f"{p.name} was deleted or moved"
        assert p.read_bytes() == data, f"{p.name} was rewritten"
        assert p.stat().st_mtime_ns == mtime, f"{p.name} was opened for writing"


# --------------------------------------------------------------------------- #
# ⚠️ Every refusal below must leave BOTH editions catalogued
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("field", ASSERTED_FIELDS)
def test_drift_on_any_asserted_field_refuses_rather_than_adapting(tmp_path, field):
    """A row whose live state has drifted is refused BY NAME, never repaired.

    ⚠️ `duration_hhmm` is in this list for a reason the other three do not
    cover: title/author/narrator catch a RETAG, and duration catches a
    SUBSTITUTION — a re-download, a different cut, a path reused for another
    book. A check that only looked at names would sail straight past the one
    kind of drift that changes which recording is behind the path.
    """
    root, files = _library(tmp_path, SURVIVOR_REL, RETIRE_REL)
    kept, report = apply_catalog_twins(
        files, root,
        table_path=_table(tmp_path, _entry()),
        probe=_both(over_retire={field: "something else entirely"}),
    )

    assert len(kept) == 2, "a drifted entry must leave both editions in the catalogue"
    assert report.applied == 0
    assert len(report.refused) == 1
    label, why = report.refused[0]
    assert "Isles of the Emberdark" in label
    assert field in why and "drifted" in why


def test_drift_on_the_SURVIVOR_refuses_too(tmp_path):
    """⚠️ Both sides are asserted, not just the one being dropped. If the
    survivor is not the file the table describes, dropping the other one leaves
    the catalogue holding a row nobody vetted."""
    root, files = _library(tmp_path, SURVIVOR_REL, RETIRE_REL)
    kept, report = apply_catalog_twins(
        files, root,
        table_path=_table(tmp_path, _entry()),
        probe=_both(over_survivor={"narrator": "Somebody Else"}),
    )
    assert len(kept) == 2
    assert "survivor" in report.refused[0][1]


def test_a_missing_SURVIVOR_refuses(tmp_path):
    """🔴 The dangerous shape: if the survivor is not in the walk and the drop
    went ahead anyway, the book would leave the catalogue entirely."""
    root, files = _library(tmp_path, RETIRE_REL)
    kept, report = apply_catalog_twins(
        files, root, table_path=_table(tmp_path, _entry()), probe=_both()
    )
    assert [p.name for p in kept] == [Path(RETIRE_REL).name]
    assert "survivor" in report.refused[0][1]


def test_a_missing_RETIRED_edition_is_reported_and_changes_nothing(tmp_path):
    """Not an error worth stopping for — the duplicate is already absent from
    this walk (it may simply not be on this machine) — but it is SAID, because
    a table row that matches nothing is a row somebody should look at."""
    root, files = _library(tmp_path, SURVIVOR_REL)
    kept, report = apply_catalog_twins(
        files, root, table_path=_table(tmp_path, _entry()), probe=_both()
    )
    assert len(kept) == 1
    assert report.applied == 0
    assert "nothing to drop" in report.refused[0][1]


def test_the_two_sides_must_still_claim_the_same_title(tmp_path):
    """⚠️ That claim IS the duplication. Two files that no longer agree on a
    title are two books, and folding them would be the Space Knight failure
    (`docs/info/book-ingestion.md` §4) in a new place."""
    root, files = _library(tmp_path, SURVIVOR_REL, RETIRE_REL)
    entry = _entry(retire={"title": "A Completely Different Book"})
    kept, report = apply_catalog_twins(
        files, root,
        table_path=_table(tmp_path, entry),
        probe=_both(over_retire={"title": "A Completely Different Book"}),
    )
    assert len(kept) == 2
    assert "same title" in report.refused[0][1]


def test_an_entry_that_retires_its_own_survivor_is_refused(tmp_path):
    """A copy-paste typo that would delete the book from the catalogue."""
    root, files = _library(tmp_path, SURVIVOR_REL)
    entry = _entry(retire={"file": SURVIVOR_REL})
    kept, report = apply_catalog_twins(
        files, root, table_path=_table(tmp_path, entry), probe=_both()
    )
    assert len(kept) == 1
    assert "same path" in report.refused[0][1]


def test_an_unreadable_file_is_a_refusal_not_a_crash(tmp_path):
    root, files = _library(tmp_path, SURVIVOR_REL, RETIRE_REL)

    def boom(path: Path):
        raise OSError("the file is locked by another process")

    kept, report = apply_catalog_twins(
        files, root, table_path=_table(tmp_path, _entry()), probe=boom
    )
    assert len(kept) == 2
    assert "could not be read" in report.refused[0][1]


def test_an_entry_missing_a_required_assertion_is_refused(tmp_path):
    """⚠️ A half-filled entry must not be read generously. An absent `narrator`
    is not "narrator does not matter" — it is a row nobody finished."""
    root, files = _library(tmp_path, SURVIVOR_REL, RETIRE_REL)
    entry = _entry()
    del entry["retire"]["narrator"]
    kept, report = apply_catalog_twins(
        files, root, table_path=_table(tmp_path, entry), probe=_both()
    )
    assert len(kept) == 2
    assert "'narrator'" in report.refused[0][1]


def test_an_entry_missing_a_whole_side_is_refused(tmp_path):
    root, files = _library(tmp_path, SURVIVOR_REL, RETIRE_REL)
    entry = _entry()
    del entry["retire"]
    kept, report = apply_catalog_twins(
        files, root, table_path=_table(tmp_path, entry), probe=_both()
    )
    assert len(kept) == 2
    assert "'survivor' and a 'retire'" in report.refused[0][1]


# --------------------------------------------------------------------------- #
# The table itself
# --------------------------------------------------------------------------- #
def test_a_malformed_table_is_a_LOUD_no_op(tmp_path):
    """⚠️ Malformed means "catalogue everything", which is what the site did
    before this layer existed — the fail-safe direction. But it is REPORTED:
    a correction layer that silently stops correcting is how nobody notices for
    a month. Compare `overrides_store.load()`, which refuses malformed JSON for
    the mirror-image reason (there, the fail-safe read would overwrite every
    correction with nothing)."""
    bad = tmp_path / "catalog_twins.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    root, files = _library(tmp_path, SURVIVOR_REL, RETIRE_REL)

    kept, report = apply_catalog_twins(files, root, table_path=bad, probe=_both())
    assert len(kept) == 2
    assert report.refused and report.refused[0][0] == "<table>"

    entries, problem = load_table(bad)
    assert entries == [] and "not readable JSON" in problem


def test_an_absent_table_is_a_silent_no_op(tmp_path):
    """No file means no twins recorded — that is a normal state, not a fault,
    and it must not print a warning on every build of a repo that has none."""
    root, files = _library(tmp_path, SURVIVOR_REL)
    kept, report = apply_catalog_twins(
        files, root, table_path=tmp_path / "nope.json", probe=_both()
    )
    assert len(kept) == 1
    assert report.refused == []
    assert load_table(tmp_path / "nope.json") == ([], None)


def test_the_shipped_table_is_complete_and_evidenced():
    """Every entry a person can add must carry the same shape the Emberdark one
    does. `evidence` is mandatory in the corrections layer for exactly this
    reason (`docs/info/catalog-corrections.md` §5): a future reader must be able
    to tell a researched decision from a typo — and here the decision removes a
    book from the catalogue."""
    entries, problem = load_table(DEFAULT_TABLE_PATH)
    assert problem is None
    assert entries, "scripts/catalog_twins.json has no entries"
    for entry in entries:
        label = entry.get("book")
        assert label, "every entry needs a human-readable 'book' label"
        for key in ("added", "owner", "why"):
            assert entry.get(key), f"{label}: missing {key!r} — this is a decision, not a derivation"
        for side in ("survivor", "retire"):
            spec = entry.get(side)
            assert isinstance(spec, dict), f"{label}: {side} must be an object"
            assert spec.get("file"), f"{label}: {side} must name a file"
            for field in ASSERTED_FIELDS:
                assert spec.get(field), f"{label}: {side} must assert {field!r}"
        assert entry["survivor"]["file"] != entry["retire"]["file"]
        assert entry["survivor"]["title"] == entry["retire"]["title"], (
            f"{label}: the two sides must claim the same title — that IS the duplication"
        )


def test_the_build_applies_the_table():
    """🔴 THE WIRING GUARD, same shape as `test_the_REBUILD_actually_happened`.

    A perfect table nothing calls is a file, not a mechanism. `app/main.py` is
    the ONE consumer, and it must run the pass AFTER the two filename dedupe
    passes (those fold files that share a name; this folds files that share a
    book) and BEFORE `extract_metadata`, so a retired edition never even has
    its cover written out."""
    src = (REPO / "app" / "main.py").read_text(encoding="utf-8")
    assert "apply_catalog_twins" in src, "app/main.py no longer applies the twin table"
    assert src.index("dedupe_library(files)") < src.index("apply_catalog_twins(deduped_files")
    assert src.index("apply_catalog_twins(deduped_files") < src.index("extract_metadata(p)")
    # ⚠️ And every refusal reaches a human: a refused entry is a duplicate
    # still on the site.
    assert "catalog twin refused" in src


# --------------------------------------------------------------------------- #
# The live check — skipped anywhere the real library is not mounted
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    not Path(__import__("app.config", fromlist=["ROOT_DIR"]).ROOT_DIR).exists(),
    reason="the real audiobook library is not on this machine (CI)",
)
def test_the_shipped_table_still_asserts_against_the_live_library():
    """⚠️ The one test that can catch the table going stale on the machine that
    matters. A retag, a re-download or a rename under `ROOT_DIR` makes an entry
    refuse — safely, but silently, unless something looks. This looks.

    It reads real files and writes nothing; `probe_file` is deliberately
    side-effect-free for this reason (`extract_metadata` would write covers)."""
    from app.config import EXTS, ROOT_DIR
    from app.core.file_dedupe import dedupe_library
    from app.metadata import walk_library

    root = Path(ROOT_DIR)
    exts = set(EXTS) if isinstance(EXTS, (set, list, tuple)) else {".m4b", ".m4a", ".mp4"}
    files = [f for f in walk_library(root, exts) if not f.name.startswith("Copy of ")]
    files, _ = dedupe_library(files)

    kept, report = apply_catalog_twins(files, root)
    assert report.refused == [], f"the twin table has drifted: {report.refused}"
    assert report.applied == len(load_table(DEFAULT_TABLE_PATH)[0])
    assert len(kept) == len(files) - report.applied
