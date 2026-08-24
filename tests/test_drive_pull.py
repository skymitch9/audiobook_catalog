"""The three lessons of the 2026-08-24 duplicate incident, as tests.

If any of these regresses, the pull will re-create duplicates or hide real books.
"""

from app.core.drive_pull import plan_pull, is_copy_name, match_key


def test_copy_of_is_never_pulled():
    # Rule 2: a 'Copy of …' is a Drive-side duplicate — skip it even though the
    # original is present (this is the exact file the old --fix downloaded).
    p = plan_pull(["Copy of Dungeon Crawler Carl.m4b"], ["Dungeon Crawler Carl.m4b"])
    assert p.to_pull == []
    assert p.skipped_copies == ["Copy of Dungeon Crawler Carl.m4b"]


def test_present_epub_is_not_missing():
    # Rule 1: an ebook already on disk must read as present, not missing — the
    # audio-only scan is what flagged 170 present epubs as missing.
    p = plan_pull(["Whisper Me This.epub"], ["Whisper Me This.epub"])
    assert p.to_pull == []
    assert p.skipped_present == ["Whisper Me This.epub"]


def test_series_volume_is_pulled_not_treated_as_copy():
    # Rule 3: 'Summoner 2' is book 2, NOT a copy of 'Summoner'. Local has only
    # book 1, so volume 2 is genuinely new and must be pulled.
    p = plan_pull(["Summoner 2.m4b"], ["Summoner.m4b"])
    assert p.to_pull == ["Summoner 2.m4b"]
    assert p.skipped_copies == []


def test_series_volume_already_present_is_skipped():
    p = plan_pull(["Summoner 2.m4b"], ["Summoner.m4b", "Summoner 2.m4b"])
    assert p.to_pull == []
    assert p.skipped_present == ["Summoner 2.m4b"]


def test_ebook_pulled_when_only_the_audiobook_is_present():
    # Format-class: the household wants BOTH the ebook and the audiobook, so the
    # ebook is not 'present' just because the m4b is on disk.
    p = plan_pull(["Wandering Inn.epub"], ["Wandering Inn.m4b"])
    assert p.to_pull == ["Wandering Inn.epub"]


def test_parenthesised_copy_marker_is_skipped():
    p = plan_pull(["Book X (1).m4b"], ["Book X.m4b"])
    assert p.skipped_copies == ["Book X (1).m4b"]


def test_non_book_files_are_ignored():
    p = plan_pull(["cover.jpg", "desktop.ini"], [])
    assert set(p.ignored) == {"cover.jpg", "desktop.ini"}
    assert p.to_pull == []


def test_key_keeps_volume_numbers_but_strips_copy_markers():
    assert match_key("Summoner 2.m4b") != match_key("Summoner.m4b")
    assert match_key("Copy of Summoner.m4b") == match_key("Summoner.m4b")
    assert is_copy_name("Copy of X.m4b") and not is_copy_name("Summoner 2.m4b")
