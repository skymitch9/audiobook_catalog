"""Cross-language drift guard for the TITLE/KEY functions (normalization item
1), run against catalog-platform/data/title-key-fixtures.json — the shared
contract library_catalog's packages/core/test/title-key-fixtures.test.ts also
runs. There is no shared runtime between a Python static-site pipeline and a
Cloudflare Worker, so there is no shared implementation; the fixtures are what
keep the two honest. Same mechanism as tests/test_universes.py, applied to a
second contract (see that file's own header, and PLATFORM.md §2.3, for why
this class of bug gets its own guard: resolve_author_link / _resolveAuthorFolder
drifted apart once already, silently).

These functions produce PERSISTED keys (work.work_key, Firestore review
document ids) — a failing case here means someone changed one of the ported
functions without migrating stored keys. Fix the PORT or run the migration;
never edit the fixture to match a new implementation.

The data is NOT in this repo. If these tests skip, catalog-platform is not
where this file can see it - set CATALOG_PLATFORM_DIR.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core import review_join as rj
from app.core import universes as uv  # reuses find_platform_dir/ENV_VAR — one resolver, not a second
from app.library_link import clean_audiobook_title, clean_title_with_series
from app.tools.import_pagebound_reviews import book_id as legacy_book_id

PLATFORM_DIR, _TRIED = uv.find_platform_dir()

requires_platform = pytest.mark.skipif(
    PLATFORM_DIR is None,
    reason=f"catalog-platform not found (tried: {'; '.join(_TRIED)}). Set {uv.ENV_VAR}.",
)

FIXTURES = (
    json.loads((PLATFORM_DIR / "data" / "title-key-fixtures.json").read_text(encoding="utf-8"))
    if PLATFORM_DIR
    else {
        "titles": [],
        "bookIds": [],
        "splitAuthorsCases": [],
        "workKeyCases": [],
        "cleanAudiobookTitleCases": [],
        "cleanTitleWithSeriesCases": [],
        "splitSeriesPrefixCases": [],
    }
)


@requires_platform
def test_schema_version_and_not_empty():
    assert FIXTURES["schemaVersion"] == 1
    total = sum(
        len(FIXTURES[k])
        for k in (
            "titles",
            "bookIds",
            "splitAuthorsCases",
            "workKeyCases",
            "cleanAudiobookTitleCases",
            "cleanTitleWithSeriesCases",
        )
    )
    assert total >= 50, f"expected >=50 fixture cases (this repo's functions only), found {total}"


# --------------------------------------------------------------------------- #
# normalise_title — mirror of titles.ts::normaliseTitle
# --------------------------------------------------------------------------- #


@requires_platform
@pytest.mark.parametrize("case", FIXTURES["titles"], ids=lambda c: c["raw"] or "<empty>")
def test_normalise_title_reproduces_every_fixture(case):
    assert rj.normalise_title(case["raw"]) == case["expect"], case["why"]


# --------------------------------------------------------------------------- #
# book_id_from_title — mirror of site/reviews.js::bookIdFromTitle, and its
# two Python ports must agree with EACH OTHER as well as with canon.
# --------------------------------------------------------------------------- #


@requires_platform
@pytest.mark.parametrize("case", FIXTURES["bookIds"], ids=lambda c: c["raw"] or "<empty>")
def test_book_id_from_title_reproduces_every_fixture(case):
    assert rj.book_id_from_title(case["raw"]) == case["expect"], case["why"]


@requires_platform
@pytest.mark.parametrize("case", FIXTURES["bookIds"], ids=lambda c: c["raw"] or "<empty>")
def test_the_second_python_port_agrees_too(case):
    """app/tools/import_pagebound_reviews.py:book_id() is a SEPARATE port of
    the same JS, made for the same reason (the JS runs in the browser only).
    test_review_join.py already spot-checks the two against each other; this
    runs the full shared fixture set through both, not just a handful."""
    assert legacy_book_id(case["raw"]) == case["expect"], case["why"]


@requires_platform
def test_book_id_from_title_and_normalise_title_disagree_on_leading_articles_by_design():
    assert rj.normalise_title("The Lake House") == "lake house"
    assert rj.book_id_from_title("The Lake House") == "the-lake-house"


# --------------------------------------------------------------------------- #
# split_authors — mirror of titles.ts::splitAuthors
# --------------------------------------------------------------------------- #


@requires_platform
@pytest.mark.parametrize("case", FIXTURES["splitAuthorsCases"], ids=lambda c: c["raw"] or "<empty>")
def test_split_authors_reproduces_every_fixture(case):
    assert rj.split_authors(case["raw"]) == case["expect"], case["why"]


# --------------------------------------------------------------------------- #
# work_key_for — mirror of titles.ts::workKeyFor
# --------------------------------------------------------------------------- #


@requires_platform
@pytest.mark.parametrize("case", FIXTURES["workKeyCases"], ids=lambda c: f"{c['title']}|{c['authors']}")
def test_work_key_for_reproduces_every_fixture(case):
    assert rj.work_key_for(case["title"], case["authors"]) == case["expect"], case["why"]


# --------------------------------------------------------------------------- #
# clean_audiobook_title / clean_title_with_series — mirror of titles.ts
# --------------------------------------------------------------------------- #


@requires_platform
@pytest.mark.parametrize("case", FIXTURES["cleanAudiobookTitleCases"], ids=lambda c: c["raw"])
def test_clean_audiobook_title_reproduces_every_fixture(case):
    assert clean_audiobook_title(case["raw"]) == case["expect"], case["why"]


@requires_platform
@pytest.mark.parametrize(
    "case", FIXTURES["cleanTitleWithSeriesCases"], ids=lambda c: f"{c['raw']}::{c['series']}"
)
def test_clean_title_with_series_reproduces_every_fixture(case):
    assert clean_title_with_series(case["raw"], case["series"]) == case["expect"], case["why"]


# --------------------------------------------------------------------------- #
# splitSeriesPrefix has no audiobook port (no OPF ingest path here) — nothing
# to test against in THIS repo. The library side pins it in
# packages/core/test/title-key-fixtures.test.ts; recorded here only so a
# reader of this file knows the omission is deliberate, not an oversight.
# --------------------------------------------------------------------------- #
