"""Descriptions for files that carry NONE (2026-11-24).

136 library files had no description tag — every one an ffmpeg remux whose
source part carried none — so the catalogue showed no blurb. The Audible
backfill now carries Audible's publisher summary for them, FILL-ONLY: a real
description tag always wins. Nothing here reads the real library.
"""

from __future__ import annotations

import json

from app import metadata as md
from app.core import audible_backfill as ab
from app.tools import build_audible_backfill as b

FILE = {"rel": "A/B.m4b", "nam": "Ship of Magic: The Liveship Traders, Book 1", "art": "Robin Hobb",
        "cdek": "", "gen": "Science Fiction & Fantasy:Fantasy", "day": "1998", "sec": 2120 * 60}
EXPORTS = {"A1": {"asin": "A1", "title": "Ship of Magic", "subtitle": "The Liveship Traders, Book 1",
                  "authors": "Robin Hobb", "runtime_length_min": 2120, "genres": "", "release_date": "1998-03-01"}}


def test_description_is_empty_without_an_entry_or_desc():
    assert ab.description(None) == ""
    assert ab.description({"asin": "A1"}) == ""
    assert ab.description({"asin": "A1", "desc": "  A tale.  "}) == "A tale."


def test_load_summaries_reads_only_the_public_summary_as_plain_text(tmp_path):
    p = tmp_path / "lib.json"
    p.write_text(json.dumps({"items": [
        {"asin": "A1", "publisher_summary": "<p>One &amp; two.</p><p>Three.</p>", "purchase_date": "2020-01-01"},
        {"asin": "A2", "publisher_summary": ""},
    ]}), encoding="utf-8")
    assert b.load_summaries([p]) == {"A1": "One & two.\n\nThree."}


def test_builder_gives_desc_only_to_a_file_with_none():
    no_desc = dict(FILE, has_desc=False)
    entries, unmatched, _ = b.build([no_desc], EXPORTS, {"A1": "A tale."})
    assert unmatched == [] and len(entries) == 1
    assert entries[0]["desc"] == "A tale." and entries[0]["match"] == {"title_tag": FILE["nam"], "artist_tag": "Robin Hobb"}

    # a file that HAS a description, and no placeholder, gets no entry at all
    has = dict(FILE, has_desc=True)
    assert b.build([has], EXPORTS, {"A1": "A tale."})[0] == []

    # no summaries passed: description-only candidates are not even considered
    assert b.build([no_desc], EXPORTS)[0] == []

    # Audible has no summary for it: no entry (nothing to fill)
    assert b.build([no_desc], EXPORTS, {"OTHER": "x"})[0] == []


def test_a_placeholder_file_with_a_real_description_gets_no_desc():
    placeholder = dict(FILE, gen="Audiobook", has_desc=True)
    entries, _, _ = b.build([placeholder], EXPORTS, {"A1": "A tale."})
    assert len(entries) == 1 and "desc" not in entries[0]


def test_extract_metadata_prefers_the_real_tag(monkeypatch):
    entry = {"asin": "A1", "desc": "From Audible."}
    monkeypatch.setattr(md, "_backfill_entry", lambda tags: entry)
    assert (md._extract_description({"\xa9cmt": ["<b>Own</b> blurb"]})
            or ab.description(md._backfill_entry({}))) == "Own blurb"
    assert (md._extract_description({}) or ab.description(md._backfill_entry({}))) == "From Audible."
