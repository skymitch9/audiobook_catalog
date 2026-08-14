# tests/test_library_link.py
# "Other versions available", the audiobook side (app/library_link.py):
# the join against library_catalog's mapping, and the fail-soft pipeline hook.
# Mirrors tests/test_index_push.py's shape for the same pull-vs-push module.

import pytest

from app.library_link import (
    fetch_mapping,
    stamp_after_build,
    stamp_after_build_safe,
    stamp_rows,
)


def row(**overrides):
    """A catalog.csv row as written by app/writers.py (14 columns, post-stamp)."""
    r = {
        "title": "Harry Potter and the Sorcerer's Stone (Full-Cast Edition)",
        "series": "Harry Potter (Full-Cast Editions)",
        "series_index_display": "1",
        "series_index_sort": "1.0",
        "author": "J.k. Rowling",
        "narrator": "Full Cast",
        "year": "2016",
        "genre": "Fiction:Fantasy",
        "duration_hhmm": "8:33",
        "cover_href": "covers/J.k. Rowling/Harry Potter 1.jpg",
        "companion_files": "",
        "desc": "",
        "library_work_id": "",
        "library_formats": "",
    }
    r.update(overrides)
    return r


def mapping_row(**overrides):
    r = {"workId": 347, "audiobookTitle": row()["title"], "formats": ["Hardcover", "Ebook"]}
    r.update(overrides)
    return r


# --------------------------------------------------------------------------- #
# stamp_rows — the join itself
# --------------------------------------------------------------------------- #


def test_exact_title_match_stamps_work_id_and_formats():
    rows = [row()]
    stats = stamp_rows(rows, [mapping_row()])
    assert rows[0]["library_work_id"] == "347"
    assert rows[0]["library_formats"] == "Hardcover|Ebook"
    assert stats.stamped == 1
    assert stats.unmatched_titles == []


def test_no_match_leaves_row_untouched():
    rows = [row(title="A Book Nobody Owns In Print")]
    stats = stamp_rows(rows, [mapping_row()])
    assert rows[0]["library_work_id"] == ""
    assert rows[0]["library_formats"] == ""
    assert stats.stamped == 0
    assert stats.unmatched_titles == [mapping_row()["audiobookTitle"]]


def test_match_is_exact_not_fuzzy():
    # A near-miss (different case, one changed word) must NOT match — the
    # module's whole point is "shown, never guessed at". (Surrounding
    # whitespace IS trimmed on both sides — see the next test.)
    rows = [row(title=row()["title"].upper())]
    stats = stamp_rows(rows, [mapping_row()])
    assert rows[0]["library_work_id"] == ""
    assert stats.stamped == 0


def test_surrounding_whitespace_is_trimmed_before_the_exact_match():
    rows = [row(title=row()["title"] + " ")]
    stats = stamp_rows(rows, [mapping_row()])
    assert rows[0]["library_work_id"] == "347"
    assert stats.stamped == 1


def test_multiple_rows_multiple_mappings():
    rows = [row(title="Book A"), row(title="Book B"), row(title="Book C")]
    mapping = [
        mapping_row(workId=1, audiobookTitle="Book A", formats=["Hardcover"]),
        mapping_row(workId=2, audiobookTitle="Book B", formats=["Ebook"]),
    ]
    stats = stamp_rows(rows, mapping)
    assert rows[0]["library_work_id"] == "1"
    assert rows[1]["library_work_id"] == "2"
    assert rows[2]["library_work_id"] == ""
    assert stats.stamped == 2
    assert stats.mapping_rows == 2
    assert stats.unmatched_titles == []


def test_empty_mapping_title_is_skipped_not_a_wildcard():
    rows = [row(title="")]
    stats = stamp_rows(rows, [mapping_row(audiobookTitle="")])
    assert rows[0]["library_work_id"] == ""
    assert stats.stamped == 0


def test_formats_list_can_be_empty():
    # A library work with a holding but no edition rows (e.g. a shelf photo
    # that never got a printing recorded) — honest, not an error.
    rows = [row()]
    stats = stamp_rows(rows, [mapping_row(formats=[])])
    assert rows[0]["library_work_id"] == "347"
    assert rows[0]["library_formats"] == ""
    assert stats.stamped == 1


# --------------------------------------------------------------------------- #
# The pipeline hook fails soft without configuration
# --------------------------------------------------------------------------- #


def test_stamp_after_build_skips_quietly_when_env_unset(monkeypatch, capsys):
    monkeypatch.delenv("LIBRARY_MAPPING_URL", raising=False)
    monkeypatch.delenv("LIBRARY_MAPPING_TOKEN", raising=False)
    assert stamp_after_build([row()]) is None
    assert "Library link stamping skipped" in capsys.readouterr().out


def test_stamp_after_build_safe_never_raises_on_fetch_failure(monkeypatch, capsys):
    monkeypatch.setenv("LIBRARY_MAPPING_URL", "https://library.invalid")
    monkeypatch.setenv("LIBRARY_MAPPING_TOKEN", "t")

    import requests

    def fake_get(url, headers=None, timeout=None):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(requests, "get", fake_get)
    assert stamp_after_build_safe([row()]) is None
    assert "Library link stamping failed" in capsys.readouterr().err


def test_fetch_mapping_sends_bearer_get_and_parses_rows(monkeypatch):
    monkeypatch.setenv("LIBRARY_MAPPING_URL", "https://library.example/")
    calls = {}

    class FakeResp:
        ok = True

        def json(self):
            return {"rows": [mapping_row()], "generatedAt": "now"}

    def fake_get(url, headers=None, timeout=None):
        calls["url"] = url
        calls["headers"] = headers
        return FakeResp()

    import requests

    monkeypatch.setattr(requests, "get", fake_get)
    rows = fetch_mapping("https://library.example/", "sekrit")
    assert calls["url"] == "https://library.example/api/machine/audiobook-mapping"
    assert calls["headers"]["Authorization"] == "Bearer sekrit"
    assert rows == [mapping_row()]


def test_fetch_mapping_raises_on_non_ok():
    class FakeResp:
        ok = False
        status_code = 401
        text = "unauthenticated"

    def fake_get(url, headers=None, timeout=None):
        return FakeResp()

    import requests as _requests

    import pytest as _pytest

    orig = _requests.get
    _requests.get = fake_get
    try:
        with _pytest.raises(RuntimeError, match="401"):
            fetch_mapping("https://library.example/", "bad")
    finally:
        _requests.get = orig


def test_stamp_after_build_end_to_end(monkeypatch, capsys):
    monkeypatch.setenv("LIBRARY_MAPPING_URL", "https://library.example/")
    monkeypatch.setenv("LIBRARY_MAPPING_TOKEN", "sekrit")

    class FakeResp:
        ok = True

        def json(self):
            return {"rows": [mapping_row()]}

    import requests

    monkeypatch.setattr(requests, "get", lambda url, headers=None, timeout=None: FakeResp())

    rows = [row()]
    stats = stamp_after_build(rows)
    assert stats.stamped == 1
    assert rows[0]["library_work_id"] == "347"
    assert "Library link stamping OK" in capsys.readouterr().out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
