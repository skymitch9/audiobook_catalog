"""app/tools/missing_purchases_report.py — the daily full-library look.

Why: the 15-minute audit sees only the newest 50 purchases, so a burst of 50+
adds hid two older gaps for weeks. The properties pinned:

  1. it covers OLD purchases the top-50 window cannot see;
  2. it uses the same matcher and the same licence-refused exclusions;
  3. a profile with no rows is a FAILED report, never a shorter list;
  4. the Discord lines stay under the message limit and name the overflow.

Nothing here contacts Audible: ``collect`` is not called, ``summarise`` is pure.
"""

from __future__ import annotations

from app.tools import audit_new_purchases as anp
from app.tools import missing_purchases_report as mpr


def _row(title, date, asin, profile="skylar"):
    return {"title_short": title, "title": title, "author": "A", "purchase_date": date,
            "asin": asin, "profile": profile}


def _library(n_recent=60):
    """60 recent owned purchases plus one OLD gap below the top-50 window."""
    rows = [_row(f"Owned Book {i}", f"2026-10-{1 + i % 28:02d}", f"R{i}") for i in range(n_recent)]
    rows.append(_row("The Forgotten One", "2025-01-02", "OLD1", "samantha"))
    rows.append(_row("Paige Pick", "2026-11-01", "P1", "paige"))
    return rows


CATALOG = {anp.norm(f"Owned Book {i}") for i in range(60)} | {anp.norm("Paige Pick")}


def test_the_full_check_sees_an_old_gap_the_top_50_window_cannot():
    books = _library()
    assert anp.missing_purchases(books, CATALOG, top=50) == []
    got = mpr.summarise(books, CATALOG, skip_asins={})
    assert got["ok"] is True and got["covered_all"] is True
    assert got["items"] == [{"date": "2025-01-02", "title": "The Forgotten One",
                             "asin": "OLD1", "profile": "samantha"}]
    assert got["checked"] == len(books)


def test_licence_refused_titles_stay_excluded():
    got = mpr.summarise(_library(), CATALOG, skip_asins={"OLD1": {}})
    assert got["items"] == []


def test_a_profile_with_no_rows_fails_the_report_rather_than_shrinking_it():
    books = [b for b in _library() if b["profile"] != "paige"]
    got = mpr.summarise(books, CATALOG, skip_asins={})
    assert got["ok"] is False and "paige" in got["error"]


def test_a_library_bigger_than_the_cap_says_it_was_not_covered():
    got = mpr.summarise(_library(), CATALOG, skip_asins={}, top=10)
    assert got["covered_all"] is False and got["checked"] == 10


def test_missing_purchases_keeps_its_old_date_title_shape():
    books = [_row("Gap", "2026-11-02T10:00:00", "G1")]
    assert anp.missing_purchases(books, set(), top=5) == [("2026-11-02", "Gap")]
    assert anp.missing_purchase_rows(books, set(), top=5)[0]["asin"] == "G1"


def test_discord_lines_stay_under_the_limit_and_name_the_overflow():
    items = [{"date": "2026-01-01", "title": "X" * 80, "asin": f"A{i}", "profile": "skylar"}
             for i in range(60)]
    lines = mpr.discord_lines(items, {"A0"})
    assert sum(len(ln) + 1 for ln in lines) < 2000
    assert "🆕" in lines[0] and "🆕" not in lines[1]
    assert "more in output_files/missing_purchases_report.json" in lines[-1]


# ---------------------------------------------------------------------------
# Owned under another title — dropped ONLY when verified on Drive and on disk
# (owner order 2026-11-24: "do 1 but add checking the drive manifest to verify
# we have it").
# ---------------------------------------------------------------------------

import json
from pathlib import Path

import pytest

PERCY = "Rick Riordan\\Percy Jackson & the Olympians - The Demigod Files.m4b"


def _owned_books():
    """The real shape of the defect: the Audible title does not match the
    catalogue title of the file we hold."""
    return _library() + [_row("Percy Jackson: The Demigod Files", "2024-05-01", "B002V1NINC")]


def _ownership(*, on_drive=(PERCY,), on_disk=(PERCY,), owned_as=None, asin_index=None):
    return mpr.Ownership(
        on_drive={mpr.path_key(k): k for k in on_drive},
        owned_as=owned_as if owned_as is not None else {
            "B002V1NINC": {"file": "Rick Riordan/Percy Jackson & the Olympians - The Demigod Files.m4b",
                           "why": "test"}},
        asin_index=asin_index or {},
        on_disk=lambda key: key in set(on_disk),
    )


def _titles(report):
    return [i["title"] for i in report["items"]]


def test_without_verification_an_owned_retitled_book_is_listed():
    got = mpr.summarise(_owned_books(), CATALOG, skip_asins={})
    assert "Percy Jackson: The Demigod Files" in _titles(got)
    assert "verified_owned" not in got


def test_a_mapping_verified_on_drive_and_disk_drops_the_title_and_says_how():
    got = mpr.summarise(_owned_books(), CATALOG, skip_asins={}, ownership=_ownership())
    assert _titles(got) == ["The Forgotten One"]
    [v] = got["verified_owned"]
    assert v["asin"] == "B002V1NINC" and v["how"] == "owned_as" and v["file"] == PERCY


def test_a_mapping_whose_file_is_NOT_on_drive_does_not_suppress_and_says_why():
    got = mpr.summarise(_owned_books(), CATALOG, skip_asins={},
                        ownership=_ownership(on_drive=(), on_disk=(PERCY,)))
    [percy] = [i for i in got["items"] if i["asin"] == "B002V1NINC"]
    assert "not on Drive" in percy["note"]
    assert got["verified_owned"] == []


def test_a_mapping_on_drive_but_NOT_on_disk_does_not_suppress():
    got = mpr.summarise(_owned_books(), CATALOG, skip_asins={},
                        ownership=_ownership(on_disk=()))
    [percy] = [i for i in got["items"] if i["asin"] == "B002V1NINC"]
    assert "not on local disk" in percy["note"]


def test_a_companion_epub_on_drive_never_counts_as_the_audiobook():
    epub = "Rick Riordan\\Percy Jackson & the Olympians - The Demigod Files.epub"
    manifest = {epub: {"drive_file_id": "x"}, "A\\No Id.m4b": {"drive_file_id": ""}}
    assert mpr.manifest_audiobooks(manifest) == {}
    own = _ownership(owned_as={"B002V1NINC": {"file": epub, "why": "t"}}, on_drive=(), on_disk=(epub,))
    own.on_drive = mpr.manifest_audiobooks(manifest)
    got = mpr.summarise(_owned_books(), CATALOG, skip_asins={}, ownership=own)
    assert "Percy Jackson: The Demigod Files" in _titles(got)


def test_manifest_keys_match_separator_and_case_blind():
    assert mpr.path_key(PERCY) == mpr.path_key("rick riordan/PERCY JACKSON & the olympians - the demigod files.m4b")


def test_the_asin_tag_route_drops_a_title_only_for_an_on_disk_file():
    own = _ownership(owned_as={}, asin_index={"B002V1NINC": PERCY})
    got = mpr.summarise(_owned_books(), CATALOG, skip_asins={}, ownership=own)
    assert got["verified_owned"][0]["how"] == "asin_tag"
    own = _ownership(owned_as={}, asin_index={"B002V1NINC": PERCY}, on_disk=())
    assert "Percy Jackson: The Demigod Files" in _titles(
        mpr.summarise(_owned_books(), CATALOG, skip_asins={}, ownership=own))


def test_a_failed_mapping_is_not_rescued_by_the_tag_index_but_names_it():
    other = "Rick Riordan\\Demigod Files (2).m4b"
    own = _ownership(on_drive=(other,), on_disk=(other,), asin_index={"B002V1NINC": other})
    got = mpr.summarise(_owned_books(), CATALOG, skip_asins={}, ownership=own)
    [percy] = [i for i in got["items"] if i["asin"] == "B002V1NINC"]
    assert "not on Drive" in percy["note"] and other in percy["note"]


def test_a_note_does_not_change_the_posted_identity_and_reaches_discord():
    item = {"date": "2026-01-01", "title": "T", "asin": "A1", "profile": "skylar", "note": "mapped to X but X is not on Drive"}
    assert mpr.asin_key(item) == "A1"
    assert "not on Drive" in mpr.discord_lines([item], set())[0]


def test_load_ownership_fails_loudly_without_a_manifest_or_a_library(tmp_path):
    with pytest.raises(FileNotFoundError):
        mpr.load_ownership(tmp_path / "nope", tmp_path / "m.json")
    with pytest.raises(FileNotFoundError):
        mpr.load_ownership(tmp_path, tmp_path / "m.json")
    (tmp_path / "m.json").write_text("{trunc", encoding="utf-8")
    with pytest.raises(ValueError):
        mpr.load_ownership(tmp_path, tmp_path / "m.json")


def test_load_ownership_reads_a_real_shaped_manifest_and_disk(tmp_path):
    lib = tmp_path / "books"
    (lib / "Rick Riordan").mkdir(parents=True)
    (lib / "Rick Riordan" / "Percy Jackson & the Olympians - The Demigod Files.m4b").write_bytes(b"not audio")
    man = tmp_path / "upload_manifest.json"
    man.write_text(json.dumps({PERCY: {"drive_file_id": "d1", "author": "Rick Riordan"}}), encoding="utf-8")
    owned = tmp_path / "owned_as.json"
    owned.write_text(json.dumps({"_doc": "x", "B002V1NINC": {
        "file": "Rick Riordan/Percy Jackson & the Olympians - The Demigod Files.m4b", "why": "t"}}),
        encoding="utf-8")
    own = mpr.load_ownership(lib, man, owned)
    assert own.asin_index == {}  # the fake file has no tags; unreadable, skipped
    got = mpr.summarise(_owned_books(), CATALOG, skip_asins={}, ownership=own)
    assert [v["asin"] for v in got["verified_owned"]] == ["B002V1NINC"]


def test_without_mutagen_the_tag_index_is_empty_and_says_so(monkeypatch, tmp_path):
    import builtins
    real = builtins.__import__

    def _no_mutagen(name, *a, **k):
        if name.startswith("mutagen"):
            raise ImportError("no mutagen here")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_mutagen)
    index, note = mpr.build_asin_index(tmp_path, {"k": "A\\B.m4b"})
    assert index == {} and "unavailable" in note


def test_the_committed_owned_as_map_is_complete_and_evidenced():
    data = json.loads(mpr.OWNED_AS_PATH.read_text(encoding="utf-8"))
    entries = mpr.load_owned_as(mpr.OWNED_AS_PATH)
    assert entries, "scripts/owned_as.json must be tracked and seeded"
    for asin, e in entries.items():
        assert asin == asin.strip() and asin in data
        assert e.get("file", "").lower().endswith(mpr.AUDIOBOOK_EXTS), asin
        assert len(e.get("why", "")) > 30, f"{asin} has no evidence"


def test_the_manifest_path_matches_the_sync_owner(monkeypatch):
    monkeypatch.delenv("SYNC_DATA_DIR", raising=False)
    assert mpr.manifest_path() == Path(__file__).resolve().parents[1] / "scripts" / "upload_manifest.json"
    monkeypatch.setenv("SYNC_DATA_DIR", "D:/elsewhere")
    assert mpr.manifest_path() == Path("D:/elsewhere") / "upload_manifest.json"
