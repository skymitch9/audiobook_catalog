"""
`site/cross-catalog-overrides.json` — the hand-reviewed cross-catalog joins.

⚠️ WHY THIS FILE EXISTS. The overrides file is the ONE place a join that both
automatic matchers refuse may still be asserted, which makes it the one place a
wrong join cannot be caught by anything else. `app/library_link.py` tombstones
an ambiguous title rather than guess; `library_catalog`'s mapping route
withholds a fold for the same reason. A curated row overrules both of those
refusals BY DESIGN, so the only protection left is that somebody looked — and
these tests are what stop the row rotting quietly after they did.

⚠️ THE OTHER HALF OF THE CHECK IS IN THE SIBLING REPO, and it has to be.
This repo cannot ask whether library work 229 exists; only `library_catalog`
can reach that database. So the check is split, deliberately:

    here                              `npm run check:cross-links` (library_catalog)
    ────                              ──────────────────────────
    every audiobookTitle exists       every libraryWorkId exists in `work`
    VERBATIM in site/catalog.csv      (the check that stops a dead /work/<id>
                                      link shipping)

Neither half is sufficient. Say so when reporting either one.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
OVERRIDES_PATH = REPO_ROOT / "site" / "cross-catalog-overrides.json"
CATALOG_CSV = REPO_ROOT / "site" / "catalog.csv"


@pytest.fixture(scope="module")
def overrides() -> list[dict]:
    data = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    assert isinstance(data.get("overrides"), list), "overrides must be a list"
    return data["overrides"]


@pytest.fixture(scope="module")
def catalog_titles() -> set[str]:
    if not CATALOG_CSV.exists():  # pragma: no cover - a checkout without a built site
        pytest.skip(f"{CATALOG_CSV} not present")
    with open(CATALOG_CSV, "r", encoding="utf-8", newline="") as f:
        return {(r.get("title") or "").strip() for r in csv.DictReader(f)}


def test_file_is_present_and_tracked():
    """
    ⚠️ `.gitignore` ignores `*.json` wholesale, so a new one under `site/` is
    invisible until a negation is added. An untracked overrides file is two
    test suites that pass on this machine and fail everywhere else — the exact
    trap `tests/fixtures/slug_cases.json`'s own negation comment records.
    """
    assert OVERRIDES_PATH.exists()
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "!site/cross-catalog-overrides.json" in gitignore


def test_every_row_has_the_required_fields(overrides):
    for row in overrides:
        for field in ("audiobookTitle", "libraryWorkId", "libraryTitle", "format", "why", "reviewedOn"):
            assert row.get(field) not in (None, ""), f"{field} missing from {row}"


def test_work_ids_are_positive_integers(overrides):
    """A float, a zero or a string would render `/work/NaN` — a dead link with
    a plausible shape, which is worse than the absent one the refusal gives."""
    for row in overrides:
        wid = row["libraryWorkId"]
        assert isinstance(wid, int) and not isinstance(wid, bool), f"{wid!r} is not an int"
        assert wid > 0


def test_format_is_always_present_and_a_single_word_list(overrides):
    """
    The owner's cross-catalog spec, 2026-08-14: every entry says the form the
    media is in. A pipe here would be read as one string by the renderer, not
    split — the auto stamp's `library_formats` is the pipe-separated field, and
    a curated row is one work, one format.
    """
    for row in overrides:
        assert "|" not in row["format"], f"{row['format']!r} — one format per curated row"


def test_no_duplicate_pairs(overrides):
    pairs = [(r["audiobookTitle"], r["libraryWorkId"]) for r in overrides]
    assert len(pairs) == len(set(pairs)), "the same (audiobook, work) pair is listed twice"


def test_every_audiobook_title_exists_verbatim_in_the_catalog(overrides, catalog_titles):
    """
    ⚠️ VERBATIM, not folded. A curated row is a claim about ONE catalogue row;
    matching it loosely would let it claim a family of them, which is precisely
    what the automatic matchers refuse to do. A row naming a book this
    catalogue does not hold renders nothing at all, so it must fail here rather
    than sit in the file looking effective.
    """
    missing = sorted({r["audiobookTitle"] for r in overrides if r["audiobookTitle"] not in catalog_titles})
    assert not missing, f"not in site/catalog.csv: {missing}"


def test_the_wandering_inn_acceptance_case(overrides):
    """
    Owner, 2026-09-02: audiobook Book 1 must reach library works 229 AND 230,
    audiobook Book 2 must reach 231 AND 232 — the two-works-one-audiobook shape
    both matchers correctly refuse. Pinned here as well as in
    `site/__tests__/library-link.test.js` because the two suites protect
    different halves: that one pins what RENDERS, this one pins that the rows
    still name books this catalogue actually holds.
    """
    by_title: dict[str, list[int]] = {}
    for row in overrides:
        by_title.setdefault(row["audiobookTitle"], []).append(row["libraryWorkId"])

    assert by_title["The Wandering Inn - The Wandering Inn, Book 1"] == [229, 230]
    assert by_title["Fae and Fare - The Wandering Inn, Book 2"] == [231, 232]


def test_nothing_else_was_bulk_curated(overrides):
    """
    ⚠️ The owner asked for exactly these four rows and NOTHING else curated.
    This mechanism overrules two deliberate refusals; a row that arrives
    without a person deciding on it is the failure mode, and it would arrive
    silently. Updating this number is the moment to ask who reviewed the new
    row and what they checked.
    """
    assert len(overrides) == 4
