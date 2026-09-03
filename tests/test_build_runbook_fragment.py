"""
The runbook fragment generator — scripts/build_runbook_fragment.py.

⚠️ WHY THIS EXISTS. Measured 2026-09-02: the published runbooks carried NO
heading ids, so every in-page link the markdown had (`[Option E](#option-e--…)`,
`[§4C](#c-standing-access--…)`) worked in a markdown preview and was dead on
heygabi.ai — for two weeks, on the one page written for a person who is not us.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from build_runbook_fragment import anchor_headings, number_headings  # noqa: E402


def test_headings_get_the_id_github_would():
    """The docs' links were written for GitHub's slug rule, so that is the rule."""
    body = (
        "<h2>4. ⚠️ THE ONE THING STILL BROKEN — and the last favour</h2>\n"
        "<h3>C. Standing access — so this is the last message</h3>\n"
        "<h3>Option E — Tailscale + a limited SSH user <em>(gives: a shell on the box)</em></h3>"
    )
    out = anchor_headings(body)
    assert 'id="4--the-one-thing-still-broken--and-the-last-favour"' in out
    assert 'id="c-standing-access--so-this-is-the-last-message"' in out
    assert 'id="option-e--tailscale--a-limited-ssh-user-gives-a-shell-on-the-box"' in out


def test_duplicate_headings_get_distinct_ids():
    out = anchor_headings("<h3>What to send back</h3><h3>What to send back</h3>")
    assert 'id="what-to-send-back"' in out and 'id="what-to-send-back-1"' in out


def test_section_numbers_survive_the_id_attribute():
    """number_headings used to match a bare `<h2>`; with ids it must still fire."""
    out = number_headings(anchor_headings("<h2>2. Fix the shape</h2>"))
    assert out == '<h2 id="2-fix-the-shape"><span class="n">2</span> Fix the shape</h2>'
