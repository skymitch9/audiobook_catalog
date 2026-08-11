"""Tests for the catalog corrections layer (app/core/catalog_overrides.py).

Two jobs:
  * pin the mechanism - matching, precedence, blank-not-guessed, canonicalisation,
    and the fail-safe behaviour when the JSON is missing or broken;
  * pin the seeded data, so a bad edit to scripts/catalog_overrides.json fails
    here rather than silently reordering a series in the published catalog.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core import catalog_overrides as co

REPO_ROOT = Path(__file__).resolve().parent.parent
OVERRIDES_JSON = REPO_ROOT / "scripts" / "catalog_overrides.json"

CC = "The Completionist Chronicles"
SHT = "The Selfless Hero Trilogy"


@pytest.fixture(autouse=True)
def _restore_default_overrides():
    """A test that loads a temp file must not leak into the next one."""
    yield
    co.reload_overrides()


def _apply(title, author=None, series="", series_index="", **extra):
    row = {"title": title, "author": author, "series": series, "series_index": series_index}
    row.update(extra)
    return co.apply_overrides(row, path=extra.pop("path", None), asin=extra.pop("asin", None))


# --------------------------------------------------------------------------- #
# The file itself
# --------------------------------------------------------------------------- #


def test_overrides_file_is_valid_json_and_populated():
    assert OVERRIDES_JSON.exists(), "scripts/catalog_overrides.json is missing"
    data = json.loads(OVERRIDES_JSON.read_text(encoding="utf-8"))
    assert isinstance(data.get("overrides"), list) and data["overrides"]
    assert isinstance(data.get("canonical_series"), dict)


def test_every_correction_carries_evidence_for_the_field_it_changes():
    """An override that does not say why it exists is indistinguishable from a typo."""
    data = json.loads(OVERRIDES_JSON.read_text(encoding="utf-8"))
    for entry in data["overrides"]:
        who = entry["match"].get("title") or entry["match"].get("asin")
        ev = entry.get("evidence")
        assert ev, f"{who}: no evidence block"
        assert entry.get("added"), f"{who}: no 'added' date"
        for field in entry["set"]:
            assert ev.get(field), f"{who}: 'set' changes {field!r} but evidence says nothing about it"


def test_corrections_only_touch_allowed_fields():
    data = json.loads(OVERRIDES_JSON.read_text(encoding="utf-8"))
    for entry in data["overrides"]:
        for field in entry["set"]:
            assert field in co.CORRECTABLE_FIELDS, f"{field!r} is not a correctable field"


def test_match_blocks_use_a_stable_key():
    """Never key on filename alone - filenames drift. ASIN, or title+author."""
    data = json.loads(OVERRIDES_JSON.read_text(encoding="utf-8"))
    for entry in data["overrides"]:
        m = entry["match"]
        assert m.get("asin") or (m.get("title") and m.get("author")), f"{m}: needs an asin, or a title AND an author"


# --------------------------------------------------------------------------- #
# The seeded data: Completionist Chronicles
# --------------------------------------------------------------------------- #

# Volumes 1-7 come from the Audible vendor tags (SRNM/SRSQ) and need no entry.
# Volumes 8-14 are hand-tagged files whose trkn was right and whose filenames
# were not; these are the corrections. Confirmed against the Goodreads series
# listing: https://www.goodreads.com/series/229735-the-completionist-chronicles
FROM_VENDOR_TAGS = {
    "Ritualist": "1",
    "Regicide": "2",
    "Rexus: Side Quest": "3",
    "Raze": "4",
    "Ruthless": "5",
    "Inflame": "6",
    "Invent": "7",
}
FROM_CORRECTIONS = {
    "Implode": "8",
    "Tenacity": "9",
    "Thesaurize": "10",
    "Thunderplump": "11",
    "Untapped": "12",
    "Unmapped": "13",
    "Uncapped": "14",
}


@pytest.mark.parametrize("title,index", sorted(FROM_CORRECTIONS.items()))
def test_completionist_corrections(title, index):
    out = _apply(title, author="Dakota Krout")
    assert out["series"] == CC
    assert out["series_index"] == index


def test_completionist_run_is_contiguous_1_to_14():
    tagged = set(FROM_VENDOR_TAGS.values())
    corrected = set(FROM_CORRECTIONS.values())
    assert not (tagged & corrected), "a correction collides with a correctly-tagged volume"
    assert tagged | corrected == {str(n) for n in range(1, 15)}


def test_there_was_never_really_a_volume_11_collision():
    """Both FILENAMES claimed 11. The trkn tags said 11 and 12, and were right."""
    thunder = _apply("Thunderplump", author="Dakota Krout")["series_index"]
    untapped = _apply("Untapped", author="Dakota Krout")["series_index"]
    assert (thunder, untapped) == ("11", "12")


def test_uncapped_series_field_held_the_book_title():
    out = _apply("Uncapped", author="Dakota Krout", series="Uncapped", series_index="14")
    assert out["series"] == CC
    assert out["series_index"] == "14"


def test_implode_had_no_series_anywhere_in_its_filename():
    out = co.apply_overrides(
        {"title": "Implode", "author": "Dakota Krout", "series": None, "series_index": None},
        path=Path("Implode.m4b"),
    )
    assert (out["series"], out["series_index"]) == (CC, "8")


def test_owned_gaps_are_not_manufactured():
    """6.5, 7.5, 11.5 and 13.5 are books the owner does not have. No entries for them."""
    data = json.loads(OVERRIDES_JSON.read_text(encoding="utf-8"))
    indices = {e["set"].get("series_index") for e in data["overrides"]}
    assert not {"6.5", "7.5", "11.5", "13.5"} & indices
    not_owned = data["_not_owned"][CC]
    assert {"6.5", "7.5", "11.5", "13.5"} <= set(not_owned)


def test_invent_short_story_stays_unresolved():
    """It is either 7.5 or a duplicate of #7. Guessing would reorder the series."""
    data = json.loads(OVERRIDES_JSON.read_text(encoding="utf-8"))
    unresolved = {u["item"] for u in data["_unresolved"]}
    assert any("Invent Short Story" in u for u in unresolved)
    titles = {e["match"].get("title") for e in data["overrides"]}
    assert "Invent Short Story" not in titles


# --------------------------------------------------------------------------- #
# The seeded data: Selfless Hero Trilogy
# --------------------------------------------------------------------------- #


def test_selfless_hero_is_in_reading_order_not_alphabetical():
    order = {}
    for stem in ("Otherlife Dreams", "Otherlife Nightmares", "Otherlife Awakenings"):
        out = _apply(f"{stem} - {SHT}", author="William D. Arand")
        assert out["series"] == SHT
        order[stem] = out["series_index"]
    assert order == {"Otherlife Dreams": "1", "Otherlife Nightmares": "2", "Otherlife Awakenings": "3"}
    # Alphabetical would put Awakenings first. Reading order does not.
    assert order["Otherlife Awakenings"] > order["Otherlife Dreams"]


# --------------------------------------------------------------------------- #
# Canonicalisation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "variant",
    [
        "Completionist Chronicles",
        "The Completionist Chronicles",
        "Completionist Chronicles Series",
        "The Completionist Chronicles Series",
        "  the   completionist chronicles  ",
    ],
)
def test_four_spellings_fold_to_one(variant):
    assert co.canonicalize_series(variant) == CC


def test_canonicalisation_applies_to_books_with_no_entry():
    out = _apply("Some Unlisted Book", author="Dakota Krout", series="Completionist Chronicles Series", series_index="99")
    assert out["series"] == CC
    assert out["series_index"] == "99", "an unmatched book keeps its extracted volume"


def test_unknown_series_passes_through():
    assert co.canonicalize_series("The Divine Dungeon") == "The Divine Dungeon"
    assert co.canonicalize_series("") == ""
    assert co.canonicalize_series(None) is None


# --------------------------------------------------------------------------- #
# Mechanism
# --------------------------------------------------------------------------- #


def test_no_match_leaves_the_row_alone():
    out = _apply("A Book Nobody Corrected", author="Nobody At All", series="Some Series", series_index="3")
    assert (out["series"], out["series_index"]) == ("Some Series", "3")


def test_author_mismatch_prevents_a_match():
    """Bare titles like 'Implode' must not hijack another author's book."""
    out = _apply("Implode", author="Some Other Author", series="Their Series", series_index="1")
    assert (out["series"], out["series_index"]) == ("Their Series", "1")


def test_author_matches_inside_a_multi_author_field():
    assert _apply("Tenacity", author="Dennis Vanderkerken, Dakota Krout")["series"] == CC


def test_matching_is_case_and_whitespace_insensitive():
    out = _apply("  tENACITY ", author="dakota krout")
    assert (out["series"], out["series_index"]) == (CC, "9")


def test_input_row_is_not_mutated():
    row = {"title": "Tenacity", "author": "Dakota Krout", "series": "", "series_index": ""}
    out = co.apply_overrides(row)
    assert row["series"] == "" and row["series_index"] == ""
    assert out["series"] == CC


def _write_and_load(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "catalog_overrides.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    co.reload_overrides(p)
    return p


def test_any_derived_field_can_be_corrected(tmp_path):
    """The layer is general, not a series patch."""
    _write_and_load(
        tmp_path,
        {
            "canonical_series": {},
            "overrides": [
                {
                    "match": {"title": "Messy Book", "author": "Real Author"},
                    "set": {
                        "title": "Tidy Book",
                        "author": "Real Author",
                        "narrator": "Right Narrator",
                        "year": "2019",
                        "genre": "Fantasy",
                        "series": "Some Series",
                        "series_index": "2",
                    },
                }
            ],
        },
    )
    out = co.apply_overrides(
        {
            "title": "Messy Book",
            "author": "Real Author",
            "narrator": "",
            "year": "",
            "genre": "",
            "series": "",
            "series_index": "",
        }
    )
    assert out == {
        "title": "Tidy Book",
        "author": "Real Author",
        "narrator": "Right Narrator",
        "year": "2019",
        "genre": "Fantasy",
        "series": "Some Series",
        "series_index": "2",
    }


def test_unknown_field_in_set_is_ignored(tmp_path):
    _write_and_load(
        tmp_path,
        {"canonical_series": {}, "overrides": [{"match": {"title": "X"}, "set": {"seriesss": "oops", "series": "OK"}}]},
    )
    out = co.apply_overrides({"title": "X", "author": None, "series": "", "series_index": ""})
    assert out["series"] == "OK"
    assert "seriesss" not in out


def test_asin_key_matches_regardless_of_title_or_filename(tmp_path):
    """The rename-proof, retag-proof key."""
    _write_and_load(
        tmp_path,
        {"canonical_series": {}, "overrides": [{"match": {"asin": "B07BTHWMFF"}, "set": {"series": "By ASIN"}}]},
    )
    out = co.apply_overrides(
        {"title": "anything at all", "author": "anyone", "series": "", "series_index": ""},
        path=Path("renamed.m4b"),
        asin="B07BTHWMFF",
    )
    assert out["series"] == "By ASIN"


def test_asin_mismatch_blocks_the_entry(tmp_path):
    _write_and_load(
        tmp_path,
        {"canonical_series": {}, "overrides": [{"match": {"asin": "B07BTHWMFF"}, "set": {"series": "By ASIN"}}]},
    )
    out = co.apply_overrides({"title": "x", "author": "y", "series": "Keep", "series_index": ""}, asin="OTHERASIN")
    assert out["series"] == "Keep"


def test_empty_index_forces_blank_and_never_guesses(tmp_path):
    _write_and_load(
        tmp_path,
        {"canonical_series": {}, "overrides": [{"match": {"title": "Mystery"}, "set": {"series": "S", "series_index": ""}}]},
    )
    out = co.apply_overrides({"title": "Mystery", "author": "X", "series": "Wrong", "series_index": "7"})
    assert out["series_index"] == "", "an explicit blank must clear the volume, not keep a wrong one"


def test_omitted_field_leaves_the_extracted_value_alone(tmp_path):
    _write_and_load(
        tmp_path, {"canonical_series": {}, "overrides": [{"match": {"title": "Name Only"}, "set": {"series": "Fixed"}}]}
    )
    out = co.apply_overrides({"title": "Name Only", "author": "X", "series": "Bad", "series_index": "4"})
    assert (out["series"], out["series_index"]) == ("Fixed", "4")


def test_file_field_disambiguates_identical_titles(tmp_path):
    _write_and_load(
        tmp_path,
        {
            "canonical_series": {},
            "overrides": [
                {"match": {"title": "Twin", "file": "twin-a.m4b"}, "set": {"series": "A"}},
                {"match": {"title": "Twin", "file": "twin-b.m4b"}, "set": {"series": "B"}},
            ],
        },
    )
    out = co.apply_overrides({"title": "Twin", "author": None, "series": "", "series_index": ""}, path=Path("/x/twin-b.m4b"))
    assert out["series"] == "B"


def test_an_entry_may_correct_the_field_it_is_keyed_on(tmp_path):
    """Matching uses pre-correction values, so retitling is safe."""
    _write_and_load(
        tmp_path,
        {"canonical_series": {}, "overrides": [{"match": {"title": "Old Name"}, "set": {"title": "New Name"}}]},
    )
    out = co.apply_overrides({"title": "Old Name", "author": None, "series": "", "series_index": ""})
    assert out["title"] == "New Name"


def test_missing_file_is_a_no_op(tmp_path):
    co.reload_overrides(tmp_path / "does-not-exist.json")
    out = co.apply_overrides({"title": "Tenacity", "author": "Dakota Krout", "series": "S", "series_index": "1"})
    assert (out["series"], out["series_index"]) == ("S", "1")


def test_malformed_file_does_not_break_the_build(tmp_path):
    bad = tmp_path / "broken.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    co.reload_overrides(bad)
    out = co.apply_overrides({"title": "Tenacity", "author": "Dakota Krout", "series": "S", "series_index": "1"})
    assert (out["series"], out["series_index"]) == ("S", "1")


def test_extract_metadata_is_wired_to_the_layer():
    """Guards the hook site in app/metadata.py against a silent removal."""
    import app.metadata as md

    assert hasattr(md, "apply_overrides")
    body = (REPO_ROOT / "app" / "metadata.py").read_text(encoding="utf-8").split("def extract_metadata")[1]
    assert "apply_overrides(" in body
