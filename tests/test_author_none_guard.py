"""The AI author-matcher's NONE guard is REAL (review fix, 2026-11-24).

Before: a NONE was honoured when ``scored[0][0] in verdict.shown`` — always
true, so the model alone decided whether a near-identical author got a second
Drive folder. Now a NONE is honoured only when the closest existing folder
scores below ``CLAUDE_NONE_TRUSTED_BELOW`` (90), and a deterministic
contributor-set pre-check files role/order variants before the model is asked.

Real thefuzz scores throughout (no score monkeypatching), so the numbers the
bound was chosen from are pinned here. No real network: requests.post is faked.
"""

from __future__ import annotations

import pytest
from thefuzz import fuzz

from scripts import sync_to_drive as sync


class _Resp:
    status_code = 200

    def __init__(self, text):
        self.text = text

    def json(self):
        return {"content": [{"text": self.text}]}


@pytest.fixture
def claude(monkeypatch):
    """Fake the Messages API. ``claude.answer`` sets the reply; ``claude.calls``
    counts how often the model was asked."""
    import requests

    class _State:
        answer = "NONE"
        calls = 0

    state = _State()

    def post(url, headers=None, json=None, timeout=None):
        state.calls += 1
        return _Resp(state.answer)

    monkeypatch.setattr(sync, "CLAUDE_API_KEY", "test-key")
    monkeypatch.setattr(requests, "post", post)
    return state


@pytest.fixture
def headless(monkeypatch):
    monkeypatch.setattr(sync, "NON_INTERACTIVE", True)
    monkeypatch.setattr("builtins.input", lambda *a, **k: pytest.fail("must not prompt"))


def _score(a, b):
    return fuzz.token_sort_ratio(a.lower(), b.lower())


def test_the_measured_scores_the_bound_rests_on():
    """If thefuzz's scoring ever drifts, the bound's evidence is gone — fail loudly."""
    assert _score("Alex Maher", "Alex Aster") == 80          # distinct people (2026-11-23)
    assert _score("Stephen Hawking", "Stephen King") == 89   # distinct people, both in the library
    assert _score("Kurt Vonnegut Jr.", "Kurt Vonnegut") == 90  # same person
    assert sync.CLAUDE_NONE_TRUSTED_BELOW == 90


def test_none_at_80_still_creates_a_new_author(headless, claude):
    """The case that motivated NO_MATCH must not regress."""
    assert sync.resolve_author_to_drive_folder("Alex Maher", {"Alex Aster": "fid"}) is None
    assert claude.calls == 1


def test_none_at_89_distinct_people_creates_a_new_author(headless, claude):
    assert sync.resolve_author_to_drive_folder("Stephen Hawking", {"Stephen King": "fid"}) is None


def test_none_at_90_is_overruled_to_review_headless(headless, claude, capsys):
    result = sync.resolve_author_to_drive_folder("Kurt Vonnegut Jr.", {"Kurt Vonnegut": "fid"})
    assert isinstance(result, sync.AmbiguousAuthorFolder)
    assert (result.best_name, result.score) == ("Kurt Vonnegut", 90)
    out = capsys.readouterr().out
    assert "not trusting it to create a second folder" in out
    assert "[REVIEW]" in out
    assert "new author" not in out


def test_none_at_90_prompts_a_human_when_one_is_attached(monkeypatch, claude):
    monkeypatch.setattr(sync, "is_interactive", lambda: True)
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or "y")
    result = sync.resolve_author_to_drive_folder("Kurt Vonnegut Jr.", {"Kurt Vonnegut": "fid"})
    assert result == ("Kurt Vonnegut", "fid")
    assert len(asked) == 1


def test_a_match_answer_at_90_is_still_used(headless, claude):
    claude.answer = "Kurt Vonnegut"
    assert sync.resolve_author_to_drive_folder(
        "Kurt Vonnegut Jr.", {"Kurt Vonnegut": "fid"}) == ("Kurt Vonnegut", "fid")


# --- deterministic contributor-set pre-check ---------------------------------


@pytest.mark.parametrize("author, folder", [
    # real confirmed variants from author_drive_map.json, scores 85-91
    ("Nadya Lee, Alex Toxic, Dan Veksler", "Alex Toxic, Nadya Lee, Dan Veksler - Translator"),
    ("Oleg Sapphire, Alexey Kovtunov, Jennifer E. Sunseri",
     "Alexey Kovtunov, Oleg Sapphire, Jennifer E. Sunseri -translator"),
    ("Nadya Lee, Alex Toxic, Dan Veksler -translated By", "Nadya Lee, Alex Toxic, Dan Veksler"),
    ("Joe Harris, Chris Carter, Dirk Maggs - Adaptation", "Joe Harris, Chris Carter, Dirk Maggs"),
])
def test_same_contributors_file_without_asking_the_model(headless, claude, author, folder):
    result = sync.resolve_author_to_drive_folder(author, {folder: "fid", "Stephen King": "x"})
    assert result == (folder, "fid")
    assert claude.calls == 0


def test_two_folders_with_the_same_people_go_to_review_not_a_pick(headless, claude):
    folders = {
        "Alex Toxic, Nadya Lee, Dan Veksler": "a",
        "Nadya Lee, Alex Toxic, Dan Veksler - Translator": "b",
    }
    result = sync.resolve_author_to_drive_folder("Dan Veksler, Alex Toxic, Nadya Lee", folders)
    assert isinstance(result, sync.AmbiguousAuthorFolder)
    assert result.best_name in folders
    assert claude.calls == 0


@pytest.mark.parametrize("a, b", [
    ("Martin Luther King Jr.", "Martin Luther King Sr."),
    ("Daisuke Aizawa", "Daisuke Aizawa, Touzai"),
    ("Stephen Hawking", "Stephen King"),
    ("りゅうせんひろつぐ", "五示正司"),  # non-Latin must not collapse to the same empty key
])
def test_contributor_key_keeps_different_people_apart(a, b):
    assert sync._contributor_key(a) != sync._contributor_key(b)
    assert sync._contributor_key(a)


def test_contributor_key_of_punctuation_only_is_empty():
    assert sync._contributor_key(" - ") == frozenset()
