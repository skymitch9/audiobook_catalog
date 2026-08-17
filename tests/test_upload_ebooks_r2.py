"""Tests for scripts/upload_ebooks_r2.py — the viewer phase-0a file ingest.

Two things are worth pinning here, and neither is "does wrangler work":

  1. ⚠️ **THE KEY SCHEME.** The R2 object key is the `ebooks.json` row's
     `path`, verbatim. The phase-1a Worker will resolve
     `anchor -> path -> object key` assuming the last arrow is the identity
     function, and 1.8 GB of objects are already stored under it. Changing
     `object_key()` is a migration, not an edit — so the golden fixtures below
     spell out exact expected keys, and `test_key_scheme_mutations_fail`
     states, as executable code, which plausible "improvements" (a prefix,
     case folding, keying on the anchor, URL-encoding) must break the suite.

  2. **The skip decision.** 1.8 GB must not re-upload every run, and the
     3x/day pipeline must not read 1.8 GB off disk to learn that. `decide()`
     is the whole of that logic, injected with its hasher so the tests can
     count how often it is called.

Everything that touches the network (`upload_via_wrangler`, `upload_via_s3`)
is deliberately NOT tested here — it is exercised by the real backfill, and a
mock of wrangler would only test the mock.
"""

from __future__ import annotations

import json

import pytest

from scripts import upload_ebooks_r2 as up


# ---------------------------------------------------------------------------
# 1. the key scheme
# ---------------------------------------------------------------------------
GOLDEN = [
    # (row, expected R2 object key)
    ({"path": "Brandon Sanderson/Defiant.pdf", "anchor": "b-a49cd096d824"},
     "Brandon Sanderson/Defiant.pdf"),
    ({"path": "Brené Brown/Atlas of the Heart.epub", "anchor": "b-000000000000"},
     "Brené Brown/Atlas of the Heart.epub"),
    ({"path": "Brandon Sanderson/White Sand Omnibus (Brandon Sanderson's White Sand) - Rik Hoskin.epub",
      "anchor": "b-111111111111"},
     "Brandon Sanderson/White Sand Omnibus (Brandon Sanderson's White Sand) - Rik Hoskin.epub"),
    ({"path": "Shirtaloon/He Who Fights with Monsters 12- A LitRPG Adventure.pdf"},
     "Shirtaloon/He Who Fights with Monsters 12- A LitRPG Adventure.pdf"),
]


@pytest.mark.parametrize("row,expected", GOLDEN)
def test_object_key_is_the_path_verbatim(row, expected):
    assert up.object_key(row) == expected


def test_object_key_normalises_separators_and_leading_slash():
    """A Windows-built manifest could carry backslashes; R2 keys are POSIX."""
    assert up.object_key({"path": r"Brandon Sanderson\Defiant.pdf"}) == "Brandon Sanderson/Defiant.pdf"
    assert up.object_key({"path": "/Brandon Sanderson/Defiant.pdf"}) == "Brandon Sanderson/Defiant.pdf"


def test_object_key_refuses_a_row_with_no_path():
    with pytest.raises(ValueError):
        up.object_key({"anchor": "b-a49cd096d824"})


def test_key_scheme_mutations_fail():
    """⚠️ The mutation guard. Each of these is a plausible "improvement" to
    `object_key`, and each must be WRONG — if any of them starts matching, the
    scheme has silently moved and every object in the bucket is orphaned.
    """
    row = {"path": "Brandon Sanderson/Defiant.pdf", "anchor": "b-a49cd096d824"}
    key = up.object_key(row)
    assert key != "ebooks/" + row["path"], "a bucket prefix would orphan every object"
    assert key != row["path"].lower(), "case folding would orphan every object"
    assert key != row["anchor"], "keying on the anchor is a different scheme (design §2.1)"
    assert key != row["anchor"] + ".pdf", "anchor+ext is a different scheme"
    assert key != row["path"].replace(" ", "%20"), "URL-encoding is the caller's job, not the key's"
    assert key != row["path"].replace("/", "_"), "flattening would collide across authors"


def test_anchor_to_key_is_one_to_one_on_the_real_manifest():
    """The property phase 1a's `GET /api/ebook/:anchor/file` depends on: every
    anchor resolves to exactly one key, and no two anchors share a key.

    Skips when `site/ebooks.json` is absent — it is GITIGNORED (this repo is
    public), so CI checkouts legitimately do not have it.
    """
    if not up.EBOOKS_JSON.exists():
        pytest.skip("site/ebooks.json is gitignored; present only on the pipeline machine")
    rows = json.loads(up.EBOOKS_JSON.read_text(encoding="utf-8"))["ebooks"]
    by_anchor = {}
    for row in rows:
        anchor = row["anchor"]
        assert anchor not in by_anchor, f"duplicate anchor {anchor}"
        by_anchor[anchor] = up.object_key(row)
    assert len(set(by_anchor.values())) == len(by_anchor), "two anchors share one object key"


# ---------------------------------------------------------------------------
# 2. content type / wrangler encoding
# ---------------------------------------------------------------------------
def test_content_types():
    assert up.content_type_for("A/B.epub") == "application/epub+zip"
    assert up.content_type_for("A/B.PDF") == "application/pdf"
    assert up.content_type_for("A/B.mobi") == "application/octet-stream"


def test_wrangler_key_encodes_only_the_three_that_bite():
    """⚠️ `#` silently TRUNCATES the key while wrangler reports success; `%`
    crashes Node. Everything else — spaces, apostrophes, `&`, `(`, non-ASCII —
    was verified working literally by the covers backfill and must stay
    untouched, or the key stored differs from the key the Worker asks for.
    """
    assert up.wrangler_key("A/B#9.epub") == "A/B%239.epub"
    assert up.wrangler_key("A/1% Lifesteal.epub") == "A/1%25 Lifesteal.epub"
    assert up.wrangler_key("A/B?.epub") == "A/B%3F.epub"
    for untouched in ["Brené Brown/Atlas.epub", "A/B & C.epub", "A/Rik's Book (2).epub"]:
        assert up.wrangler_key(untouched) == untouched


# ---------------------------------------------------------------------------
# 3. the skip decision
# ---------------------------------------------------------------------------
META = {"size": 1000, "mtime_ns": 111}


def _hasher(calls):
    def h(key):
        calls.append(key)
        return "deadbeef"
    return h


def test_no_record_uploads_without_hashing():
    calls = []
    verdict, digest = up.decide("k", META, None, force=False, hasher=_hasher(calls))
    assert verdict == "upload"
    assert calls == [], "a file with no record is uploaded regardless; hashing it first is waste"


def test_size_and_mtime_match_skips_without_hashing():
    """The whole point: the 3x/day pipeline step must not read 1.8 GB to say
    'nothing changed'."""
    calls = []
    rec = {"size": 1000, "mtime_ns": 111, "sha256": "deadbeef"}
    verdict, digest = up.decide("k", META, rec, force=False, hasher=_hasher(calls))
    assert verdict == "skip"
    assert digest == "deadbeef"
    assert calls == []


def test_mtime_moved_but_bytes_identical_skips_after_hashing():
    """A touched / re-copied / re-exported-identical file costs a hash, never
    the uplink. ⚠️ The HASH is the authority; mtime may only ever say 'skip'
    faster, never 'upload' on its own."""
    calls = []
    rec = {"size": 1000, "mtime_ns": 999, "sha256": "deadbeef"}
    verdict, digest = up.decide("k", META, rec, force=False, hasher=_hasher(calls))
    assert verdict == "skip"
    assert calls == ["k"], "the mtime differed, so it had to hash to be sure"


def test_changed_bytes_upload():
    calls = []
    rec = {"size": 1000, "mtime_ns": 999, "sha256": "0ldc0ffee"}
    verdict, digest = up.decide("k", META, rec, force=False, hasher=_hasher(calls))
    assert verdict == "upload"
    assert digest == "deadbeef"


def test_size_changed_uploads_even_when_mtime_matches():
    """A same-mtime size change is a rewrite (a restore from backup will do
    it). Size is part of the tier-1 key precisely so this is not skipped."""
    calls = []
    rec = {"size": 42, "mtime_ns": 111, "sha256": "0ldc0ffee"}
    verdict, _ = up.decide("k", META, rec, force=False, hasher=_hasher(calls))
    assert verdict == "upload"


def test_force_uploads_without_hashing():
    calls = []
    rec = {"size": 1000, "mtime_ns": 111, "sha256": "deadbeef"}
    verdict, _ = up.decide("k", META, rec, force=True, hasher=_hasher(calls))
    assert verdict == "upload"
    assert calls == []


def test_record_missing_mtime_still_decides_by_hash():
    """Records written before mtime existed, or by a different tool, must fall
    through to the hash rather than skipping on a None == None coincidence."""
    calls = []
    rec = {"size": 1000, "sha256": "deadbeef"}
    verdict, _ = up.decide("k", META, rec, force=False, hasher=_hasher(calls))
    assert verdict == "skip"
    assert calls == ["k"], "no recorded mtime means tier 1 cannot fire"


# ---------------------------------------------------------------------------
# 4. the 300 MiB wall and the backend split
# ---------------------------------------------------------------------------
def test_wrangler_limit_is_the_measured_one():
    """⚠️ MEASURED 2026-08-17 against wrangler 4.123.0, twice (--file and
    --pipe): 'Wrangler only supports uploading files up to 300 MiB in size'.
    Exactly one of the 168 files (393 MiB) is over it."""
    assert up.WRANGLER_MAX_BYTES == 300 * 1024 * 1024
    assert 189930310 < up.WRANGLER_MAX_BYTES, "the 181 MiB handbook goes via wrangler"
    assert 412436591 > up.WRANGLER_MAX_BYTES, "the 393 MiB omnibus does not"


def test_s3_unavailable_reason_names_what_to_fix(monkeypatch):
    """A refusal must say what happened, what it needs, and how to get it —
    never a bare failure."""
    for name in up.S3_ENV:
        monkeypatch.delenv(name, raising=False)
    reason = up.s3_unavailable_reason()
    assert reason
    assert "R2_ACCESS_KEY_ID" in reason
    assert "estate-ebooks" in reason


def test_upload_timeout_scales_with_size():
    assert up.upload_timeout_for(1000) == 300, "a small file still gets a floor"
    assert up.upload_timeout_for(412436591) > 2000, "393 MiB needs minutes, not 180 s"


# ---------------------------------------------------------------------------
# 5. bookkeeping
# ---------------------------------------------------------------------------
def test_orphans_are_reported_not_deleted():
    local = {"A/keep.epub": {"size": 1}}
    record = {"A/keep.epub": {"size": 1}, "A/gone.epub": {"size": 2}}
    assert up.orphan_keys(local, record) == ["A/gone.epub"]


def test_record_path_is_gitignored_by_design():
    """⚠️ `site/ebook_files_manifest.json` lists books by filename, which is
    exactly the surface `site/ebooks.json` was gitignored to close on
    2026-08-17 (this repo is PUBLIC). It deviates from design doc §2.1's
    'committed' on purpose. This test fails if someone negates it in
    .gitignore.
    """
    gitignore = (up.PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "!site/ebook_files_manifest.json" not in gitignore
    assert up.RECORD_PATH.name.endswith(".json"), "*.json is ignored by default in this repo"
