"""F6 (2026-09-07) — the audio library path has ONE derivation, and the state
no test covered is the one the recovery machine is in: `ROOT_DIR` UNSET.

THE DEFECT
----------
`scripts/sync_to_drive.py` read `ROOT_DIR` from the environment itself, with a
default of `C:\\Users\\nbasl\\OpenAudible\\books`; `app/config.py` read the
same variable with a default of `<repo>/library`. The 2026-08-24 sanctity audit
recorded these as *"identical in the default config"*. They were not identical
in the default config — they were identical only because THIS box sets
`ROOT_DIR`. With it unset they pointed at two entirely different folders,
unconditionally, and a fresh clone (the recovery machine, `docs/access/
RECOVERY.md`) has no `.env`.

WHY THESE TESTS ARE SHAPED THIS WAY
-----------------------------------
The obvious test — "assert the two constants are equal" — passes on this box
for the wrong reason, because `ROOT_DIR` is set and any two readings of a set
variable agree. So the tests below do the two things that would actually have
caught it:

* **exercise the UNSET state**, by reloading `app.config` with the variable
  removed and `load_dotenv` neutered (⚠️ without that second half the reload
  reads `.env` straight back and the test silently re-tests the set state);
* **pin the DEDUPLICATION**, not just the current agreement, by asserting no
  second reading of `ROOT_DIR` exists in `sync_to_drive.py`'s source. Two
  constants that agree today are exactly what F6 was, and the audit's own
  wording is the evidence that agreement-today is not a durable property.

⚠️ THE SUITE-HYGIENE HAZARD, stated because it bites silently: reloading
`app.config` rebinds a module every other test imports from. The reload test
restores it in a `finally`, and asserts the restored value matches what it
found — a test that leaves `ROOT_DIR` pointing somewhere else would not fail
here, it would fail somewhere unrelated an hour later.
"""

from __future__ import annotations

import importlib
import os
import re
from pathlib import Path

import app.config as cfg
from scripts import sync_to_drive as sync

REPO = Path(__file__).resolve().parents[1]
OPENAUDIBLE_DEFAULT = Path(r"C:\Users\nbasl\OpenAudible\books")

# ⚠️ SAMPLED AT IMPORT, DELIBERATELY. The reload tests below rebind
# `cfg.ROOT_DIR` to a fresh Path object, while `sync.OPENAUDIBLE_BOOKS_DIR`
# keeps the binding it took when `sync_to_drive` was imported — so an identity
# check evaluated after a reload reports False for a reason that has nothing
# to do with F6. Reading it once, here, makes the assertion independent of the
# order tests happen to run in.
_SAME_OBJECT_AT_IMPORT = sync.OPENAUDIBLE_BOOKS_DIR is cfg.ROOT_DIR


def test_the_sorter_source_and_the_library_root_are_the_same_object():
    """The F6 property itself. `is`, not `==`: equal values can drift apart on
    the next machine, one object cannot drift at all."""
    assert _SAME_OBJECT_AT_IMPORT, (
        "scripts/sync_to_drive.OPENAUDIBLE_BOOKS_DIR must BE app.config.ROOT_DIR"
    )


def test_sync_to_drive_no_longer_reads_ROOT_DIR_itself():
    """⚠️ THE ONE THAT PINS THE FIX RATHER THAN THE SYMPTOM. Re-inlining
    `Path(os.getenv("ROOT_DIR", ...))` here would restore the divergence while
    leaving the equality test above green on this box, because this box sets
    the variable."""
    source = (REPO / "scripts" / "sync_to_drive.py").read_text(encoding="utf-8")
    code = [ln for ln in source.splitlines() if not ln.lstrip().startswith("#")]
    offenders = [ln for ln in code if re.search(r"getenv\(\s*[\"']ROOT_DIR", ln)]
    assert not offenders, (
        "sync_to_drive must import ROOT_DIR from app.config, not re-derive it: "
        f"{offenders}"
    )


def test_with_ROOT_DIR_UNSET_both_land_on_the_openaudible_export():
    """⚠️ THE STATE NO TEST COVERED, AND THE ONE THE RECOVERY MACHINE IS IN.

    Before F6 this asserted two different folders. `<repo>/library` is the
    default that was RETIRED here — it is asserted against by name, because a
    revert to it would otherwise look like a harmless tidy-up.
    """
    import dotenv

    saved = os.environ.pop("ROOT_DIR", None)
    real_load_dotenv = dotenv.load_dotenv
    # ⚠️ Without this the reload re-reads `.env` and puts ROOT_DIR straight
    # back, and the test passes while testing nothing.
    dotenv.load_dotenv = lambda *a, **k: False
    try:
        reloaded = importlib.reload(cfg)
        expected = OPENAUDIBLE_DEFAULT.expanduser().resolve()

        assert reloaded.DEFAULT_LIBRARY_DIR == OPENAUDIBLE_DEFAULT
        assert reloaded.ROOT_DIR == expected
        assert reloaded.ROOT_DIR != (reloaded.PROJECT_ROOT / "library").resolve(), (
            "`<repo>/library` is the pre-F6 default; it diverged from the "
            "sorter's source on every machine with ROOT_DIR unset"
        )
    finally:
        dotenv.load_dotenv = real_load_dotenv
        if saved is not None:
            os.environ["ROOT_DIR"] = saved
        importlib.reload(cfg)

    # Suite hygiene: the module other tests import from is back as it was.
    assert (cfg.ROOT_DIR == Path(saved).expanduser().resolve()) if saved else True


def test_an_empty_ROOT_DIR_is_not_the_current_directory():
    """`ROOT_DIR=""` used to give `sync_to_drive` `Path("")` — the CWD — which
    is the sorter rglobbing whatever directory it was launched from. Both
    readers now treat set-but-empty as unset."""
    import dotenv

    saved = os.environ.get("ROOT_DIR")
    os.environ["ROOT_DIR"] = ""
    real_load_dotenv = dotenv.load_dotenv
    dotenv.load_dotenv = lambda *a, **k: False
    try:
        reloaded = importlib.reload(cfg)
        assert reloaded.ROOT_DIR == OPENAUDIBLE_DEFAULT.expanduser().resolve()
        assert reloaded.ROOT_DIR != Path("").resolve()
    finally:
        dotenv.load_dotenv = real_load_dotenv
        if saved is not None:
            os.environ["ROOT_DIR"] = saved
        else:
            os.environ.pop("ROOT_DIR", None)
        importlib.reload(cfg)


# ---------------------------------------------------------------------------
# the residual F6 did NOT close — measured 2026-09-07, and it is seven, not one
# ---------------------------------------------------------------------------

# ⚠️ THE AUDIT SAID "TWO DERIVATIONS". THERE WERE NINE.
# `app/config.py` plus EIGHT copies of the identical expression
# `Path(os.getenv("ROOT_DIR", r"C:\Users\nbasl\OpenAudible\books"))`. F6 merged
# the one that matters — `sync_to_drive.py`, the pipeline entrypoint, the
# module whose divergence F5 was named after — and left these seven, which are
# hand-run tools rather than scheduled pipeline steps.
#
# 🔴 They are LISTED, not tolerated silently, and the list is asserted EXACT so
# a tenth copy turns this red instead of joining a crowd. Choosing the
# OpenAudible path as `app/config.py`'s default is what makes them all agree in
# the meantime: under the other option (`<repo>/library`) this merge would have
# left ONE reader pointing somewhere else and seven pointing here, i.e. more
# divergence than it removed.
#
# ⚠️ `transcribe_audiobook.py` is on the LIVE ingestion path and a nightly run
# was mid-cycle while F6 landed, which is the other reason these were left.
KNOWN_UNMERGED_ROOT_DIR_READERS = {
    "scripts/audit_drive_vs_local.py",
    "scripts/generate_test_book.py",
    "scripts/health_check.py",
    "scripts/migrate_folder_names.py",
    "scripts/reclaim_drive_files.py",
    "scripts/reclaim_others.py",
    "scripts/transcribe_audiobook.py",
}


def test_no_new_place_starts_reading_ROOT_DIR_from_the_environment():
    found = set()
    for path in sorted((REPO / "scripts").glob("*.py")) + sorted(
        (REPO / "app").rglob("*.py")
    ):
        rel = path.relative_to(REPO).as_posix()
        if rel == "app/config.py":
            continue  # the ONE canonical reader
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lstrip().startswith("#"):
                continue
            if re.search(r"getenv\(\s*[\"']ROOT_DIR", line):
                found.add(rel)
                break

    new = found - KNOWN_UNMERGED_ROOT_DIR_READERS
    assert not new, (
        "a NEW second derivation of the library path appeared - import "
        f"`app.config.ROOT_DIR` instead: {sorted(new)}"
    )
    gone = KNOWN_UNMERGED_ROOT_DIR_READERS - found
    assert not gone, (
        "these were merged - delete them from KNOWN_UNMERGED_ROOT_DIR_READERS "
        f"so the list keeps meaning something: {sorted(gone)}"
    )
