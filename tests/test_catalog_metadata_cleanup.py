"""The 2026-11-24 catalogue metadata clean-up — one test per rule.

Defects it answers (measured on the live catalogue that day, 1,180 rows):
  1. 27 Audible downloads titled "Title: Series, Book N (Unabridged)" with no series;
  2. 139 rows with the placeholder genre "Audiobook";
  3. placeholder years 2000 / 2200;
  4. author/narrator case damage ("M.a. Wardell", "Bob Mcgough");
  5. a second Disney shelf ("Disney Book Group" beside "Disney Books").

Nothing here reads the real library. Files are generated into tmp_path.
"""

import json
from pathlib import Path

import pytest
from mutagen.mp4 import MP4

from app import metadata as md
from app.additions_log import identity_fold, update_additions_log
from app.core import audible_backfill as ab
from app.core.people import normalize_people_field
from app.parsers.title import parse_series_and_index_from_title

REPO = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# 4. people casing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw, want", [
    ("M.A. Wardell", "M.A. Wardell"),
    ("m.a. wardell", "M.A. Wardell"),
    ("D.k. Holmberg", "D.K. Holmberg"),
    ("Bob McGough", "Bob McGough"),
    ("Michael R. DeCamp", "Michael R. DeCamp"),
    ("Marie O'Regan", "Marie O'Regan"),
    ("Mashton XX", "Mashton XX"),
    ("FUNA", "FUNA"),            # whole-name all-caps <= 5 stays (unchanged rule)
    ("JANE DOE", "Jane Doe"),     # long all-caps words still capitalise
    ("john smith", "John Smith"),
    ("bob mcgough", "Bob Mcgough"),  # an all-lowercase tag carries no case to keep
])
def test_people_casing(raw, want):
    assert normalize_people_field(raw) == want


def test_metadata_uses_the_one_people_rule():
    from app.core import people
    assert md.normalize_people_field is people.normalize_people_field


def test_casefold_dedupe_still_applies():
    assert normalize_people_field("M.A. Wardell, m.a. wardell") == "M.A. Wardell"


# --------------------------------------------------------------------------- #
# 1. title shape -> series
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("title, series, idx", [
    ("SOS Hotel: For a Supernaturally Safe Stay!: SOS Hotel, Book 1", "SOS Hotel", "1"),
    ("The Wedding Date Disaster: The Double Dilemma Series, Book 1", "The Double Dilemma", "1"),
    ("Battleforged: Survivor: Battleforged, Book 1", "Battleforged", "1"),
    ("Mother of Learning Arc 1: Mother of Learning, Book 1", "Mother of Learning", "1"),
    ("The Hedge Wizard: A LitRPG / Progression Fantasy Adventure (The Hedge Wizard, Book 1)", "The Hedge Wizard", "1"),
    # must NOT take the "Gender Fall - Gender Game" tail as the series
    ("The Gender Game 5: The Gender Fall - The Gender Game, Book 5", "The Gender Game", "5"),
])
def test_title_series_shapes(title, series, idx):
    assert parse_series_and_index_from_title(title) == (series, idx)


def test_unabridged_still_blocks_the_raw_parser():
    """The exclusion stays; the suffix is removed BEFORE parsing instead."""
    assert parse_series_and_index_from_title("Rejected: Shadow Beast Shifters, Book 1 (Unabridged)") == (None, None)


def test_strip_edition_suffix():
    assert md.strip_edition_suffix("Wait with Me (Unabridged)") == "Wait with Me"
    assert md.strip_edition_suffix("The Wedding Date Disaster : X, Book 1 (Unabridged)") == "The Wedding Date Disaster: X, Book 1"
    assert md.strip_edition_suffix("Some Book (Abridged)") == "Some Book (Abridged)"


def _tags(**kw):
    keys = {"nam": md.K_TITLE, "art": md.K_ARTIST, "wrt": md.K_WRITER, "day": md.K_DAY,
            "gen": md.K_GENRE, "cdek": md.K_ASIN, "srnm": md.K_SERIES_VENDOR}
    return {keys[k]: [v] for k, v in kw.items()}


def test_derive_parses_series_but_keeps_the_title_key():
    """Title stays as tagged (it is a persisted key — see STRIP_UNABRIDGED_FROM_TITLE)."""
    assert md.STRIP_UNABRIDGED_FROM_TITLE is False
    d = md.derive_correctable_fields(_tags(nam="Rejected: Shadow Beast Shifters, Book 1 (Unabridged)", art="Jaymin Eve"))
    assert d["title"] == "Rejected: Shadow Beast Shifters, Book 1 (Unabridged)"
    assert (d["series"], d["series_index"]) == ("Shadow Beast Shifters", "1")


def test_real_series_tag_still_wins():
    d = md.derive_correctable_fields(_tags(nam="A: Wrong, Book 9 (Unabridged)", art="X", srnm="Right"))
    assert d["series"] == "Right"


# --------------------------------------------------------------------------- #
# 2/3. Audible backfill
# --------------------------------------------------------------------------- #
def test_placeholder_years():
    assert ab.is_placeholder_year("2000")
    assert ab.is_placeholder_year("2200")
    assert not ab.is_placeholder_year("2000-03-14")  # a full date is somebody's real date
    assert not ab.is_placeholder_year("2024")
    assert not ab.is_placeholder_year("")


def test_fill_only_replaces_placeholders():
    e = {"genre": "Romance:Contemporary", "release_date": "2025-10-10"}
    assert ab.fill({"genre": "Audiobook", "year": "2000"}, e) == {"genre": "Romance:Contemporary", "year": "2025-10-10"}
    assert ab.fill({"genre": "Fantasy:Epic", "year": "2019"}, e) == {"genre": "Fantasy:Epic", "year": "2019"}


def test_fill_never_takes_a_placeholder_release_date():
    assert ab.fill({"genre": "Audiobook", "year": "2000"}, {"genre": "", "release_date": "2200-01-01"}) == {
        "genre": "Audiobook", "year": "2000"}


def test_future_year_without_data_is_blanked():
    assert ab.fill({"year": "2200"}, None)["year"] == ""
    assert ab.fill({"year": "2000"}, None)["year"] == "2000"  # 2000 is only REPLACED, never blanked


@pytest.fixture
def backfill_file(tmp_path):
    p = tmp_path / "audible_backfill.json"
    p.write_text(json.dumps({
        "genre_paths": ["Romance:Contemporary"],
        "entries": [
            {"asin": "B0TAGS0001", "match": {"title_tag": "Book One: Saga, Book 1 (Unabridged)", "artist_tag": "Ann Author"},
             "genre": "Romance:Contemporary", "release_date": "2025-10-10"},
            {"asin": "B0CDEK0002", "match": {"asin": "B0CDEK0002"}, "genre": "Romance:Contemporary", "release_date": ""},
        ],
    }), encoding="utf-8")
    ab.reload(p)
    yield p
    ab.reload()


def test_backfill_matches_raw_tags_and_feeds_the_asin(backfill_file):
    tags = _tags(nam="Book One: Saga, Book 1 (Unabridged)", art="Ann Author", gen="Audiobook", day="2000")
    d = md.derive_correctable_fields(tags)
    assert (d["genre"], d["year"], d["series"], d["series_index"]) == ("Romance:Contemporary", "2025-10-10", "Saga", "1")
    assert md.resolve_asin(tags) == "B0TAGS0001"


def test_backfill_by_cdek_and_own_cdek_wins(backfill_file):
    tags = _tags(nam="Anything", art="Someone", gen="Audiobook", cdek="B0CDEK0002")
    assert md.derive_correctable_fields(tags)["genre"] == "Romance:Contemporary"
    assert md.resolve_asin(tags) == "B0CDEK0002"


def test_backfill_needs_an_exact_raw_match(backfill_file):
    tags = _tags(nam="Book One: Saga, Book 1", art="Ann Author", gen="Audiobook")
    assert md.derive_correctable_fields(tags)["genre"] == "Audiobook"
    assert md.resolve_asin(tags) is None


def test_backfill_missing_or_malformed_is_a_noop(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    try:
        ab.reload(bad)
        assert ab.lookup("x", "y", None) is None
        ab.reload(tmp_path / "absent.json")
        assert ab.lookup("x", "y", None) is None
    finally:
        ab.reload()


def test_committed_backfill_is_product_data_only():
    data = json.loads((REPO / "scripts" / "audible_backfill.json").read_text(encoding="utf-8"))
    assert data["entries"], "the generated file should not be empty"
    banned = {"purchase_date", "date_added", "profile", "is_finished", "percent_complete"}
    for e in data["entries"]:
        assert not (banned & set(e)) and not (banned & set(e.get("evidence", {}))), e["asin"]
        assert e["match"].get("asin") or e["match"].get("title_tag"), e["asin"]
        assert not ab.is_placeholder_year(e["release_date"]), e["asin"]


PATHS = ["Literature & Fiction:Classics", "Science Fiction & Fantasy:Fantasy:Epic",
         "Mystery, Thriller & Suspense:Thriller & Suspense:Technothrillers", "Teen & Young Adult:Romance"]


def test_split_ladders_rejoins_comma_names():
    flat = "Mystery, Thriller & Suspense, Thriller & Suspense, Technothrillers, Science Fiction & Fantasy, Fantasy, Epic"
    assert ab.split_ladders(flat, PATHS) == [
        "Mystery, Thriller & Suspense:Thriller & Suspense:Technothrillers",
        "Science Fiction & Fantasy:Fantasy:Epic"]


def test_first_known_ladder_refuses_unknown():
    assert ab.first_known_ladder("Literature & Fiction, Classics, Science Fiction & Fantasy, Fantasy", PATHS) == \
        "Literature & Fiction:Classics"
    assert ab.first_known_ladder("Sports & Outdoors, Running & Jogging", PATHS) == ""


def test_builder_matching_needs_runtime_and_author():
    from app.tools import build_audible_backfill as b
    exports = {"A1": {"asin": "A1", "title": "Ship of Magic", "subtitle": "The Liveship Traders, Book 1",
                      "authors": "Robin Hobb", "runtime_length_min": 2120}}
    by_title = {b._norm_title("Ship of Magic: The Liveship Traders, Book 1"): ["A1"]}
    whole = {"nam": "Ship of Magic: The Liveship Traders, Book 1 (Unabridged)", "art": "Robin Hobb", "cdek": "", "sec": 2120 * 60}
    part = dict(whole, sec=453 * 60)          # a "Part 1" file is not the product
    other = dict(whole, art="Someone Else")
    assert b.match_file(whole, exports, by_title) == ("A1", "title+author+runtime")
    assert b.match_file(part, exports, by_title)[0] is None
    assert b.match_file(other, exports, by_title)[0] is None


# --------------------------------------------------------------------------- #
# the converter — future downloads come out clean
# --------------------------------------------------------------------------- #
META = {"title": "Loner Life in Another World, Vol. 1", "subtitle": "Light Novel",
        "genres": "Literature & Fiction, Classics", "release_date": "2025-10-10",
        "series_title": "Loner Life in Another World", "series_sequence": "1"}


def test_audible_tags_pure():
    from app.tools.audible_download import audible_tags
    t = audible_tags("B0FVMMZB5W", META, "Loner Life in Another World, Vol. 1: Light Novel (Unabridged)", PATHS)
    assert t == {
        "CDEK": "B0FVMMZB5W", "SRNM": "Loner Life in Another World", "SRSQ": "1",
        "\xa9gen": "Literature & Fiction:Classics", "\xa9day": "2025-10-10",
        "\xa9nam": "Loner Life in Another World, Vol. 1: Light Novel",
        "\xa9alb": "Loner Life in Another World, Vol. 1: Light Novel",
    }


def test_audible_tags_leave_placeholders_out():
    from app.tools.audible_download import audible_tags
    t = audible_tags("B0X", {"genres": "Sports & Outdoors", "release_date": "2200-01-01"}, "Plain Title", PATHS)
    assert t == {"CDEK": "B0X"}


def test_write_audible_tags_on_a_new_file(tmp_path, backfill_file):
    from scripts.generate_test_book import generate_test_book

    from app.tools.audible_download import write_audible_tags
    out = tmp_path / "new.m4b"
    generate_test_book(title="Fresh: Saga, Book 2 (Unabridged)", author="Ann Author", genre="Audiobook",
                       year="2000", series=None, series_index=None, output=out)
    written = write_audible_tags(out, "B0NEW00001", {
        "genres": "Romance, Contemporary", "release_date": "2026-01-02",
        "series_title": "Saga", "series_sequence": "2"})
    tags = MP4(str(out)).tags
    assert tags["CDEK"] == ["B0NEW00001"] and tags["SRNM"] == ["Saga"] and tags["SRSQ"] == ["2"]
    assert tags["\xa9gen"] == ["Romance:Contemporary"] and tags["\xa9day"] == ["2026-01-02"]
    assert tags["\xa9nam"] == ["Fresh: Saga, Book 2"]
    assert set(written) == {"CDEK", "SRNM", "SRSQ", "\xa9gen", "\xa9day", "\xa9nam", "\xa9alb"}
    # and the catalogue reads it with no backfill entry at all
    d = md.derive_correctable_fields(tags)
    assert (d["title"], d["series"], d["series_index"], d["genre"]) == ("Fresh: Saga, Book 2", "Saga", "2", "Romance:Contemporary")


def test_export_rows_carry_the_product_fields(monkeypatch, tmp_path):
    """audible_cli_books() must hand the converter genres/series/subtitle."""
    import subprocess

    from app.tools import audit_new_purchases as anp

    def fake_run(cmd, **kw):
        out = Path(cmd[cmd.index("--output") + 1])
        out.write_text("asin\ttitle\tsubtitle\tauthors\tnarrators\tseries_title\tseries_sequence\tgenres\t"
                       "release_date\tpurchase_date\n"
                       "B1\tT\tS\tA\tN\tSer\t3\tRomance, Contemporary\t2025-01-01\t2025-02-02\n", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(anp, "AUDIBLE_PROFILES", ("p1",))
    monkeypatch.setattr(anp.subprocess, "run", fake_run)
    [row] = anp.audible_cli_books()
    assert (row["subtitle"], row["genres"], row["series_title"], row["series_sequence"]) == (
        "S", "Romance, Contemporary", "Ser", "3")


# --------------------------------------------------------------------------- #
# identity: a cosmetic key change is not a new book
# --------------------------------------------------------------------------- #
def test_identity_fold():
    assert identity_fold("Meetings (Unabridged)|M.a. Wardell") == identity_fold("Meetings|M.A. Wardell")
    assert identity_fold("Book|Ann") != identity_fold("Book 2|Ann")
    assert identity_fold("Book|Ann") != identity_fold("Book|Bob")


def test_additions_log_carries_the_date_over(tmp_path):
    (tmp_path / "additions_log.json").write_text(json.dumps({"entries": [
        {"key": "Meetings|M.a. Wardell", "title": "Meetings", "author": "M.a. Wardell", "added": "2026-06-02", "source": "pipeline"}
    ]}), encoding="utf-8")
    ent = update_additions_log([{"title": "Meetings", "author": "M.A. Wardell"},
                                {"title": "Brand New", "author": "Ann"}], tmp_path, today="2026-11-24")
    assert ent["Meetings|M.A. Wardell"]["added"] == "2026-06-02"
    assert ent["Meetings|M.A. Wardell"]["carried_from"] == "Meetings|M.a. Wardell"
    assert ent["Meetings|M.a. Wardell"]["added"] == "2026-06-02"  # old entry untouched
    assert ent["Brand New|Ann"]["added"] == "2026-11-24"


def test_additions_log_refuses_an_ambiguous_fold(tmp_path):
    (tmp_path / "additions_log.json").write_text(json.dumps({"entries": [
        {"key": "X|a b", "added": "2020-01-01"}, {"key": "X|A B", "added": "2021-01-01"}]}), encoding="utf-8")
    ent = update_additions_log([{"title": "X", "author": "A b"}], tmp_path, today="2026-11-24")
    assert ent["X|A b"]["added"] == "2026-11-24"


def test_detect_new_books_ignores_cosmetic_key_changes(tmp_path, monkeypatch):
    import csv

    from app.tools import detect_new_books as dnb
    monkeypatch.chdir(tmp_path)
    (tmp_path / "site").mkdir()
    with open(tmp_path / "site" / "catalog.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["title", "author"])
        w.writeheader()
        w.writerow({"title": "Meetings", "author": "M.A. Wardell"})
        w.writerow({"title": "Really New", "author": "Ann"})
    (tmp_path / "last_catalog_snapshot.json").write_text(json.dumps({"book_ids": ["Meetings|M.a. Wardell"]}), encoding="utf-8")
    monkeypatch.setattr(dnb, "SNAPSHOT_PATH", Path("last_catalog_snapshot.json"))
    monkeypatch.setattr(dnb, "EBOOK_STATUS_PATH", Path("site/ebooks_status.json"))
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr("sys.argv", ["detect_new_books"])
    dnb.main()
    out = json.loads((tmp_path / "new_books.json").read_text(encoding="utf-8"))
    assert [b["title"] for b in out["books"]] == ["Really New"]


# --------------------------------------------------------------------------- #
# 5. Disney shelf
# --------------------------------------------------------------------------- #
def test_disney_book_group_shelves_under_disney_books():
    from app.author_names import load_shelf_aliases, resolve_shelf_author
    load_shelf_aliases.cache_clear()
    assert resolve_shelf_author("Disney Book Group") == "Disney Books"
    assert resolve_shelf_author("Disney Books") == "Disney Books"
