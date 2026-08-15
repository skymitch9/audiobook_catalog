"""Cross-language drift guard for the CLUB-ENGINE mirrors (normalization item
2), run against catalog-platform/data/club-fixtures.json — the shared
contract site/__tests__/club-fixtures.test.js also runs against the real
browser-side site/club-reads.js (the canon; it is what the live app runs).
Same mechanism as tests/test_title_key_fixtures.py and tests/test_universes.py:
no shared runtime between a browser ES module and a Python static-site
pipeline, so a fixture file is what keeps the two honest instead.

⚠️ Building this fixture file caught a REAL divergence, fixed the same day —
see club-fixtures.json's own "_divergenceFound2026_08_14" field and
app/club_announcements.py's member_position docstring for the full account.
member_position() used to infer `chaptered` from milestone shape instead of
taking it as an explicit argument the way memberSchedulePosition does; two
adversarial fixture cases below (still present, still asserted) are exactly
the two inputs that exposed it.

The data is NOT in this repo. If these tests skip, catalog-platform is not
where this file can see it - set CATALOG_PLATFORM_DIR.
"""

from __future__ import annotations

import json

import pytest

from app.club_announcements import (
    _due_of,
    member_position,
    past_due_positions,
    tally_poll_votes,
    tally_ratings,
)
from app.core import universes as uv  # reuses find_platform_dir/ENV_VAR — one resolver

PLATFORM_DIR, _TRIED = uv.find_platform_dir()

requires_platform = pytest.mark.skipif(
    PLATFORM_DIR is None,
    reason=f"catalog-platform not found (tried: {'; '.join(_TRIED)}). Set {uv.ENV_VAR}.",
)

FIXTURES = (
    json.loads((PLATFORM_DIR / "data" / "club-fixtures.json").read_text(encoding="utf-8"))
    if PLATFORM_DIR
    else {"positionCases": [], "statusCases": [], "ratingsCases": [], "pollCases": []}
)


@requires_platform
def test_schema_version_and_not_empty():
    assert FIXTURES["schemaVersion"] == 1
    total = sum(
        len(FIXTURES[k]) for k in ("positionCases", "statusCases", "ratingsCases", "pollCases")
    )
    assert total >= 30, f"expected >=30 fixture cases, found {total}"


# --------------------------------------------------------------------------- #
# member_position — mirror of memberSchedulePosition(milestones, progress,
# chaptered). `chaptered` is passed explicitly, exactly as canon does, so the
# two ADVERSARIAL cases exercise the fix rather than the old inference.
# --------------------------------------------------------------------------- #


@requires_platform
@pytest.mark.parametrize("case", FIXTURES["positionCases"], ids=lambda c: c["name"])
def test_member_position_reproduces_every_fixture(case):
    got = member_position(case["milestones"], case["progress"], case["chaptered"])
    assert got == case["expectPosition"], case.get("why", case["name"])


# --------------------------------------------------------------------------- #
# scheduleStatus has no literal Python mirror — app/club_announcements.py
# only needs the AGGREGATE ("N of M readers on track"), built in
# on_track_summary() from past_due_positions() + member_position(). This
# reconstructs scheduleStatus's per-member status/behindBy from those same
# two real functions (not a new production implementation) and asserts the
# composition reproduces canon exactly, the same composition on_track_summary
# performs internally per member.
# --------------------------------------------------------------------------- #


def _has_schedule(milestones):
    return any(_due_of(m) is not None for m in milestones or [])


def _status_for_member(milestones, progress, chaptered, now_ms):
    if not _has_schedule(milestones):
        return {"status": "none", "behindBy": 0}
    mlist = milestones or []
    last = max((int(m.get("position", 0)) for m in mlist), default=-1)
    pos = member_position(mlist, progress, chaptered)
    if (progress and progress.get("finished")) or (pos >= 0 and pos >= last):
        return {"status": "done", "behindBy": 0}
    due_positions = past_due_positions(mlist, now_ms)
    behind_by = sum(1 for d in due_positions if d > pos)
    return {"status": "behind", "behindBy": behind_by} if behind_by > 0 else {"status": "on-track", "behindBy": 0}


@requires_platform
@pytest.mark.parametrize("case", FIXTURES["statusCases"], ids=lambda c: c["name"])
def test_has_schedule_reproduces_every_fixture(case):
    assert _has_schedule(case["milestones"]) == case["expectHasSchedule"], case["name"]


@requires_platform
@pytest.mark.parametrize("case", FIXTURES["statusCases"], ids=lambda c: c["name"])
def test_schedule_status_composition_reproduces_every_fixture(case):
    got = _status_for_member(case["milestones"], case["progress"], case["chaptered"], case["nowMs"])
    assert got == case["expectStatus"], case["name"]


# --------------------------------------------------------------------------- #
# tally_ratings — mirror of tallyRatings(ratings) -> {average, count}.
# Python returns a (average, count) TUPLE, not an object; same values.
# --------------------------------------------------------------------------- #


@requires_platform
@pytest.mark.parametrize("case", FIXTURES["ratingsCases"], ids=lambda c: c["name"])
def test_tally_ratings_reproduces_every_fixture(case):
    average, count = tally_ratings(case["ratings"])
    assert average == case["expect"]["average"], case["name"]
    assert count == case["expect"]["count"], case["name"]


# --------------------------------------------------------------------------- #
# tally_poll_votes — mirror of tallyPollVotes(options, votes) -> {counts,
# total}. Python returns counts ALONE (a list); total is re-derivable as
# sum(counts) — poll_winners() uses any(counts) instead of a stored total,
# a shape difference, not a logic divergence (see club-fixtures.json's
# _implementations note).
# --------------------------------------------------------------------------- #


@requires_platform
@pytest.mark.parametrize("case", FIXTURES["pollCases"], ids=lambda c: c["name"])
def test_tally_poll_votes_reproduces_every_fixture(case):
    counts = tally_poll_votes(case["options"], case["votes"])
    assert counts == case["expect"]["counts"], case["name"]
    assert sum(counts) == case["expect"]["total"], case["name"]
