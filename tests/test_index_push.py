# tests/test_index_push.py
# The shared-index projection (app/index_push.py): allow-list, raw strings,
# and the cover-URL canonicalisation that mirrors site/covers-base.js.
#
# ⚠️ What is deliberately NOT tested: any fold/normalisation of titles or
# authors — there is none. The index Worker folds on write (index-worker
# design §6); these tests assert the pusher sends RAW strings untouched.

import json

import pytest

from app.index_push import (
    ALLOWED_KEYS,
    build_projection,
    canonical_cover_url,
    detail_url_for,
    push_after_build,
)

BASE = "https://covers.heygabi.ai/"


def row(**overrides):
    """A catalog.csv row as written by app/writers.py (all 12 columns)."""
    r = {
        "title": "Avenging Home - The Survivalist Series, Book 7",
        "series": "The Survivalist Series",
        "series_index_display": "7",
        "series_index_sort": "7.0",
        "author": "A. American",
        "narrator": "Duke Fontaine",
        "year": "2016-06-14",
        "genre": "Literature & Fiction:Action & Adventure",
        "duration_hhmm": "10:07",
        "cover_href": "covers/A. American/Avenging Home - The Survivalist Series, Book 7.jpg",
        "companion_files": "",
        "desc": "Ownership-adjacent prose that must never travel.",
    }
    r.update(overrides)
    return r


# --------------------------------------------------------------------------- #
# Cover URL canonicalisation — the covers-base.js coverUrl() mirror
# --------------------------------------------------------------------------- #


def test_raw_href_is_encoded_once():
    assert canonical_cover_url("covers/A. American/Home.jpg", BASE) == BASE + "A.%20American/Home.jpg"


def test_preencoded_href_is_not_double_encoded():
    # The 2026-08-13 lesson: %20 must NOT become %2520.
    assert canonical_cover_url("covers/J.R.%20Mathews/Book.jpg", BASE) == BASE + "J.R.%20Mathews/Book.jpg"
    # …and it lands on the same canonical URL as its raw twin.
    assert canonical_cover_url("covers/J.R.%20Mathews/Book.jpg", BASE) == canonical_cover_url(
        "covers/J.R. Mathews/Book.jpg", BASE
    )


def test_literal_percent_stays_raw_then_encodes():
    # No valid %XX pair: not decoded (JS decodeURIComponent would throw), then
    # the literal % is encoded exactly once.
    assert canonical_cover_url("covers/50% off/x.jpg", BASE) == BASE + "50%25%20off/x.jpg"


def test_mixed_valid_pair_and_lone_percent_is_left_raw():
    # decodeURIComponent("A%20B% C") throws → JS keeps the whole value raw.
    # The mirror: any lone % means no decode at all.
    assert canonical_cover_url("covers/A%20B% C/x.jpg", BASE) == BASE + "A%2520B%25%20C/x.jpg"


def test_absolute_and_data_hrefs_pass_through():
    for href in ("https://elsewhere.example/c.jpg", "http://x/c.jpg", "//host/c.jpg", "data:image/png;base64,AA"):
        assert canonical_cover_url(href, BASE) == href


def test_empty_href_is_empty():
    assert canonical_cover_url("", BASE) == ""
    assert canonical_cover_url(None, BASE) == ""
    assert canonical_cover_url("   ", BASE) == ""


def test_quote_encodes_the_chars_encodeuricomponent_leaves():
    # quote(safe='/') percent-encodes !'()* — the JS side adds a replace() to
    # match Python, so Python must actually produce the encoded forms.
    assert canonical_cover_url("covers/a!'()*.jpg", BASE) == BASE + "a%21%27%28%29%2A.jpg"


# --------------------------------------------------------------------------- #
# Detail URL — the site's only book anchor is the hash search (#q=…)
# --------------------------------------------------------------------------- #


def test_detail_url_uses_hash_query_with_urlsearchparams_encoding():
    url = detail_url_for("Dungeon Crawler Carl", site_url="https://audiobooks.heygabi.ai/")
    # urlencode == URLSearchParams.toString(): space → '+'.
    assert url == "https://audiobooks.heygabi.ai/#q=Dungeon+Crawler+Carl"


def test_detail_url_encodes_reserved_characters():
    url = detail_url_for("Fire & Ice #2", site_url="https://audiobooks.heygabi.ai")
    assert url == "https://audiobooks.heygabi.ai/#q=Fire+%26+Ice+%232"


# --------------------------------------------------------------------------- #
# The projection — default-deny, raw strings, stable ids
# --------------------------------------------------------------------------- #


def test_projection_keys_are_exactly_the_allow_list():
    (p,) = build_projection([row()])
    assert set(p.keys()) == ALLOWED_KEYS


def test_ownership_and_personal_fields_never_travel():
    (p,) = build_projection([row()])
    dumped = json.dumps(p)
    for verboten in ("narrator", "duration", "companion", "desc", "Ownership-adjacent"):
        assert verboten not in dumped


def test_titles_and_authors_are_raw_not_folded():
    (p,) = build_projection([row(title="The KAIJU Preservation Society!", author="John Scalzi")])
    assert p["title"] == "The KAIJU Preservation Society!"  # untouched, articles and all
    assert p["creator"] == "John Scalzi"


def test_source_id_is_the_catalog_book_key():
    (p,) = build_projection([row()])
    assert p["source_id"] == "Avenging Home - The Survivalist Series, Book 7|A. American"


def test_field_shapes():
    (p,) = build_projection([row()])
    assert p["format"] == "audiobook"
    assert p["year"] == 2016  # from '2016-06-14'
    assert p["series_index"] == 7.0
    assert p["series"] == "The Survivalist Series"
    assert p["cover_url"].startswith("https://covers.heygabi.ai/")
    assert "%20" in p["cover_url"] and "%2520" not in p["cover_url"]
    assert p["detail_url"].startswith("https://audiobooks.heygabi.ai/#q=")


def test_empty_optionals_become_none_not_empty_string():
    (p,) = build_projection([row(series="", series_index_sort="", year="", cover_href="", author="")])
    assert p["series"] is None
    assert p["series_index"] is None
    assert p["year"] is None
    assert p["cover_url"] is None
    assert p["creator"] is None  # the strict schema refuses '' (min length 1)


def test_unparseable_series_index_is_none():
    for raw in ("1-3", "one", "nan", "inf"):
        (p,) = build_projection([row(series_index_sort=raw)])
        assert p["series_index"] is None, raw


def test_untitled_rows_are_skipped_not_pushed():
    assert build_projection([row(title=""), row(title="  ")]) == []


def test_duplicate_source_ids_keep_first():
    projection = build_projection([row(), row(narrator="Someone Else")])
    assert len(projection) == 1  # the index 422s duplicate source_id


def test_projection_is_json_serialisable():
    json.dumps(build_projection([row()]))  # NaN/inf would raise here


# --------------------------------------------------------------------------- #
# The pipeline hook fails soft without configuration
# --------------------------------------------------------------------------- #


def test_push_after_build_skips_quietly_when_env_unset(monkeypatch, capsys):
    monkeypatch.delenv("INDEX_URL", raising=False)
    monkeypatch.delenv("INDEX_PUSH_TOKEN", raising=False)
    assert push_after_build([row()]) is None
    assert "Index push skipped" in capsys.readouterr().out


def test_push_after_build_refuses_empty_projection(monkeypatch, capsys):
    # Configured but nothing to send: zero rows is a failed export — no HTTP.
    monkeypatch.setenv("INDEX_URL", "https://index.invalid")
    monkeypatch.setenv("INDEX_PUSH_TOKEN", "t")
    assert push_after_build([]) is None
    assert "zero rows" in capsys.readouterr().err


def test_push_after_build_sends_bearer_put(monkeypatch):
    monkeypatch.setenv("INDEX_URL", "https://index.example/")
    monkeypatch.setenv("INDEX_PUSH_TOKEN", "sekrit")
    calls = {}

    class FakeResp:
        ok = True

        def json(self):
            return {"ok": True, "source": "audiobook", "rows": 1, "pushed_at": "now", "unfoldable_titles": 0}

    def fake_put(url, json=None, headers=None, timeout=None):
        calls["url"] = url
        calls["json"] = json
        calls["headers"] = headers
        return FakeResp()

    import requests

    monkeypatch.setattr(requests, "put", fake_put)
    result = push_after_build([row()])
    assert result["rows"] == 1
    assert calls["url"] == "https://index.example/api/push/audiobook"
    assert calls["headers"]["Authorization"] == "Bearer sekrit"
    assert isinstance(calls["json"], list) and calls["json"][0]["format"] == "audiobook"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
