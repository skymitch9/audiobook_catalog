"""The Discord post counts audiobooks and ebooks SEPARATELY (2026-10-02).

Until then the embed said "**N** total books", and N was `len(catalog.csv)` —
audiobooks only. Ebooks were counted nowhere, while the label claimed the
whole library. Owner: "next to the audiobook counter add an ebook counter too".

The ebook number comes from the tracked `site/ebooks_status.json`; the list
itself is gitignored (the shelf is permission-gated), so the count is the one
ebook fact CI can see. An unavailable count is None and prints NOTHING — never
"0 ebooks".
"""

from __future__ import annotations

import json

from app.tools import detect_new_books as detect
from app.tools.send_discord_notification import create_embed

SITE = "https://audiobooks.heygabi.ai/"
BOOK = {"title": "T", "author": "A"}


def _stats(embeds) -> str:
    return next(f["value"] for f in embeds[0]["fields"] if f["name"] == "📊 Library Stats")


def test_new_books_embed_shows_both_counters():
    embeds = create_embed(
        {"new_count": 1, "total_count": 1113, "ebook_count": 244, "books": [BOOK]}, SITE
    )
    assert _stats(embeds) == "**1113** audiobooks\n**244** ebooks\n**1** new additions"


def test_unknown_ebook_count_prints_no_ebook_line():
    embeds = create_embed(
        {"new_count": 1, "total_count": 1113, "ebook_count": None, "books": [BOOK]}, SITE
    )
    assert _stats(embeds) == "**1113** audiobooks\n**1** new additions"
    # An older new_books.json with no ebook_count key reads the same way.
    embeds = create_embed({"new_count": 1, "total_count": 1113, "books": [BOOK]}, SITE)
    assert "ebooks" not in _stats(embeds)


def test_zero_ebooks_is_a_real_count_and_is_shown():
    embeds = create_embed(
        {"new_count": 1, "total_count": 5, "ebook_count": 0, "books": [BOOK]}, SITE
    )
    assert "**0** ebooks" in _stats(embeds)


def test_refresh_embed_shows_both_counters():
    embeds = create_embed({"new_count": 0, "total_count": 1113, "ebook_count": 244, "books": []}, SITE)
    assert embeds[0]["description"] == (
        "Catalog refreshed with **1113** audiobooks and **244** ebooks."
    )


def test_load_ebook_count_reads_the_tracked_status_file(tmp_path, monkeypatch):
    status = tmp_path / "ebooks_status.json"
    status.write_text(json.dumps({"count": 244, "needs_human_cover_count": 0}), encoding="utf-8")
    monkeypatch.setattr(detect, "EBOOK_STATUS_PATH", status)
    assert detect.load_ebook_count() == 244


def test_load_ebook_count_is_none_not_zero_when_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(detect, "EBOOK_STATUS_PATH", tmp_path / "missing.json")
    assert detect.load_ebook_count() is None

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(detect, "EBOOK_STATUS_PATH", bad)
    assert detect.load_ebook_count() is None

    odd = tmp_path / "odd.json"
    odd.write_text(json.dumps({"count": "244"}), encoding="utf-8")
    monkeypatch.setattr(detect, "EBOOK_STATUS_PATH", odd)
    assert detect.load_ebook_count() is None
