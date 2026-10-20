"""Multi-part Audible titles are joined into one m4b (2026-10-20).

The pure pieces: part ordering and the FFMETADATA chapter document. The
ffmpeg path itself was exercised by a dry run on the 8 real multi-part
purchases, not here (CI has no ffmpeg and no Audible auth).
"""
from pathlib import Path

from app.tools.audible_download import build_ffmetadata, natural_key


def test_parts_sort_by_number_not_by_text():
    names = ["Day_16_x.aax", "Day_2_x.aax", "Day_1_x.aax", "Day_10_x.aax"]
    assert [p for p in sorted(names, key=natural_key)] == [
        "Day_1_x.aax", "Day_2_x.aax", "Day_10_x.aax", "Day_16_x.aax"]


def test_natural_key_accepts_paths():
    assert sorted([Path("a/Week_3.aax"), Path("a/Week_1.aax")], key=natural_key)[0].name == "Week_1.aax"


def test_ffmetadata_has_globals_then_one_chapter_per_part():
    doc = build_ffmetadata(
        {"title": "Ab Blaster", "artist": "MoveWith", "composer": None},
        [("10 Min Core", 0, 520567), ("Plank Holds", 520567, 1000000)],
    )
    lines = doc.splitlines()
    assert lines[0] == ";FFMETADATA1"
    assert "title=Ab Blaster" in lines and "artist=MoveWith" in lines
    assert not any(l.startswith("composer=") for l in lines)  # empty values are dropped
    assert lines.count("[CHAPTER]") == 2
    assert "START=520567" in lines and "END=1000000" in lines
    assert "title=Plank Holds" in lines


def test_ffmetadata_escapes_its_special_characters():
    doc = build_ffmetadata({"title": "A=B; C#1 \\ D"}, [])
    assert "title=A\\=B\\; C\\#1 \\\\ D" in doc.splitlines()


def test_bonus_parts_go_after_the_numbered_days():
    names = ["Find_Your_Bliss_Bonus_Meditation_Embodying_Values-LC_64.aax",
             "Find_Your_Bliss_Day_2_Embodiment-LC_64.aax",
             "Find_Your_Bliss_Day_1_Values-LC_64.aax"]
    assert sorted(names, key=natural_key) == [
        "Find_Your_Bliss_Day_1_Values-LC_64.aax",
        "Find_Your_Bliss_Day_2_Embodiment-LC_64.aax",
        "Find_Your_Bliss_Bonus_Meditation_Embodying_Values-LC_64.aax"]
