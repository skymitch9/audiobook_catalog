# tests/test_library_link.py
# "Other versions available", the audiobook side (app/library_link.py):
# the join against library_catalog's mapping, and the fail-soft pipeline hook.
# Mirrors tests/test_index_push.py's shape for the same pull-vs-push module.

import pytest

from app.core.review_join import normalise_title
from app.library_link import (
    clean_audiobook_title,
    clean_title_with_series,
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
    # `foldedTitle` mirrors what apps/worker/src/routes/audiobook-mapping.ts
    # actually sends: the SAME fold (normaliseTitle/normalise_title) computed
    # once, over there, from `audiobookTitle`. Read from `overrides` first so
    # a caller that supplies its own `audiobookTitle` gets a fold that
    # matches it, not the fixture default's.
    title = overrides.get("audiobookTitle", row()["title"])
    r = {
        "workId": 347,
        "audiobookTitle": title,
        "foldedTitle": normalise_title(title),
        "formats": ["Hardcover", "Ebook"],
    }
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


def test_decoration_only_difference_now_matches_via_the_fold():
    # The fix itself (owner, 2026-08-14): the mapping's audiobookTitle is
    # library_catalog's CACHED copy of this catalog's title, and it drifts in
    # decoration (case here) without becoming a different book. Both sides
    # fold through the identical `normaliseTitle`/`normalise_title`
    # implementation — the estate's one trusted identity fold, not a fuzzy
    # match — so a case-only difference is exactly the shape this join now
    # closes. Before the fix this stayed unmatched; that was the 51-of-90 gap.
    rows = [row(title=row()["title"].upper())]
    stats = stamp_rows(rows, [mapping_row()])
    assert rows[0]["library_work_id"] == "347"
    assert rows[0]["library_formats"] == "Hardcover|Ebook"
    assert stats.stamped == 1


def test_a_genuinely_different_title_still_does_not_match():
    # The fold narrows decoration differences; it must never widen into an
    # actual guess. A different word is a different book, fold or no fold —
    # the module's whole point is still "shown, never guessed at".
    rows = [row(title="Harry Potter and the Chamber of Secrets")]
    stats = stamp_rows(rows, [mapping_row()])
    assert rows[0]["library_work_id"] == ""
    assert stats.stamped == 0


def test_a_folded_collision_falls_back_to_each_row_s_own_exact_title():
    # Two mapping rows whose audiobookTitle folds identically. The library
    # side is supposed to withhold `foldedTitle` from both in this case (see
    # audiobook-mapping.ts) — simulated here directly, since this module only
    # ever sees the JSON it was handed. Neither is guessed at via the fold;
    # each is still reachable through its own untouched exact spelling.
    a = mapping_row(workId=1, audiobookTitle="Cafe Insomniac", foldedTitle=None, formats=["Hardcover"])
    b = mapping_row(workId=2, audiobookTitle="CAFE INSOMNIAC", foldedTitle=None, formats=["Ebook"])
    rows = [row(title="Cafe Insomniac"), row(title="CAFE INSOMNIAC")]
    stats = stamp_rows(rows, [a, b])
    assert rows[0]["library_work_id"] == "1"
    assert rows[1]["library_work_id"] == "2"
    assert stats.stamped == 2


def test_python_side_also_guards_a_folded_collision_and_logs_it(capsys):
    # Defense in depth: even if the library side ever sent a duplicate
    # `foldedTitle` for two different rows (a bug over there, not the
    # documented contract), this side must still never guess — and it must
    # say so on stderr rather than fail silently.
    a = mapping_row(workId=1, audiobookTitle="Cafe Insomniac", formats=["Hardcover"])
    b = mapping_row(workId=2, audiobookTitle="CAFE INSOMNIAC", formats=["Ebook"])
    assert a["foldedTitle"] == b["foldedTitle"]  # both fold to "cafe insomniac"

    rows = [row(title="Cafe Insomniac"), row(title="CAFE INSOMNIAC")]
    stats = stamp_rows(rows, [a, b])
    assert rows[0]["library_work_id"] == "1"  # reached via its own exact spelling
    assert rows[1]["library_work_id"] == "2"
    assert "Folded title collision" in capsys.readouterr().err


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


# --------------------------------------------------------------------------- #
# clean_audiobook_title / clean_title_with_series — the titles.ts port
#
# Real rows, measured against the live `catalog.csv` 2026-08-14. These are the
# exact shapes that made a byte-exact join miss 53 of 90 pairs: Audible's own
# packaging, which `normalise_title` alone does not remove.
# --------------------------------------------------------------------------- #


def test_strips_the_ordinary_dash_series_book_n_suffix():
    assert clean_title_with_series("Dungeon Born - Divine Dungeon Series, Book 1", "The Divine Dungeon") == (
        "Dungeon Born"
    )


def test_strips_series_suffix_even_when_the_series_name_itself_differs_by_the_article():
    # catalog.csv's `series` column says "The Divine Dungeon"; the title
    # suffix says "Divine Dungeon Series" — cleanAudiobookTitle's generic
    # ", Book N" strip (no series-name comparison at all) is what catches
    # this one, not the series-aware pass. Recorded so a future change to
    # either strip does not silently stop covering it.
    assert clean_audiobook_title("Dungeon Born - Divine Dungeon Series, Book 1") == "Dungeon Born"


def test_strips_the_series_suffix_using_the_known_series_name_exactly():
    assert (
        clean_title_with_series(
            "Oathbound Healer - Beneath the Dragoneye Moons, Book 1", "Beneath the Dragoneye Moons"
        )
        == "Oathbound Healer"
    )
    assert (
        clean_title_with_series(
            "Under Ashen Skies - Beneath the Dragoneye Moons, Book 10", "Beneath the Dragoneye Moons"
        )
        == "Under Ashen Skies"
    )


def test_never_returns_empty_when_the_title_is_only_the_series_name():
    # A standalone book whose title IS its series name would otherwise
    # collapse to nothing — the same guard titles.ts documents.
    assert clean_title_with_series("Space Knight", "Space Knight") == "Space Knight"


def test_a_bare_trailing_number_is_never_stripped():
    # "Summoner 6" IS the title — Eric Vall's books are named that way.
    assert clean_audiobook_title("Summoner 6") == "Summoner 6"


# --------------------------------------------------------------------------- #
# stamp_rows — end to end with the real Audible-decorated shape
# --------------------------------------------------------------------------- #


def test_decorated_catalog_title_matches_the_library_s_already_clean_cache():
    # This is the fix's whole point: `catalog.csv`'s own title still carries
    # Audible's packaging; the library's cached `audiobookTitle` is already
    # clean (that side stripped it before caching). Byte-exact comparison
    # never met these two strings; the fold now does.
    rows = [row(title="Dungeon Born - Divine Dungeon Series, Book 1", series="The Divine Dungeon")]
    mapping = [mapping_row(workId=7, audiobookTitle="Dungeon Born", formats=["Ebook"])]
    stats = stamp_rows(rows, mapping)
    assert rows[0]["library_work_id"] == "7"
    assert stats.stamped == 1


def test_catalog_side_fold_collision_refuses_both_rather_than_guess():
    # The Space Knight shape, measured live 2026-08-14: TWO catalog.csv rows
    # (this library's own two Space Knight volumes) both clean down to the
    # bare series name "Space Knight" once their volume decoration is
    # stripped — the volume number that told them apart lived in a separate
    # CSV column (`series_index_sort`), never in the title text this join
    # reads. Only ONE mapping entry exists for that folded name. Matching
    # either catalog row via the fold would be a coin flip over which one
    # "really" owns it, so BOTH are refused and left for the exact-title
    # fallback — which also cannot tell them apart, because the library's
    # cached title is identically bare "Space Knight" for both holdings too.
    rows = [
        row(title="Space Knight, Book 1", series="Space Knight", series_index_sort="1.0"),
        row(title="Space Knight, Book 2", series="Space Knight", series_index_sort="2.0"),
    ]
    mapping = [mapping_row(workId=250, audiobookTitle="Space Knight", formats=["Ebook"])]
    stats = stamp_rows(rows, mapping)
    assert rows[0]["library_work_id"] == ""
    assert rows[1]["library_work_id"] == ""
    assert stats.stamped == 0
    assert stats.unmatched_titles == ["Space Knight"]


def test_two_works_caching_the_identical_raw_title_refuses_rather_than_last_one_wins():
    # The bug this closes, found live 2026-08-14 while measuring Task 1's
    # coverage AFTER Task 2's volume-disambiguation backfill ran: works #249
    # and #250 both cache the identical bare "Space Knight" — and catalog.csv
    # separately holds a PLAIN "Space Knight" row (volume 1, needing no
    # cleaning at all) that exact-matches that raw string. Before this fix,
    # `by_title`'s "last one wins" silently handed that row to whichever
    # work happened to be inserted last — a WRONG link (volume 1's audio
    # pointed at volume 2's print work), not a missing one.
    rows = [row(title="Space Knight", series="Space Knight", series_index_sort="1.0")]
    mapping = [
        mapping_row(workId=249, audiobookTitle="Space Knight", formats=["Ebook"]),
        mapping_row(workId=250, audiobookTitle="Space Knight", formats=["Paperback"]),
    ]
    stats = stamp_rows(rows, mapping)
    assert rows[0]["library_work_id"] == ""
    assert stats.stamped == 0
    assert stats.unmatched_titles == ["Space Knight"]


def test_exact_title_collision_is_logged(capsys):
    rows = [row(title="Space Knight", series="Space Knight")]
    mapping = [
        mapping_row(workId=249, audiobookTitle="Space Knight", formats=["Ebook"]),
        mapping_row(workId=250, audiobookTitle="Space Knight", formats=["Paperback"]),
    ]
    stamp_rows(rows, mapping)
    assert "Exact title collision" in capsys.readouterr().err


def test_a_stale_link_from_an_earlier_less_strict_run_is_cleared_not_left():
    # Found live 2026-08-14 while verifying the fix above against the real
    # `site/catalog.csv`: the plain "Space Knight" row already carried
    # `library_work_id=250` on disk, written by a PRIOR pipeline run under
    # the old byte-exact join, before work #249 existed to make it
    # ambiguous. Once this run's stricter logic refuses to guess, that old
    # value must not just sit there looking current — it has to be cleared.
    rows = [row(title="Space Knight", series="Space Knight", library_work_id="250", library_formats="Paperback")]
    mapping = [
        mapping_row(workId=249, audiobookTitle="Space Knight", formats=["Ebook"]),
        mapping_row(workId=250, audiobookTitle="Space Knight", formats=["Paperback"]),
    ]
    stats = stamp_rows(rows, mapping)
    assert rows[0]["library_work_id"] == ""
    assert rows[0]["library_formats"] == ""
    assert stats.stamped == 0
    assert stats.cleared == 1


def test_an_already_blank_row_with_no_match_does_not_count_as_cleared():
    rows = [row(title="A Book Nobody Owns In Print")]
    stats = stamp_rows(rows, [mapping_row()])
    assert stats.cleared == 0


def test_catalog_side_collision_is_logged(capsys):
    rows = [
        row(title="Space Knight, Book 1", series="Space Knight"),
        row(title="Space Knight, Book 2", series="Space Knight"),
    ]
    mapping = [mapping_row(workId=250, audiobookTitle="Space Knight", formats=["Ebook"])]
    stamp_rows(rows, mapping)
    assert "folded title collision(s) within catalog.csv" in capsys.readouterr().err


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
