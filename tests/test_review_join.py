"""Tests for app/core/review_join.py - the review-join key ports and the
live (read-only) review count behind edit_overrides.py's key-move warning
(Phase A2, catalog-platform/docs/info/edit-audit-design.md sec 3.4/6).

The two folds (book_id_from_title, work_key_for) are pinned against the exact
algorithms in site/reviews.js and library_catalog/packages/core/src/titles.ts
- see the docstrings in review_join.py for why they cannot simply import one
implementation. count_reviews_for_book_id is tested against a stubbed
urllib.request.urlopen; nothing here makes a real network call.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.core import review_join as rj


# --------------------------------------------------------------------------- #
# book_id_from_title - mirror of site/reviews.js
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "title,expected",
    [
        ("The Lake House", "the-lake-house"),
        ("Thunderplump", "thunderplump"),
        ("Tamer: King of Dinosaurs", "tamer-king-of-dinosaurs"),
        ("  Leading   & Trailing  ", "leading-trailing"),
        ("Café Society", "caf-society"),  # bookIdFromTitle does NOT fold diacritics - unlike normalise_title
        ("---already---dashed---", "already-dashed"),
        ("", ""),
        (None, ""),
    ],
)
def test_book_id_from_title_mirrors_reviews_js(title, expected):
    assert rj.book_id_from_title(title) == expected


def test_book_id_from_title_matches_the_existing_python_port():
    """app/tools/import_pagebound_reviews.py:book_id() is the same fold,
    ported once already for the same reason (the JS runs in the browser
    only). The two must never drift apart."""
    from app.tools.import_pagebound_reviews import book_id

    samples = ["The Lake House", "Tamer: King of Dinosaurs", "A, B & C!!", ""]
    for title in samples:
        assert rj.book_id_from_title(title) == book_id(title)


# --------------------------------------------------------------------------- #
# normalise_title / primary_author / work_key_for - mirror of titles.ts
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("The Lake House", "lake house"),
        ("A Study in Scarlet", "study in scarlet"),
        ("An Unexpected Journey", "unexpected journey"),
        ("Fish & Chips", "fish and chips"),
        ("Café Society", "cafe society"),  # diacritics ARE folded here, unlike book_id_from_title
        ("  Extra   Space  ", "extra space"),
        ("THUNDERPLUMP", "thunderplump"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalise_title_mirrors_titles_ts(raw, expected):
    assert rj.normalise_title(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Dakota Krout", ["Dakota Krout"]),
        ("Author A, Author B", ["Author A", "Author B"]),
        ("Author A & Author B", ["Author A", "Author B"]),
        ("Author A and Author B", ["Author A", "Author B"]),
        # The role suffix is stripped from whichever segment carries it; the
        # NAME that's left is still a real, non-empty credit and stays -
        # splitAuthors only drops segments that end up EMPTY after stripping.
        ("Jennifer E. Sunseri - Translator", ["Jennifer E. Sunseri"]),
        ("Real Author; Jennifer E. Sunseri - Translator", ["Real Author", "Jennifer E. Sunseri"]),
        ("", []),
    ],
)
def test_split_authors_mirrors_titles_ts(raw, expected):
    assert rj.split_authors(raw) == expected


def test_primary_author_falls_back_to_the_raw_string_only_when_splitting_yields_nothing():
    # A role-only credit still yields a name (see test_split_authors above),
    # so this is NOT the fallback path - it is a normal first-element result.
    assert rj.primary_author("Jennifer E. Sunseri - Translator") == "Jennifer E. Sunseri"
    assert rj.primary_author("Author A, Author B") == "Author A"
    assert rj.primary_author("") == ""
    # A segment that strips down to nothing at all IS the fallback path -
    # split_authors returns [], so the (stripped) raw string is kept rather
    # than silently folding the key onto an empty author half.
    assert rj.split_authors(" - Translator") == []
    assert rj.primary_author(" - Translator") == "- Translator"


def test_work_key_for_matches_the_titles_ts_formula():
    assert rj.work_key_for("The Lake House", "Dakota Krout") == "lake house|dakota krout"
    assert rj.work_key_for("The Lake House", "Author A, Author B") == "lake house|author a"


def test_work_key_for_changes_when_title_or_author_changes():
    """The whole point: this is what edit_overrides.py compares old vs new on."""
    base = rj.work_key_for("Implode", "Dakota Krout")
    assert rj.work_key_for("Implode Retitled", "Dakota Krout") != base
    assert rj.work_key_for("Implode", "A Different Author") != base
    # A pure-tag change that folds identically must NOT look like a move.
    assert rj.work_key_for("THE Implode", "Dakota Krout") == rj.work_key_for("Implode", "Dakota Krout")


# --------------------------------------------------------------------------- #
# count_reviews_for_book_id - read-only, network stubbed
# --------------------------------------------------------------------------- #


def _fake_response(payload):
    body = json.dumps(payload).encode("utf-8")

    class _Resp:
        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Resp()


def test_count_reviews_for_book_id_counts_document_rows(monkeypatch):
    rows = [{"document": {"name": "a"}}, {"document": {"name": "b"}}, {"readTime": "..."}]  # last row: no match, no doc
    monkeypatch.setattr(rj.urllib.request, "urlopen", lambda req, timeout=None: _fake_response(rows))
    assert rj.count_reviews_for_book_id("thunderplump") == 2


def test_count_reviews_for_book_id_returns_zero_for_no_matches(monkeypatch):
    monkeypatch.setattr(rj.urllib.request, "urlopen", lambda req, timeout=None: _fake_response([]))
    assert rj.count_reviews_for_book_id("nothing-here") == 0


def test_count_reviews_for_book_id_is_none_on_network_failure(monkeypatch):
    def _boom(req, timeout=None):
        raise OSError("network unreachable")

    monkeypatch.setattr(rj.urllib.request, "urlopen", _boom)
    assert rj.count_reviews_for_book_id("thunderplump") is None


def test_count_reviews_for_book_id_is_none_on_malformed_response(monkeypatch):
    monkeypatch.setattr(rj.urllib.request, "urlopen", lambda req, timeout=None: _fake_response({"not": "a list"}))
    assert rj.count_reviews_for_book_id("thunderplump") is None


def test_count_reviews_for_book_id_is_none_for_an_empty_book_id():
    assert rj.count_reviews_for_book_id("") is None
    assert rj.count_reviews_for_book_id(None) is None


def test_count_reviews_for_book_id_sends_a_structured_query_not_a_scan(monkeypatch):
    """Read-only and scoped: it must query, never fetch the whole collection."""
    captured = SimpleNamespace(req=None)

    def _capture(req, timeout=None):
        captured.req = req
        return _fake_response([])

    monkeypatch.setattr(rj.urllib.request, "urlopen", _capture)
    rj.count_reviews_for_book_id("thunderplump")
    assert captured.req is not None
    assert captured.req.full_url.endswith(":runQuery?key=" + captured.req.full_url.rsplit("key=", 1)[1])
    sent = json.loads(captured.req.data)
    assert sent["structuredQuery"]["from"] == [{"collectionId": "reviews"}]
    assert sent["structuredQuery"]["where"]["fieldFilter"]["value"]["stringValue"] == "thunderplump"
