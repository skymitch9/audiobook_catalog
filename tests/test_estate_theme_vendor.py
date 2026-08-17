"""
The drift guard on the vendored estate theme.

WHY IT EXISTS. `hearts` — the estate's fifth theme — shipped into
catalog-platform on 2026-08-16 and reached this site on 2026-08-17, a day late,
only because someone went looking. Nothing failed in between. The appearance
controls in site/account-modal.js already built their list the right way (from
`window.estateTheme.themes`), so the ONLY thing between this site and a new
theme was a human remembering to re-copy two files. Owner order 2026-08-17,
verbatim: "Add the pink theme as an option for every site, when a theme is
added all sites get it some may just default right away."

This file is the "or breaks loudly" half of that. Two kinds of test, and the
difference matters:

  · SELF-CONSISTENCY (always runs, including in CI, which has no sibling
    checkout): the vendored files must agree with EACH OTHER and with the
    modal. This is what catches a HALF-DONE sync — a theme.js offering a theme
    whose palette never arrived, which renders as whichever theme came before
    it rather than as an error.

  · DRIFT VS CANONICAL (needs the catalog-platform checkout, so it SKIPS in
    CI): the vendored copy must be byte-identical to what the sync script
    would write. This is the one that fails the day theme #6 lands upstream.

⚠️ The skip is a real limitation, said out loud rather than hidden: GitHub
Actions runs pytest with no sibling repo, so the drift half only protects a
developer's or an agent's checkout. It is still the right place for it — that
is where re-vendoring happens, and a CI job that cloned a second repo to check
a copy would be a heavier promise than this earns. The self-consistency half
runs everywhere.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.sync_estate_theme import (  # noqa: E402
    SyncError,
    canonical_dir,
    drift,
)

VENDORED_JS = REPO_ROOT / "site" / "static" / "js" / "theme.js"
VENDORED_CSS = REPO_ROOT / "site" / "static" / "css" / "estate-theme.css"
ACCOUNT_MODAL = REPO_ROOT / "site" / "account-modal.js"


def _themes_from_vendored_js() -> list[str]:
    """
    The registry, read out of the vendored switcher.

    A parser rather than a hardcoded list on purpose: a list written here would
    be a FOURTH copy of the theme names, which is the disease under test.
    """
    src = VENDORED_JS.read_text(encoding="utf-8")
    match = re.search(r"var THEMES = \[([^\]]*)\]", src)
    assert match, (
        "no `var THEMES = [...]` in the vendored theme.js. Either the copy is broken, "
        "or canonical renamed its registry and this guard is now blind — fix the guard "
        "before trusting a green suite."
    )
    return [t.strip().strip("'\"") for t in match.group(1).split(",") if t.strip()]


# --------------------------------------------------------------------------- #
# Self-consistency: runs everywhere, including CI.
# --------------------------------------------------------------------------- #


def test_vendored_theme_files_exist_and_are_not_empty():
    for path in (VENDORED_JS, VENDORED_CSS):
        assert path.is_file(), f"{path.name} is missing from site/static — the site would load unstyled."
        assert path.stat().st_size > 0, f"{path.name} is empty — an empty read is a failed read."


def test_every_offered_theme_has_a_palette():
    """
    A theme in the dropdown with no [data-theme=…] block does not fail — it
    renders as whichever theme came before it, which is indistinguishable from
    "the picker is broken" and impossible to notice in a screenshot.
    """
    css = VENDORED_CSS.read_text(encoding="utf-8")
    themes = _themes_from_vendored_js()
    assert themes, "the vendored switcher offers no themes at all"
    missing = [t for t in themes if f'[data-theme="{t}"]' not in css]
    assert not missing, (
        f"theme(s) {missing} are offered by site/static/js/theme.js but have no token block in "
        "site/static/css/estate-theme.css. That is a HALF-DONE re-vendor: run "
        "`python scripts/sync_estate_theme.py` so both files come from the same canonical commit."
    )


def test_every_offered_theme_has_a_human_label():
    js = VENDORED_JS.read_text(encoding="utf-8")
    themes = _themes_from_vendored_js()
    unlabelled = [t for t in themes if not re.search(rf"^\s*{re.escape(t)}:\s*'", js, re.M)]
    assert not unlabelled, (
        f"theme(s) {unlabelled} have no entry in theme.js's LABELS map, so the Appearance "
        "dropdown would show a capitalised id instead of a name."
    )


def test_hearts_is_offered():
    """
    The owner's actual ask, pinned so it cannot silently regress: "Add the pink
    theme as an option for every site" (2026-08-17). If a future re-vendor drops
    `hearts` from the registry, this is the test that says so in words.
    """
    assert "hearts" in _themes_from_vendored_js(), (
        "the vendored switcher no longer offers `hearts` — the pink theme the owner asked "
        "every site to carry on 2026-08-17."
    )
    assert '[data-theme="hearts"]' in VENDORED_CSS.read_text(encoding="utf-8")


def test_account_modal_reads_the_registry_rather_than_holding_one():
    """
    The Appearance section IS this site's theme cog (there is no #hg-cog markup
    here). It must build its options from window.estateTheme.themes; a written
    list there would go stale exactly the way the apex's <option>s did.
    """
    src = ACCOUNT_MODAL.read_text(encoding="utf-8")
    assert "et.themes" in src or "estateTheme.themes" in src, (
        "site/account-modal.js no longer reads window.estateTheme.themes. If it has gone back "
        "to a hardcoded list, that list is a second registry and will go stale."
    )


def test_account_modal_fallback_list_is_a_subset_of_the_registry():
    """
    The no-switcher fallback may lag the registry (it is only for a load where
    theme.js 404'd) but it may never offer a theme that does not exist — that
    would be a dropdown entry no palette answers to.
    """
    src = ACCOUNT_MODAL.read_text(encoding="utf-8")
    match = re.search(r":\s*\[((?:\s*'[a-z]+'\s*,?)+)\]", src)
    assert match, "could not find the fallback theme list in site/account-modal.js"
    fallback = [t.strip().strip("'") for t in match.group(1).split(",") if t.strip()]
    unknown = sorted(set(fallback) - set(_themes_from_vendored_js()))
    assert not unknown, f"the modal's fallback offers theme(s) {unknown} that the switcher does not know"


def test_vendored_css_font_urls_are_relative():
    """
    The one deliberate transformation the sync makes. This site serves pages at
    both `/` and `/dev/`, so canonical's absolute `/assets/fonts/…` would 404 on
    the dev lane and every theme would render in fallback faces.
    """
    css = VENDORED_CSS.read_text(encoding="utf-8")
    assert "url('/assets/fonts/" not in css, (
        "the vendored CSS still points at the apex's absolute font path — the re-rooting in "
        "scripts/sync_estate_theme.py did not run, or was undone by a hand copy."
    )
    assert css.count("url('../fonts/") >= 6, "expected the six self-hosted faces re-rooted to ../fonts/"


def test_vendored_files_carry_a_do_not_edit_banner():
    """A copy nobody can tell is a copy is a copy somebody will edit."""
    for path in (VENDORED_JS, VENDORED_CSS):
        head = path.read_text(encoding="utf-8")[:600]
        assert "DO NOT EDIT" in head, f"{path.name} lost its generated-copy banner"
        assert "catalog-platform" in head, f"{path.name}'s banner no longer names the source of truth"


# --------------------------------------------------------------------------- #
# Drift vs canonical: skips where the sibling checkout is absent (CI).
# --------------------------------------------------------------------------- #


def test_vendored_theme_is_in_step_with_canonical():
    try:
        canonical_dir()
    except SyncError as exc:
        pytest.skip(
            "catalog-platform checkout not available, so canonical cannot be compared. "
            "This is expected in CI and NOT a pass — the self-consistency tests above are "
            f"all that ran. ({exc.args[0].splitlines()[0]})"
        )

    stale = drift()
    assert not stale, (
        "the vendored estate theme has drifted from canonical:\n"
        + "\n".join(f"  - {s}" for s in stale)
        + "\n\nA theme added upstream reaches this site by running:\n"
        "  python scripts/sync_estate_theme.py\n"
        "then committing the result. Do NOT hand-edit site/static/{css/estate-theme.css,js/theme.js} —\n"
        "the fix belongs in catalog-platform so every estate site gets it."
    )
