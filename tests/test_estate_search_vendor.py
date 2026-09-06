"""
The drift guard on the vendored estate SEARCH component.

WHY IT EXISTS. `site/estate/estate-search.js` was the estate's FOURTH copy of
one component and the only one nobody synced. Measured 2026-09-05
(`catalog-platform/docs/info/multi-library-survey-2026-09-05.md` §5) it was 23
lines behind upstream with four divergences, and NOTHING HAD FAILED: the drift
was found by an audit, not by use. Two of the four were user-visible behaviour —
the hit COVER was an inert `aria-hidden` span rather than a link (clicking it
did nothing), and `SOURCE_LABELS` had no `library2`, so a second household's
shelf could not be named on this site at all. That second one is the owner's
2026-09-05 multi-library rule landing on this surface.

This file is the "or breaks loudly" half. `scripts/sync_estate_search.py` is the
other half, and its header carries the full argument for why a SCRIPT plus a
TEST rather than a prebuild step (short version: `site/` is served straight out
of the repo, so the copy must stay TRACKED, and a sync on `pretest` would
rewrite tracked files mid-test and fight the pipeline's auto-commit for the
tree). Same shape as `tests/test_estate_theme_vendor.py`, deliberately.

Two kinds of test, and the difference matters:

  · SELF-CONSISTENCY (always runs, including in CI, which has no sibling
    checkout): the vendored file must be present, tracked-shaped, parseable as
    the component, and must carry its generated banner. This catches a
    half-done or hand-edited copy.

  · DRIFT VS CANONICAL (needs the catalog-platform checkout, so it SKIPS in
    CI): the vendored copy must be byte-identical to what the sync script would
    write. This is the one that fails the day the component changes upstream.

⚠️ The skip is a real limitation, said out loud rather than hidden: GitHub
Actions runs pytest with no sibling repo, so the drift half only protects a
developer's or an agent's checkout. It is still the right place for it — that is
where re-vendoring happens — and a CI job that cloned a second repo to check a
copy would be a heavier promise than this earns.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.sync_estate_search import (  # noqa: E402
    BANNER_LINES,
    JS_DEST,
    PROVENANCE_DEST,
    SyncError,
    canonical_file,
    drift,
)

VENDORED = REPO_ROOT / JS_DEST
PROVENANCE = REPO_ROOT / PROVENANCE_DEST


# ---------------------------------------------------------------------------
# Self-consistency — always runs, CI included
# ---------------------------------------------------------------------------


def test_the_vendored_component_is_present_and_tracked_shaped():
    """⚠️ `site/` is served straight out of the repo. A missing component is a
    search box that does not exist on the live page, and the failure looks like
    a styling bug rather than a missing file."""
    assert VENDORED.is_file(), f"{JS_DEST.as_posix()} is missing — run scripts/sync_estate_search.py"
    assert PROVENANCE.is_file(), f"{PROVENANCE_DEST.as_posix()} is missing"


def test_it_is_actually_the_component():
    """A cheap sanity check, not a schema: a file that no longer defines the
    custom element is not `<estate-search>`, whatever its name says."""
    body = VENDORED.read_text(encoding="utf-8")
    assert "customElements.define" in body
    assert "estate-search" in body


def test_it_carries_the_generated_banner_and_says_where_to_edit():
    """⚠️ The banner is the only thing standing between a well-meaning fix here
    and a fix that dies at this repo. It must name the upstream path AND the
    refresh command, because a warning with no instruction gets ignored."""
    head = VENDORED.read_text(encoding="utf-8").splitlines()[:BANNER_LINES]
    banner = "\n".join(head)
    assert "DO NOT EDIT" in banner
    assert "catalog-platform/sites/heygabi-home/public/assets/estate-search.js" in banner
    assert "scripts/sync_estate_search.py" in banner


def test_the_provenance_note_points_at_the_script_not_a_hand_recipe():
    """The old note carried a `sed`/`cat`/`sha256sum` hand recipe. It was
    followed once, on 2026-08-17, and never again — which is how the copy ended
    up 23 lines behind. The note now names the script."""
    note = PROVENANCE.read_text(encoding="utf-8")
    assert "python scripts/sync_estate_search.py" in note
    assert "Upstream sha256" in note


def test_the_two_sibling_modules_stay_unvendored():
    """⚠️ `estate-auth.js` is deliberately NOT vendored: vendoring it would
    invite a SECOND Firebase app, the exact hazard `estate-search-mount.js`'s
    adapter exists to avoid. `estate-scan.js` is not vendored because this embed
    sets no `scan` attribute. Both decisions predate the sync script and it must
    not quietly reverse them by growing its file list."""
    estate_dir = REPO_ROOT / "site" / "estate"
    present = {p.name for p in estate_dir.iterdir() if p.is_file()}
    assert "estate-auth.js" not in present
    assert "estate-scan.js" not in present


def test_the_component_is_not_in_the_pipeline_commit_allowlist():
    """⚠️ `_ALLOWLIST` in scripts/sync_to_drive.py is data the PIPELINE
    generates, mirrored in .github/workflows/auto-promote.yml's `allow=` regex.
    This file is vendored SOURCE a person commits. Adding it there would make
    every re-vendor look like a book-only auto-commit and SELF-PROMOTE TO PROD,
    which is not a decision a `git add` may make."""
    body = (REPO_ROOT / "scripts" / "sync_to_drive.py").read_text(encoding="utf-8")
    start = body.index("_ALLOWLIST = [")
    allowlist = body[start : body.index("]", start)]
    assert "estate-search" not in allowlist


# ---------------------------------------------------------------------------
# Drift vs canonical — skips without the sibling checkout
# ---------------------------------------------------------------------------


def _canonical_or_skip() -> Path:
    try:
        return canonical_file()
    except SyncError as exc:  # pragma: no cover - depends on the machine
        pytest.skip(f"no catalog-platform checkout on this machine: {exc}")


def test_the_vendored_copy_is_in_step_with_canonical():
    """⚠️ THE ONE THAT FAILS THE DAY THE COMPONENT CHANGES UPSTREAM.

    A change to `<estate-search>` reaches the apex immediately, the library and
    games repos on their next build, and this site only when somebody runs the
    script. Before this test, "somebody remembers" was the whole mechanism, and
    it held for exactly one day."""
    _canonical_or_skip()
    stale = drift()
    assert stale == [], (
        "the vendored estate search component has drifted from canonical: "
        + ", ".join(stale)
        + " — fix: python scripts/sync_estate_search.py, then commit the result"
    )


def test_the_copy_is_upstream_verbatim_below_the_banner():
    """⚠️ VERBATIM IS THE CONTRACT. The theme sync re-roots font URLs and says
    so loudly; this one transforms NOTHING, so any difference below the banner
    is drift or a hand edit, never an intended local change."""
    src = _canonical_or_skip()
    upstream = src.read_text(encoding="utf-8").replace("\r\n", "\n")
    vendored = VENDORED.read_text(encoding="utf-8").replace("\r\n", "\n")
    below = "\n".join(vendored.splitlines()[BANNER_LINES:])
    assert below == upstream.rstrip("\n") or below.rstrip("\n") == upstream.rstrip("\n")


def test_the_four_measured_divergences_are_gone():
    """The four hunks the 2026-09-05 survey measured (§5), each pinned by what
    it BROKE rather than by a line number — line numbers move, behaviour does
    not.

      1. the banner (kept, and covered above);
      2. 🔴 `SOURCE_LABELS` could not name `library2` — now the labels come from
         the estate registry, so a second household's shelf is named here;
      3. the missing `a.es-hit-cover` cursor + focus-visible rules;
      4. 🔴 the cover was an inert `aria-hidden` span — clicking it did nothing.
    """
    _canonical_or_skip()
    body = VENDORED.read_text(encoding="utf-8")

    # 2 — the registry, and the WORDED unknown that replaced `MAP[x] || x`.
    assert "/api/catalogs" in body, "the component no longer reads the estate registry"
    assert "a shelf we cannot name" in body, "the worded unknown is gone; a raw id would print"

    # 3 — the cover's own affordance.
    assert "a.es-hit-cover { cursor: pointer; }" in body
    assert "a.es-hit-cover:focus-visible" in body

    # 4 — the cover is a link routed through _openHit, so this site's own
    #     `estate-search:select` hash routing still fires on it.
    assert "createElement(linkUrl ? 'a' : 'span')" in body
    assert "this._openHit(linkUrl, row)" in body
