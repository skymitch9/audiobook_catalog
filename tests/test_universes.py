"""Tests for the shared universe list (app/core/universes.py).

Three jobs:

  * pin the mechanism - resolution order, normalisation, and the fail-safe
    behaviour when the list is missing or broken, which matters more here than
    anywhere because this pipeline runs unattended three times a day;
  * run the SHARED FIXTURES, which library_catalog's TypeScript also runs. That
    is the only thing stopping the two lookups drifting apart. There is no
    shared runtime between a Python static build and a Cloudflare Worker, so
    there is no shared implementation;
  * pin the three cases that prove a series-keyed mapping is insufficient, by
    name, because they are the reason the file has the shape it has.

The data is NOT in this repo. If these tests skip, catalog-platform is not
where app/core/universes.py can see it - set CATALOG_PLATFORM_DIR.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core import universes as uv

PLATFORM_DIR, _TRIED = uv.find_platform_dir()

# Skipped rather than failed on purpose, and this is the same reasoning as the
# module's: a checkout that has no sibling catalog-platform must still be able to
# run this repo's suite. library_catalog does the opposite and hard-fails,
# because it BUNDLES the list and shipping an empty one is worse than stopping.
requires_platform = pytest.mark.skipif(
    PLATFORM_DIR is None,
    reason=f"catalog-platform not found (tried: {'; '.join(_TRIED)}). Set {uv.ENV_VAR}.",
)

FIXTURES = (
    json.loads((PLATFORM_DIR / "data" / "universes.fixtures.json").read_text(encoding="utf-8"))
    if PLATFORM_DIR
    else {"cases": [], "canonicalNameCases": []}
)


@pytest.fixture(autouse=True)
def _restore_default_list():
    """A test that loads a temp file must not leak into the next one."""
    yield
    uv.reload_universes()


# --------------------------------------------------------------------------- #
# The shared contract
# --------------------------------------------------------------------------- #


@requires_platform
@pytest.mark.parametrize("case", FIXTURES["cases"], ids=lambda c: c["name"])
def test_shared_fixture_cases(case):
    """The same file library_catalog runs. If these disagree, the catalogs disagree."""
    assert uv.universe_for(title=case.get("title"), series=case.get("series")) == case.get("expect")


@requires_platform
@pytest.mark.parametrize("case", FIXTURES["canonicalNameCases"], ids=lambda c: c["input"])
def test_shared_canonical_name_cases(case):
    assert uv.canonical_universe_name(case["input"]) == case.get("expect")


@requires_platform
def test_the_fixture_file_is_not_empty():
    """A truncated fixtures file must not pass by vacuum."""
    assert len(FIXTURES["cases"]) >= 15
    assert len(FIXTURES["canonicalNameCases"]) >= 5


# --------------------------------------------------------------------------- #
# The three cases that decide the design
#
# Already inside the fixtures. Repeated by name on purpose: a fixture file can be
# edited, and these three are why the file has the shape it has. If one fails,
# the answer is not to change the test.
# --------------------------------------------------------------------------- #


@requires_platform
def test_secret_projects_is_mixed_four_in():
    for title in (
        "Tress of the Emerald Sea",
        "Yumi and the Nightmare Painter",
        "The Sunlit Man",
        "Isles of the Emberdark",
    ):
        assert uv.universe_for(title=title, series="") == "The Cosmere", title


@requires_platform
def test_secret_projects_is_mixed_one_out_and_that_row_is_the_proof():
    assert uv.universe_for(title="The Frugal Wizard’s Handbook for Surviving Medieval England", series="") is None


@requires_platform
def test_the_exclusion_survives_a_curly_apostrophe():
    """site/catalog.csv stores U+2019. A lookup that does not fold it gets the one
    row the whole design rests on wrong, and nothing else."""
    curly = "The Frugal Wizard’s Handbook for Surviving Medieval England"
    straight = "The Frugal Wizard's Handbook for Surviving Medieval England"
    assert curly != straight
    assert uv.universe_for(title=curly) is None
    assert uv.universe_for(title=straight) is None


@requires_platform
def test_secret_projects_as_a_series_resolves_to_nothing():
    """It is a real series value in this catalog and must never be in a series list."""
    assert uv.universe_for(title="Any Book At All", series="Secret Projects") is None


@requires_platform
def test_the_otherlife_trilogy_has_no_series_value():
    for title in (
        "Otherlife Dreams - The Selfless Hero Trilogy",
        "Otherlife Nightmares - The Selfless Hero Trilogy",
        "Otherlife Awakenings - The Selfless Hero Trilogy",
    ):
        assert uv.universe_for(title=title, series="") == "Runnerverse", title
    assert uv.universe_for(title="", series="The Selfless Hero Trilogy") is None


@requires_platform
def test_fires_of_december_is_a_seriesless_standalone_that_is_cosmere():
    assert uv.universe_for(title="Fires of December", series="") == "The Cosmere"
    assert uv.universe_for(title="Fires of December") == "The Cosmere"


# --------------------------------------------------------------------------- #
# Resolution order and normalisation
# --------------------------------------------------------------------------- #


@requires_platform
def test_an_exclusion_beats_a_series_that_would_otherwise_claim_the_row():
    # ⚠️ Rewritten 2026-08-15 alongside the fixture of the same name. It was
    # Lux with series "The Stormlight Archive"; Lux stopped being an exclusion
    # when the owner approved the Reckoners universe, because an exclusion is a
    # GLOBAL stop and would have blocked Reckoners' own claim on that row. The
    # Frugal Wizard is the exclusion this mechanism was built for.
    assert (
        uv.universe_for(
            title="The Frugal Wizard’s Handbook for Surviving Medieval England",
            series="The Stormlight Archive",
        )
        is None
    )


@requires_platform
def test_an_override_beats_a_series_from_another_universe():
    assert uv.universe_for(title="Warbreaker", series="Zodiac Academy") == "The Cosmere"


@requires_platform
def test_a_series_answers_when_the_title_says_nothing():
    assert uv.universe_for(title="Some Book Nobody Listed", series="Crescent City") == "Maasverse"


@requires_platform
def test_titles_match_exactly_never_by_substring():
    """Substring matching would make Elantris shadow The Hope of Elantris."""
    assert uv.universe_for(title="Elantris") == "The Cosmere"
    assert uv.universe_for(title="The Hope of Elantris") == "The Cosmere"
    assert uv.universe_for(title="Elantris: The Annotated Edition") is None


@requires_platform
def test_not_series_never_returns_a_universe():
    # ⚠️ Reckoners and The Skyward Series became universes of their own on
    # 2026-08-15 (owner-approved), so those two rows now resolve — and that does
    # NOT overturn the refusal being tested. The Cosmere's notSeries still lists
    # both, and "not Cosmere" is all it ever claimed, so the assertion is stated
    # that way now. Legion, claimed by nothing, still pins the plain None.
    assert uv.universe_for(title="Steelheart", series="Reckoners") != "The Cosmere"
    assert uv.universe_for(title="Skyward", series="The Skyward Series") != "The Cosmere"
    assert uv.universe_for(title="Legion", series="Legion") is None


@requires_platform
def test_none_is_the_ordinary_answer():
    assert uv.universe_for() is None
    assert uv.universe_for(title="", series="") is None
    assert uv.universe_for(title=None, series=None) is None
    assert uv.universe_for(title="Nothing", series="Nothing") is None


@requires_platform
def test_normalisation_folds_case_whitespace_and_quotes():
    assert uv.universe_for(series="  the   stormlight ARCHIVE ") == "The Cosmere"
    assert uv.universe_for(series="Monster’s Mercy") == "Runnerverse"
    assert uv.universe_for(series="Artorian’s Archives") == "CAL Verse"


# --------------------------------------------------------------------------- #
# The approved content - a bad edit in catalog-platform fails HERE
# --------------------------------------------------------------------------- #


@requires_platform
def test_seventeen_universes_in_the_order_the_owner_approved():
    # ⚠️ Willverse added 2026-08-12 was the SEVENTH. Marvel and Disney added
    # 2026-08-15 (owner/coordinator: separate universes). Same day, revised
    # again: Star Wars split OUT of Disney on the owner's crossover-potential
    # criterion, and Alliances was created (owner-approved, 'human'-decided —
    # not just llm-proposed like the others). Per-item membership on Marvel/
    # Disney/Star Wars is still 'llm'-decided, not individually owner-
    # reviewed — see their `confirmed` fields in data/universes.json. This
    # assertion failing is the test WORKING: an edit in catalog-platform
    # cannot land in either catalog unnoticed. Cytoverse (12th) and Reckoners
    # (13th) were created later the same day during the estate-wide orphan
    # sweep, both owner-approved and both "human"-decided. Middle-earth (14th),
    # Dungeon Crawler Carl (15th) and Innworld (16th) followed within the hour,
    # when the owner ruled on that sweep's verdict table. DotHack is the 17th,
    # added 2026-08-24 by direct edit ("change .hack to DotHack as the verse
    # name" — a leading-dot name reads oddly as a chip, and the CLI cannot
    # create a universe at all). Reconciled here 2026-08-26, mirroring
    # library_catalog/packages/core/test/universes.test.ts, which had already
    # been updated: ⚠️ this test failing for TWO DAYS is exactly the mechanism
    # working — a universe cannot appear in catalog-platform without a decision
    # landing in both catalogs — but it also means the audiobook suite was red
    # for two days, which is how a real regression hides.
    assert uv.universe_names() == [
        "The Cosmere",
        "Runnerverse",
        "CAL Verse",
        "Maasverse",
        "Riordanverse",
        "Solaria",
        "Willverse",
        "Marvel",
        "Disney",
        "Star Wars",
        "Alliances",
        "Cytoverse",
        "Reckoners",
        "Middle-earth",
        "Dungeon Crawler Carl",
        "Innworld",
        "DotHack",
    ]


@requires_platform
def test_the_counts_the_owner_signed_off():
    doc = json.loads((PLATFORM_DIR / "data" / "universes.json").read_text(encoding="utf-8"))
    counts = {
        u["name"]: (len(u.get("series", [])), len(u.get("bookOverrides", [])), len(u.get("bookExclusions", [])))
        for u in doc["universes"]
    }
    assert counts == {
        # 36 since 2026-08-15: +22 for Brotherwise Games' Cosmere RPG line and
        # the Mistborn deckbuilder (board-game D1, null series), +1 Shards of
        # Creation ("literally all the gods from the cosmere" — owner), +3 for
        # Arcanum Unbounded / The Emperor's Soul / Shadows for Silence in the
        # Forests of Hell — the last two used to be caught by the SERIES
        # values "Cosmere"/"The Cosmere" (a universe masquerading as a
        # series); those series fields are now blanked non-destructively at
        # the source (library_catalog change_log; audiobook_catalog
        # corrections layer for Arcanum) and caught by title instead, which
        # is also why `series` below dropped from 5 to 3.
        # series 3 -> 4 later on 2026-08-15: +White Sand, the Cosmere
        # graphic-novel line (library work #90). An author-keyed scan cannot
        # find it — `authors` reads "Julius Gopez Rik Hoskin", the artist and
        # scripter, so the word Sanderson is nowhere on the row. Exclusions
        # 8 -> 5: Snapshot, Lux and Firstborn / Defending Elysium moved out,
        # because an exclusion is a GLOBAL stop and would have blocked the new
        # Reckoners/Cytoverse overrides on those exact titles. The Cosmere
        # still refuses all three, via notSeries and the new entries' own why.
        "The Cosmere": (4, 36, 5),
        # 12 since 2026-08-12: Turncoat's Truth restored from _refused once the owner
        # verified the co-authored book does sit inside the continuity.
        "Runnerverse": (12, 3, 0),
        # +1 since 2026-08-15: 'Divine Dungeon the Game' — not a new universe,
        # since canonicalNames already folds 'divine dungeon universe' onto
        # CAL Verse.
        "CAL Verse": (9, 1, 0),
        "Maasverse": (3, 0, 0),
        # 6 -> 9 on 2026-08-24, in the same owner edit that created DotHack:
        # catalog-platform completed the Riordanverse to his ruling that ALL
        # Rick Riordan books belong in it. The three added series are Magnus
        # Chase and the Gods of Asgard, The Kane Chronicles, and the ampersand
        # spelling "Percy Jackson & the Olympians" (a spelling, not a new
        # membership — normaliseUniverseText keeps "and"/"&" distinct).
        "Riordanverse": (9, 0, 0),
        "Solaria": (2, 0, 0),
        # Cradle and The Last Horizon are owned; The Elder Empire and The
        # Traveler's Gate are listed so a future purchase files itself.
        "Willverse": (4, 0, 0),
        # New 2026-08-15. 77 title overrides: 72 Marvel/X-Men/Deadpool
        # board-game rows inside the mixed "Dice Throne" series (unclaimed at
        # series level), 4 audiobook Avengers tie-ins, 1 library "Little
        # Golden Book" row.
        # +1 later on 2026-08-15: "Panther Patience - Spidey and His Amazing
        # Friends" — a Disney Junior imprint row with Marvel characters, so it
        # goes to Marvel and not Disney, like the Age of Ultron tie-ins.
        "Marvel": (0, 78, 0),
        # New 2026-08-15, then revised the SAME day: Star Wars split out (see
        # below), leaving just the Toy Story series claim + 11 seriesless
        # Disney Books imprint titles (12 minus Star Wars: Ahsoka, moved out).
        # 1 -> 2 series and 11 -> 20 overrides later the same day: the first
        # pass keyed on the literal word "Disney" IN THE TITLE, and half the
        # imprint's rows do not carry it. Re-run by author = "Disney Books" and
        # the set closes: +Lady and the Tramp (a real series value), +3 Frozen,
        # +3 Mickey/Minnie, +Peter Pan, +The Lion King, +The Nightmare Before
        # Christmas (library work #197, the first Disney row found outside this
        # catalog). Each was tested against the owner's crossover-potential
        # criterion one at a time rather than swept.
        # +1 override on the owner's Winnie-the-Pooh ruling, which also settled
        # a general criterion (Disney's new `criterion` field): FRANCHISE-
        # inclusive, so a kid-recognisable Disney property belongs even where
        # the row's own provenance is not Disney's — "My First Winnie-the-Pooh"
        # is credited to A. A. Milne.
        "Disney": (2, 21, 0),
        # New 2026-08-15, split out of Disney on the owner's crossover-
        # potential criterion: 3 series (High Republic, Legends, Boba Fett) +
        # 1 title override (Ahsoka, seriesless) — moved verbatim from Disney.
        # +1 series later the same day: "Darth Vader and Family", Jeffrey
        # Brown's licensed Chronicle Books picture-book line (library work
        # #190, "Goodnight Darth Vader").
        "Star Wars": (4, 1, 0),
        # New 2026-08-15, owner-approved creation (not just llm-proposed):
        # Stan Lee's Alliances, 1 series claim, both owned volumes.
        "Alliances": (1, 0, 0),
        # New 2026-08-15, owner-approved during the estate-wide orphan sweep.
        # Sanderson's non-Cosmere SF continuity: the "The Skyward Series" claim
        # covers 7 audiobooks here, and the override covers library work #8
        # "Firstborn / Defending Elysium", which carries no series at all —
        # Defending Elysium's own ebook edition is subtitled "A Cytoverse
        # Novella".
        "Cytoverse": (2, 1, 0),
        # New 2026-08-15, owner-approved during the same sweep. Two series
        # because the spin-off carries a DIFFERENT series value ("Texas
        # Reckoners series", on Lux), and one override because Snapshot carries
        # none at all in either catalog — the two facts that make this a
        # universe rather than just a series.
        "Reckoners": (2, 1, 0),
        # New 2026-08-15. 1 series (the 5 LotR audiobooks here) + 13 title
        # overrides, and the override count is the point: the 12 Ascension game
        # rows and the LotR 5e book are filed under "Ascension" and "D&D",
        # neither claimable at series level because both also hold unrelated
        # products. The clearest case in the file of a universe saying what no
        # series name says.
        "Middle-earth": (1, 13, 0),
        # New 2026-08-15. ONE series claim covering 8 audiobooks here, 6
        # library works and 29 board-game rows — the games only reachable
        # because Board_Game_Catalog set series="Dungeon Crawler Carl" on ids
        # 570-598 the same day. Universe is the only tier a games row can join
        # the estate at (work_fold is null for games by design), which is what
        # earns a single-series franchise a universe here.
        "Dungeon Crawler Carl": (1, 0, 0),
        # New 2026-08-15. Named for pirateaba's world, not for The Wandering
        # Inn, so neither of its two series is elevated over the other
        # (Solaria's naming rule). Singer of Terandria is set on a continent of
        # the same world; the household owns Gravesong and Huntsong.
        "Innworld": (2, 0, 0),
        # New 2026-08-24, renamed from ".hack" to "DotHack" at the owner's
        # request ("change .hack to DotHack as the verse name") — a leading-dot
        # name reads oddly as a chip. 4 series (.hack//Another Birth /
        # G.U.+ / Legend of the Twilight / XXXX), no overrides or exclusions.
        # ⚠️ The SERIES values keep their real leading-dot spellings; only the
        # UNIVERSE was renamed, and confusing the two would unclaim all four.
        "DotHack": (4, 0, 0),
    }


@requires_platform
def test_every_book_entry_carries_a_reason():
    """A bare mapping is indistinguishable from a typo, and nobody re-checks one."""
    doc = json.loads((PLATFORM_DIR / "data" / "universes.json").read_text(encoding="utf-8"))
    for u in doc["universes"]:
        for field in ("bookOverrides", "bookExclusions"):
            for b in u.get(field, []):
                assert b.get("why", "").strip(), f"{u['name']} {field} {b.get('title')!r} has no why"


@requires_platform
def test_the_four_held_out_subjects_are_still_refused():
    # ⚠️ "Will Wight" was the fifth and is deliberately GONE — that refusal was
    # answered on 2026-08-12 and became the Willverse. Dropping it here is the other
    # half of the decision; leaving it would assert a refusal that no longer exists.
    doc = json.loads((PLATFORM_DIR / "data" / "universes.json").read_text(encoding="utf-8"))
    subjects = " | ".join(r["subject"] for r in doc["_refused"])
    for needle in ("Turncoat's Truth", "Cultivating Chaos", "The Axe Falls", "Tailored Realities"):
        assert needle in subjects, needle


@requires_platform
def test_no_held_out_series_has_been_swept_into_a_universe():
    doc = json.loads((PLATFORM_DIR / "data" / "universes.json").read_text(encoding="utf-8"))
    for r in doc["_refused"]:
        for s in r.get("heldOutSeries", []):
            assert uv.universe_for(series=s) is None, f"{s!r} is held out by {r['subject']!r} and yet resolves"


@requires_platform
def test_the_axe_falls_is_held_out_under_its_real_series_spelling():
    """The refusal names the book; the series value in site/catalog.csv is different.
    Testing the wrong spelling would pass while protecting nothing."""
    assert uv.universe_for(title="The Axe Falls - The Axe Falls Series, Book 1", series="The Axe Falls Series") is None


# --------------------------------------------------------------------------- #
# Fail-safe: this pipeline runs unattended and must never die over this file
# --------------------------------------------------------------------------- #


def test_a_missing_file_is_a_warning_and_not_a_crash(tmp_path, capsys):
    uv.reload_universes(tmp_path / "nope.json")
    assert uv.is_loaded() is False
    assert uv.universe_for(title="Warbreaker") is None
    assert uv.universe_names() == []
    assert "[WARN]" in capsys.readouterr().err


def test_malformed_json_is_a_warning_and_not_a_crash(tmp_path, capsys):
    broken = tmp_path / "universes.json"
    broken.write_text('{"schemaVersion": 1, "universes": [ oops', encoding="utf-8")
    uv.reload_universes(broken)
    assert uv.is_loaded() is False
    assert uv.universe_for(series="The Stormlight Archive") is None
    assert "not valid JSON" in capsys.readouterr().err


def test_an_unexpected_schema_version_warns_but_still_reads(tmp_path, capsys):
    """Visible, not silent - but it must not take a 3x-daily unattended build down."""
    f = tmp_path / "universes.json"
    f.write_text(
        json.dumps(
            {
                "schemaVersion": 99,
                "canonicalNames": {"testverse": "Testverse"},
                "universes": [{"name": "Testverse", "decidedHow": "seed", "series": ["A Series"]}],
            }
        ),
        encoding="utf-8",
    )
    uv.reload_universes(f)
    assert uv.is_loaded() is True
    assert uv.universe_for(series="A Series") == "Testverse"
    assert "schemaVersion" in capsys.readouterr().err


def test_report_coverage_never_raises_and_writes_nothing(capsys):
    """It reports. It must not add a column, and it must survive odd rows."""
    rows = [
        {"title": "Warbreaker", "series": ""},
        {"title": "", "series": "The Stormlight Archive"},
        {"title": None, "series": None},
        {},
    ]
    before = [dict(r) for r in rows]
    counts = uv.report_coverage(rows)
    assert rows == before, "report_coverage must not mutate the rows it is handed"
    assert isinstance(counts, dict)
    assert "[INFO] Universes" in capsys.readouterr().out


def test_report_coverage_survives_the_list_being_absent(tmp_path, capsys):
    uv.reload_universes(tmp_path / "nope.json")
    counts = uv.report_coverage([{"title": "Warbreaker", "series": ""}])
    assert counts == {}
    assert "none loaded" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# The path resolver
# --------------------------------------------------------------------------- #


def test_the_env_var_overrides_discovery(tmp_path, monkeypatch):
    fake = tmp_path / "catalog-platform"
    (fake / "data").mkdir(parents=True)
    (fake / "data" / "universes.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv(uv.ENV_VAR, str(fake))
    found, tried = uv.find_platform_dir()
    assert found == fake.resolve()
    assert str(fake.resolve()) in tried[0]


def test_an_env_var_pointing_nowhere_reports_what_it_tried(tmp_path, monkeypatch):
    monkeypatch.setenv(uv.ENV_VAR, str(tmp_path / "absent"))
    found, tried = uv.find_platform_dir()
    assert found is None
    assert tried and uv.ENV_VAR in tried[0]


def test_discovery_confirms_the_file_and_not_just_the_directory(tmp_path, monkeypatch):
    """A directory called catalog-platform with nothing in it is not a match."""
    empty = tmp_path / "catalog-platform"
    empty.mkdir()
    monkeypatch.setenv(uv.ENV_VAR, str(empty))
    found, _ = uv.find_platform_dir()
    assert found is None


@requires_platform
def test_the_real_checkout_is_found_without_the_env_var(monkeypatch):
    monkeypatch.delenv(uv.ENV_VAR, raising=False)
    found, tried = uv.find_platform_dir()
    assert found is not None, f"tried: {tried}"
    assert (found / "data" / "universes.json").is_file()
    assert (Path(found) / "tools" / "universes.mjs").is_file(), "the editor CLI should be there too"
