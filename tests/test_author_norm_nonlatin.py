"""Step 2 of the author matcher must not file one non-Latin author into
another's folder (fix 2026-11-24).

Step 4 (thefuzz) is not the risk for these: two different non-Latin names
score 0 there (measured, thefuzz 0.22.1).

The old normaliser stripped everything outside a-z/0-9, so every non-Latin
name became "" and matched the first non-Latin folder. Real incident,
2026-11-23: '五示正司' (Loner Life in Another World) was uploaded into the
'りゅうせんひろつぐ' folder, a different author.
"""

from __future__ import annotations

from scripts import sync_to_drive as sync


def _resolve(author, folders, monkeypatch):
    # No model, no human: what the deterministic steps alone decide.
    monkeypatch.setattr(sync, "CLAUDE_API_KEY", "")
    monkeypatch.setattr(sync, "is_interactive", lambda: False, raising=False)
    return sync.resolve_author_to_drive_folder(author, folders, dry_run=True)


def test_two_different_japanese_names_do_not_match(monkeypatch):
    folders = {"りゅうせんひろつぐ": "id-ryusen", "Brandon Sanderson": "id-bs"}
    got = _resolve("五示正司", folders, monkeypatch)
    assert not (isinstance(got, tuple) and got[1] == "id-ryusen")


def test_same_japanese_name_with_spacing_still_matches(monkeypatch):
    folders = {"りゅうせん ひろつぐ": "id-ryusen"}
    got = _resolve("りゅうせんひろつぐ", folders, monkeypatch)
    assert isinstance(got, tuple) and got[1] == "id-ryusen"


def test_latin_punctuation_variant_still_matches(monkeypatch):
    folders = {"J.R.R. Tolkien": "id-t"}
    got = _resolve("JRR Tolkien", folders, monkeypatch)
    assert isinstance(got, tuple) and got[1] == "id-t"
