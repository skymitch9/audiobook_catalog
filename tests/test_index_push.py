# tests/test_index_push.py
# The shared-index projection (app/index_push.py): allow-list, raw strings,
# and the cover-URL canonicalisation that mirrors site/covers-base.js.
#
# ⚠️ What is deliberately NOT tested: any fold/normalisation of titles or
# authors — there is none. The index Worker folds on write (index-worker
# design §6); these tests assert the pusher sends RAW strings untouched.

import csv as _csv
import json
from pathlib import Path

import pytest

from app.index_push import (
    ALLOWED_KEYS,
    build_ebook_rows,
    build_projection,
    canonical_cover_url,
    detail_url_for,
    ebooks_detail_url,
    load_ebook_manifest,
    push_from_disk,
)

BASE = "https://covers.heygabi.ai/"
REPO_ROOT = Path(__file__).resolve().parents[1]


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
# The pipeline step (sync STEP 7) fails soft without configuration
# --------------------------------------------------------------------------- #


def write_catalog(tmp_path, *rows):
    """A catalog.csv on disk — push_from_disk() reads files, not memory."""
    p = tmp_path / "catalog.csv"
    with open(p, "w", encoding="utf-8", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=list(row().keys()))
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return p


def test_push_from_disk_skips_quietly_when_the_token_is_unset(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("INDEX_URL", raising=False)
    monkeypatch.delenv("INDEX_PUSH_TOKEN", raising=False)
    summary = push_from_disk(write_catalog(tmp_path, row()), tmp_path / "absent.json")
    assert summary["pushed"] is False and summary["skipped"]
    assert "Index push skipped" in capsys.readouterr().out


def test_the_url_is_a_default_so_the_secret_is_the_only_thing_to_configure(monkeypatch, tmp_path):
    """⚠️ Regression guard for a silent-loss shape (2026-08-17). The CI step
    supplied INDEX_URL; the pipeline's .env holds only the SECRET. If the URL
    were still required, this machine would print 'not set' forever and every
    ebook row would stay out of estate search with nothing looking broken."""
    monkeypatch.delenv("INDEX_URL", raising=False)
    monkeypatch.setenv("INDEX_PUSH_TOKEN", "sekrit")
    calls = {}
    _fake_push(monkeypatch, calls)
    push_from_disk(write_catalog(tmp_path, row()), tmp_path / "absent.json")
    assert calls["url"] == "https://index.heygabi.ai/api/push/audiobook"


def test_push_from_disk_refuses_an_empty_projection(monkeypatch, tmp_path):
    # Configured but nothing to send: zero rows is a failed export — no HTTP.
    # It RAISES rather than returning: the CLI exits non-zero and the pipeline
    # step logs a named WARN, both louder than a quiet "skipped".
    monkeypatch.setenv("INDEX_URL", "https://index.invalid")
    monkeypatch.setenv("INDEX_PUSH_TOKEN", "t")
    with pytest.raises(RuntimeError, match="zero audiobook rows"):
        push_from_disk(write_catalog(tmp_path), tmp_path / "absent.json")


def test_push_from_disk_sends_bearer_put(monkeypatch, tmp_path):
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
    summary = push_from_disk(write_catalog(tmp_path, row()), tmp_path / "absent.json")
    assert summary["pushed"] is True and summary["audiobooks"] == 1
    assert summary["result"]["rows"] == 1
    assert calls["url"] == "https://index.example/api/push/audiobook"
    assert calls["headers"]["Authorization"] == "Bearer sekrit"
    assert isinstance(calls["json"], list) and calls["json"][0]["format"] == "audiobook"


# --------------------------------------------------------------------------- #
# Ebook rows (ebook-split design phase 3) — projected from site/ebooks.json,
# riding the SAME audiobook-source snapshot with format:'ebook'
# --------------------------------------------------------------------------- #


def ebook(**overrides):
    """A manifest entry as written by scripts/build_ebook_manifest.py."""
    e = {
        "path": "Brandon Sanderson/Dragonsteel_Prime_by_Brandon_Sanderson.epub",
        "anchor": "b-0123456789ab",
        "filename": "Dragonsteel_Prime_by_Brandon_Sanderson.epub",
        "format": "epub",
        "title": "Dragonsteel Prime",
        "author": "Brandon Sanderson",
        "source": "opf",
        "beside_audiobook": "Brandon Sanderson",
        "size_bytes": 1808754,
        "modified": "2026-06-21T17:41:57.220658Z",
    }
    e.update(overrides)
    return e


def manifest(*ebooks_):
    return {"generated_at": "2026-08-16T23:00:40Z", "root": "C:/x", "count": len(ebooks_), "ebooks": list(ebooks_)}


def test_ebook_rows_use_the_same_allow_list():
    (r,) = build_ebook_rows(manifest(ebook()))
    assert set(r.keys()) == ALLOWED_KEYS


def test_ebook_row_shape():
    (r,) = build_ebook_rows(manifest(ebook()))
    assert r["format"] == "ebook"  # the medium — NOT the file extension
    assert r["source_id"] == "ebook:Brandon Sanderson/Dragonsteel_Prime_by_Brandon_Sanderson.epub"
    assert r["title"] == "Dragonsteel Prime"
    assert r["creator"] == "Brandon Sanderson"
    assert r["series"] is None and r["series_index"] is None and r["year"] is None
    assert r["cover_url"] is None  # a manifest row without a cover pushes null
    # A DEEP LINK to the book on the shelf's own hostname (2026-08-17), not the
    # bare page: estate search used to drop the reader at the top of a
    # 168-tile shelf to find their own book.
    assert r["detail_url"] == "https://ebooks.heygabi.ai/#b-0123456789ab"


# --------------------------------------------------------------------------- #
# Ebook cover pass-through (bookshelf redesign) — the manifest resolves the
# cover at step 1b; the pusher passes it through so estate search gets it
# --------------------------------------------------------------------------- #


def test_ebook_cover_url_passes_through_untouched():
    url = BASE + "ebooks/0f3a51b2c4.jpg"
    (r,) = build_ebook_rows(manifest(ebook(cover_url=url, cover_source="epub")))
    assert r["cover_url"] == url


def test_ebook_sibling_cover_url_passes_through_untouched():
    # An already-canonical audiobook cover URL must not be re-encoded.
    url = BASE + "Brandon%20Sanderson/Dragonsteel%20Prime.jpg"
    (r,) = build_ebook_rows(manifest(ebook(cover_url=url, cover_source="audiobook")))
    assert r["cover_url"] == url
    assert "%2520" not in r["cover_url"]  # the 2026-08-13 double-encode lesson


def test_ebook_relative_cover_href_is_canonicalised_defensively():
    # The manifest stores absolutes; if a relative href ever appears, it is
    # resolved through the ONE canonicaliser rather than pushed broken.
    (r,) = build_ebook_rows(manifest(ebook(cover_url="covers/ebooks/abc.jpg")))
    assert r["cover_url"] == BASE + "ebooks/abc.jpg"


def test_ebook_cover_url_absent_null_or_junk_is_none():
    for cu in (None, "", "   ", 42):
        (r,) = build_ebook_rows(manifest(ebook(cover_url=cu)))
        assert r["cover_url"] is None, repr(cu)
    (r,) = build_ebook_rows(manifest(ebook()))  # key entirely absent
    assert r["cover_url"] is None


def test_cover_source_never_travels():
    # cover_source is the PAGE's provenance field, not in the allow-list.
    (r,) = build_ebook_rows(manifest(ebook(cover_url=BASE + "ebooks/x.jpg", cover_source="epub")))
    assert "cover_source" not in json.dumps(r)
    assert set(r.keys()) == ALLOWED_KEYS


def test_ebook_source_id_never_collides_with_book_key():
    # book_key() always contains '|'; a file path (Windows) never can.
    (r,) = build_ebook_rows(manifest(ebook()))
    assert "|" not in r["source_id"]


def test_filename_sourced_ebook_pushes_title_only():
    # A wrong author is worse than a missing one — null creator is honest.
    (r,) = build_ebook_rows(manifest(ebook(path="Brandon Sanderson/Defiant.pdf", title="Defiant", author=None, source="filename")))
    assert r["title"] == "Defiant"
    assert r["creator"] is None


def test_ebook_titles_are_raw_not_folded():
    (r,) = build_ebook_rows(manifest(ebook(title="Firstborn / Defending Elysium")))
    assert r["title"] == "Firstborn / Defending Elysium"


def test_ebook_ownership_fields_never_travel():
    (r,) = build_ebook_rows(manifest(ebook()))
    dumped = json.dumps(r)
    for verboten in ("size_bytes", "modified", "beside_audiobook", "filename"):
        assert verboten not in dumped


def test_ebook_count_matches_manifest():
    m = manifest(*[ebook(path=f"a/b{i}.epub", title=f"Book {i}") for i in range(25)])
    assert len(build_ebook_rows(m)) == 25


def test_unusable_ebook_entries_are_skipped_not_pushed(capsys):
    m = manifest(ebook(), ebook(path="x/y.epub", title=""), ebook(path="", title="No Path"), "not-a-dict")
    assert len(build_ebook_rows(m)) == 1  # one bad entry must not 422 the snapshot
    assert "skipped 3" in capsys.readouterr().err


def test_duplicate_ebook_paths_keep_first():
    m = manifest(ebook(), ebook(title="Same File, Different Title"))
    rows = build_ebook_rows(m)
    assert len(rows) == 1 and rows[0]["title"] == "Dragonsteel Prime"


def test_ebook_rows_from_none_manifest_is_empty():
    assert build_ebook_rows(None) == []


def test_ebook_rows_are_json_serialisable():
    json.dumps(build_ebook_rows(manifest(ebook())))


def test_ebooks_detail_url_honours_its_own_env_not_the_audiobook_site_url(monkeypatch):
    # ⚠️ The shelf has its OWN hostname; SITE_URL is the audiobook site's and
    # must not steer it (that would send every ebook deep link to the wrong
    # host the moment someone pointed SITE_URL at a staging lane).
    monkeypatch.setenv("SITE_URL", "https://audiobooks.example.test/")
    monkeypatch.setenv("EBOOKS_SITE_URL", "https://shelf.example.test/")
    assert ebooks_detail_url("b-abc123") == "https://shelf.example.test/#b-abc123"


def test_ebooks_detail_url_defaults_to_the_shelf_hostname(monkeypatch):
    monkeypatch.delenv("EBOOKS_SITE_URL", raising=False)
    assert ebooks_detail_url("b-abc123") == "https://ebooks.heygabi.ai/#b-abc123"


def test_an_anchorless_entry_degrades_to_the_bare_shelf_never_a_broken_link(capsys):
    # An older manifest predates the anchor field. A worse link is acceptable;
    # a link to '#undefined' or '#None' is not — and it says so, loudly.
    (r,) = build_ebook_rows(manifest(ebook(anchor=None)))
    assert r["detail_url"] == "https://ebooks.heygabi.ai/"
    assert "carry no `anchor`" in capsys.readouterr().err


def test_the_anchor_is_read_from_the_manifest_never_recomputed():
    """The one-implementation rule, pinned.

    build_ebook_manifest.ebook_anchor() is the single definition; the pusher
    and the page both READ the emitted value. If this module ever grows its
    own derivation, this test is what catches it — a made-up anchor must
    travel through untouched.
    """
    (r,) = build_ebook_rows(manifest(ebook(anchor="b-notarealhash")))
    assert r["detail_url"].endswith("#b-notarealhash")


# --------------------------------------------------------------------------- #
# The manifest loader fails soft — a broken manifest must never break the
# audiobook push
# --------------------------------------------------------------------------- #


def test_missing_manifest_is_none_and_says_the_ROWS_LEAVE(tmp_path, capsys):
    """⚠️ Upgraded from INFO to WARN on 2026-08-17, and the sentence changed
    with it: because the push is a snapshot REPLACE, a missing manifest does
    not mean "no new ebooks this run", it means every ebook row LEAVES estate
    search. A one-line INFO for that would be exactly the silent regression
    the estate's verification rules exist to stop.

    It stayed a WARN after the CI pusher was removed the same day, and the
    reason inverted: this used to be the NORMAL state in CI (a checkout has no
    gitignored manifest) and is now an ABNORMAL state anywhere — the only
    pusher is the pipeline machine, where sync step 1b rewrote the file
    earlier in the same cycle. Reaching it now means 1b failed, or a second
    pusher exists somewhere that cannot hold the manifest. See
    test_the_index_push_lives_in_the_local_pipeline_not_in_ci for the
    arrangement this warning now backstops."""
    assert load_ebook_manifest(tmp_path / "ebooks.json") is None
    err = capsys.readouterr().err
    assert "not found" in err
    assert "ABSENT" in err, "the consequence must be stated, not just the cause"


def test_unparseable_manifest_is_none_with_warn(tmp_path, capsys):
    p = tmp_path / "ebooks.json"
    p.write_text("{not json", encoding="utf-8")
    assert load_ebook_manifest(p) is None
    assert "unreadable" in capsys.readouterr().err


def test_wrong_shape_manifest_is_none_with_warn(tmp_path, capsys):
    for payload in ("[]", '{"count": 3}', '{"ebooks": "nope"}'):
        p = tmp_path / "ebooks.json"
        p.write_text(payload, encoding="utf-8")
        assert load_ebook_manifest(p) is None, payload
        assert "malformed" in capsys.readouterr().err


def test_valid_manifest_round_trips(tmp_path):
    p = tmp_path / "ebooks.json"
    p.write_text(json.dumps(manifest(ebook())), encoding="utf-8")
    loaded = load_ebook_manifest(p)
    assert loaded is not None and len(loaded["ebooks"]) == 1


# --------------------------------------------------------------------------- #
# The pipeline hook carries ebook rows in the same snapshot — and survives
# their absence
# --------------------------------------------------------------------------- #


def _fake_push(monkeypatch, calls):
    class FakeResp:
        ok = True

        def json(self):
            return {"ok": True, "source": "audiobook", "rows": 2, "pushed_at": "now", "unfoldable_titles": 0}

    def fake_put(url, json=None, headers=None, timeout=None):
        calls["url"] = url
        calls["json"] = json
        return FakeResp()

    import requests

    monkeypatch.setattr(requests, "put", fake_put)


def test_push_from_disk_appends_ebook_rows(monkeypatch, tmp_path):
    monkeypatch.setenv("INDEX_URL", "https://index.example/")
    monkeypatch.setenv("INDEX_PUSH_TOKEN", "sekrit")
    p = tmp_path / "ebooks.json"
    p.write_text(json.dumps(manifest(ebook())), encoding="utf-8")
    calls = {}
    _fake_push(monkeypatch, calls)
    summary = push_from_disk(write_catalog(tmp_path, row()), p)
    assert calls["url"].endswith("/api/push/audiobook")  # ⚠️ same source, no /api/push/ebook
    formats = [r["format"] for r in calls["json"]]
    assert formats == ["audiobook", "ebook"]
    ids = {r["source_id"] for r in calls["json"]}
    assert len(ids) == 2  # no cross-kind source_id collision
    # The counts the pipeline step logs and puts on the /status card.
    assert (summary["audiobooks"], summary["ebooks"], summary["rows"]) == (1, 1, 2)


def test_push_from_disk_defaults_to_the_sites_own_files(monkeypatch, tmp_path):
    """Called with no arguments — as sync STEP 7 does — it reads site/
    catalog.csv and site/ebooks.json, the same two files every other local
    check reads. One pipeline, one source of data."""
    import app.index_push as mod

    monkeypatch.setenv("INDEX_PUSH_TOKEN", "sekrit")
    csv_path = write_catalog(tmp_path, row())
    p = tmp_path / "ebooks.json"
    p.write_text(json.dumps(manifest(ebook())), encoding="utf-8")
    monkeypatch.setattr(mod, "SITE_DIR", tmp_path)
    monkeypatch.setattr(mod, "SITE_CSV_NAME", csv_path.name)
    monkeypatch.setattr(mod, "DEFAULT_EBOOKS_PATH", p)
    calls = {}
    _fake_push(monkeypatch, calls)
    assert push_from_disk()["rows"] == 2


def test_push_from_disk_survives_missing_manifest(monkeypatch, tmp_path):
    monkeypatch.setenv("INDEX_URL", "https://index.example/")
    monkeypatch.setenv("INDEX_PUSH_TOKEN", "sekrit")
    calls = {}
    _fake_push(monkeypatch, calls)
    summary = push_from_disk(write_catalog(tmp_path, row()), tmp_path / "absent.json")
    assert summary["pushed"] is True  # the audiobook push happened
    assert [r["format"] for r in calls["json"]] == ["audiobook"]


def test_push_from_disk_survives_malformed_manifest(monkeypatch, tmp_path):
    monkeypatch.setenv("INDEX_URL", "https://index.example/")
    monkeypatch.setenv("INDEX_PUSH_TOKEN", "sekrit")
    p = tmp_path / "ebooks.json"
    p.write_text("{broken", encoding="utf-8")
    calls = {}
    _fake_push(monkeypatch, calls)
    summary = push_from_disk(write_catalog(tmp_path, row()), p)
    assert summary["pushed"] is True
    assert [r["format"] for r in calls["json"]] == ["audiobook"]


def test_push_from_disk_still_refuses_when_audiobooks_are_zero(monkeypatch, tmp_path):
    # Ebook rows alone must NEVER become the snapshot — that would erase the
    # catalog's ~1,078 rows in one replace.
    monkeypatch.setenv("INDEX_URL", "https://index.example/")
    monkeypatch.setenv("INDEX_PUSH_TOKEN", "sekrit")
    p = tmp_path / "ebooks.json"
    p.write_text(json.dumps(manifest(ebook())), encoding="utf-8")
    with pytest.raises(RuntimeError, match="zero audiobook rows"):
        push_from_disk(write_catalog(tmp_path), p)


def test_a_missing_catalog_raises_rather_than_pushing_nothing(monkeypatch, tmp_path):
    """No catalog.csv is a broken working tree, not an empty library. It must
    reach the caller as an exception (STEP 7 logs a named WARN, the CLI exits
    2) — never a quiet no-op that leaves a stale snapshot looking fresh."""
    monkeypatch.setenv("INDEX_PUSH_TOKEN", "sekrit")
    with pytest.raises(FileNotFoundError):
        push_from_disk(tmp_path / "nope.csv", tmp_path / "absent.json")


# --------------------------------------------------------------------------- #
# Dry run — counts and samples, no HTTP
# --------------------------------------------------------------------------- #


def test_dry_run_reports_both_kinds_and_pushes_nothing(monkeypatch, tmp_path, capsys):
    import csv

    from app.index_push import main as push_main

    csv_path = tmp_path / "catalog.csv"
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row().keys()))
        w.writeheader()
        w.writerow(row())
    ebooks_path = tmp_path / "ebooks.json"
    ebooks_path.write_text(json.dumps(manifest(ebook())), encoding="utf-8")

    def explode(*a, **k):  # any HTTP attempt is a test failure
        raise AssertionError("dry run must not push")

    import requests

    monkeypatch.setattr(requests, "put", explode)
    rc = push_main(["--csv", str(csv_path), "--ebooks", str(ebooks_path), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 audiobook row(s)" in out
    assert "1 ebook row(s)" in out
    assert "2 row(s) total" in out
    assert "nothing pushed" in out
    samples = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    assert {s["format"] for s in samples} == {"audiobook", "ebook"}


# --------------------------------------------------------------------------- #
# WHO PUSHES — owner decision 2026-08-17 ("option A"), pinned
# --------------------------------------------------------------------------- #


def _uncommented(text: str) -> str:
    """The file minus its comment lines, so a WARNING ABOUT a thing cannot be
    mistaken for the thing itself — deploy.yml's replacement block names the
    deleted step on purpose."""
    return "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("#"))


def test_the_index_push_lives_in_the_local_pipeline_not_in_ci():
    """⚠️ THE ARRANGEMENT, PINNED. This test replaced a warning-about-a-loss.

    Until 2026-08-17 the index push ran in CI, and the pinned fact was the
    WARN that announced the damage: a CI checkout cannot hold site/ebooks.json
    (gitignored — the repo is PUBLIC), and the push REPLACES the whole
    `audiobook` source, so every deploy silently deleted all 168 ebook rows
    from estate search. The owner picked option A: move the push to the LOCAL
    pipeline, the one writer that owns the manifest.

    So this asserts the new path instead of the old loss:
      * no CI step pushes (the secret is retired and, after the Worker's
        rotation, inert — but a re-added step would 401 forever, silently, and
        the *right* failure to prevent is a second writer existing at all);
      * scripts/sync_to_drive.py calls the pusher, at all three call sites
        that reach the outside world: the 8h cycle, --rebuild-only, and the
        manual `publish` step.
    """
    workflow = _uncommented((REPO_ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8"))
    assert "index_push" not in workflow, "CI must not push the estate index — it can never hold the ebook manifest"
    assert "INDEX_PUSH_TOKEN" not in workflow, "the CI secret is retired; do not reintroduce it"

    sync = (REPO_ROOT / "scripts" / "sync_to_drive.py").read_text(encoding="utf-8")
    assert "def _push_estate_index" in sync
    # One definition + four call sites (cycle, idle cycle, --rebuild-only,
    # manual `publish` step).
    assert sync.count("_push_estate_index(") >= 5, "every path that reaches the outside world must push"


def test_step_7_is_not_gated_on_uploads():
    """A quiet cycle (nothing new to upload) still rewrote site/ebooks.json at
    step 1b, so it still has an index to refresh. The push therefore sits in
    the same unconditional `if not dry_run:` block as STEP 6's commit — the
    exact placement bug that once left ebook-only runs unpublished."""
    sync = (REPO_ROOT / "scripts" / "sync_to_drive.py").read_text(encoding="utf-8")
    body = sync.split("[STEP 6] Auto-commit & push")[1].split("Fulfill any flagged books")[0]
    assert "_push_estate_index()" in body, "STEP 7 must run beside STEP 6, outside the uploaded_count gate"


def test_an_idle_cycle_still_pushes():
    """⚠️ The idle path RETURNS at STEP 2 — it never reaches STEP 6 — so a
    push placed only beside the commit would skip every quiet cycle. That
    matters because the index is a REMOTE system: a push that failed while the
    library was quiet would go un-retried until the next new book arrived,
    which can be days. It costs one PUT of an unchanged snapshot."""
    sync = (REPO_ROOT / "scripts" / "sync_to_drive.py").read_text(encoding="utf-8")
    idle = sync.split("Nothing to upload. All books are synced!")[1].split("finish_run")[0]
    assert "_push_estate_index(record_step=False)" in idle, "an idle cycle must still refresh the index"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
