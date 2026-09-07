"""B18 (2026-09-07) — STEP 1b records its OWN verdict, so heygabi.ai/status
reads it instead of guessing from a trigger string.

THE DEFECT, AND WHY THE 2026-08-16 HALF-FIX WAS NOT ENOUGH
-----------------------------------------------------------
The /status ebook row went falsely amber three times, each time because it
inferred whether the ebook manifest *should* have moved. 2026-08-16 fixed the
positive case: a run that builds a manifest records `ebookManifestAt` +
`ebookCount`. What stayed a guess was the ABSENCE of those fields, which is
four different facts wearing one appearance:

    a. 1b ran and the read-back of ebooks.json failed   (a real, quiet fault)
    b. 1b was skipped by design      (--rebuild-only, a single-step run)
    c. 1b ran and the BUILD failed                       (a real fault)
    d. the run predates the 2026-08-16 change

Only (b) is harmless, and the page told it apart from (a) and (c) by reading
`trigger` — a CROSS-REPO STRING CONTRACT that degrades silently on a rename.

⚠️ SO THE TESTS THAT MATTER ARE THE ONES ABOUT ABSENCE. Asserting that a
successful build records a timestamp re-tests 2026-08-16. What is new is that
every one of the four cases above now writes a DISTINGUISHABLE record, and
those four are asserted to differ from each other rather than merely to be
non-empty.

⚠️ NOT covered here: `catalog-platform`'s reader. Its half is pinned by
`scripts/test/ebook-lane.test.mjs` in that repo — a two-repo change gets a
test on each side, because a field written correctly and read wrongly is
exactly the class of bug this whole row keeps producing.
"""

from __future__ import annotations

import pytest

from app import pipeline_status as pstatus


class _FakeDoc:
    def __init__(self, store: dict, key: tuple[str, str]):
        self._store, self._key = store, key

    def set(self, payload):
        self._store[self._key] = payload


class _FakeCollection:
    def __init__(self, store: dict, name: str):
        self._store, self._name = store, name

    def document(self, doc_id: str):
        return _FakeDoc(self._store, (self._name, doc_id))


class _FakeDB:
    def __init__(self):
        self.store: dict = {}

    def collection(self, name: str):
        return _FakeCollection(self.store, name)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(pstatus, "_state", {})
    monkeypatch.setattr(pstatus, "_run_id", None)
    monkeypatch.setattr(pstatus, "_run_started", None)
    yield


def _fake_db(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(pstatus, "_client", lambda: db)
    monkeypatch.setattr(pstatus, "_last_write", 0.0)
    return db


def _summary(db):
    return db.store[("pipeline_status", "current")]["summary"]


# ---------------------------------------------------------------------------
# the three states
# ---------------------------------------------------------------------------

def test_built_records_the_stamp_the_page_compares_against(monkeypatch):
    db = _fake_db(monkeypatch)
    pstatus.start_run("scheduled")
    pstatus.ebook_manifest(
        pstatus.EBOOK_MANIFEST_BUILT,
        generated_at="2026-09-07T09:00:04Z",
        count=168,
    )
    assert _summary(db) == {
        "ebookManifestState": "built",
        "ebookManifestAt": "2026-09-07T09:00:04Z",
        "ebookCount": 168,
    }


def test_skipped_is_a_STATEMENT_not_an_absence(monkeypatch):
    """⚠️ THE WHOLE POINT OF B18. Before this, a rebuild-only run wrote no
    ebook fields at all and the page had to deduce "skipped by design" from
    `trigger == "manual-rebuild"`."""
    db = _fake_db(monkeypatch)
    pstatus.start_run("manual-rebuild")
    pstatus.ebook_manifest(
        pstatus.EBOOK_MANIFEST_SKIPPED,
        detail="--rebuild-only: STEP 1b is excluded by design",
    )
    s = _summary(db)
    assert s["ebookManifestState"] == "skipped"
    assert "excluded by design" in s["ebookManifestDetail"]
    # No stamp and no count, because none were produced - the page must not be
    # handed a number it could mistake for a measurement of this run.
    assert "ebookManifestAt" not in s and "ebookCount" not in s


def test_failed_is_no_longer_indistinguishable_from_skipped(monkeypatch):
    """A build that FAILED used to leave exactly what a deliberate skip left:
    nothing. So a real fault got the harmless green."""
    db = _fake_db(monkeypatch)
    pstatus.start_run("scheduled")
    pstatus.ebook_manifest(pstatus.EBOOK_MANIFEST_FAILED, detail="build_manifest returned 2")
    s = _summary(db)
    assert s["ebookManifestState"] == "failed"
    assert s["ebookManifestState"] != pstatus.EBOOK_MANIFEST_SKIPPED


def test_built_without_a_stamp_still_says_it_RAN(monkeypatch):
    """⚠️ CASE (a): the build succeeded and reading `site/ebooks.json` back
    failed. "It ran, but I cannot tell you when" is strictly more than the page
    could know before, and it is what stops this landing in the `skipped`
    bucket."""
    db = _fake_db(monkeypatch)
    pstatus.start_run("scheduled")
    pstatus.ebook_manifest(
        pstatus.EBOOK_MANIFEST_BUILT,
        detail="built, but site/ebooks.json could not be read back: boom",
    )
    s = _summary(db)
    assert s["ebookManifestState"] == "built"
    assert "ebookManifestAt" not in s
    assert "could not be read back" in s["ebookManifestDetail"]


def test_the_four_absence_cases_are_all_distinguishable(monkeypatch):
    """The assertion the whole item exists for: (a) ran-but-unreadable,
    (b) skipped, (c) failed and (d) a pre-2026-08-16 run must NOT be four
    spellings of the same record."""
    seen = []
    for call in (
        dict(state=pstatus.EBOOK_MANIFEST_BUILT, detail="read-back failed"),   # a
        dict(state=pstatus.EBOOK_MANIFEST_SKIPPED, detail="rebuild-only"),     # b
        dict(state=pstatus.EBOOK_MANIFEST_FAILED, detail="build returned 2"),  # c
    ):
        db = _fake_db(monkeypatch)
        pstatus.start_run("scheduled")
        pstatus.ebook_manifest(**call)
        seen.append(_summary(db).get("ebookManifestState"))

    # (d) is the legacy run: no call at all, hence no field.
    db = _fake_db(monkeypatch)
    pstatus.start_run("scheduled")
    seen.append(_summary(db).get("ebookManifestState"))

    assert seen == ["built", "skipped", "failed", None]
    assert len(set(map(str, seen))) == 4


# ---------------------------------------------------------------------------
# the never-raise contract
# ---------------------------------------------------------------------------

def test_never_raises_without_credentials(monkeypatch):
    """Design rule 1 of this module: a status backend being absent must not
    cost a run. `_client()` returning None is a fresh clone."""
    monkeypatch.setattr(pstatus, "_client", lambda: None)
    pstatus.ebook_manifest(pstatus.EBOOK_MANIFEST_BUILT, generated_at="x", count=1)


def test_never_raises_when_firestore_throws(monkeypatch):
    class _Boom:
        def collection(self, _name):
            raise RuntimeError("firestore down")

    monkeypatch.setattr(pstatus, "_client", lambda: _Boom())
    monkeypatch.setattr(pstatus, "_last_write", 0.0)
    pstatus.ebook_manifest(pstatus.EBOOK_MANIFEST_FAILED, detail="whatever")


def test_a_long_detail_is_truncated(monkeypatch):
    """`pipeline_status/current` is world-readable and rendered; an unbounded
    exception string is neither useful nor safe to paste into a row."""
    db = _fake_db(monkeypatch)
    pstatus.start_run("scheduled")
    pstatus.ebook_manifest(pstatus.EBOOK_MANIFEST_FAILED, detail="x" * 5000)
    assert len(_summary(db)["ebookManifestDetail"]) == 300


def test_it_does_not_disturb_the_rest_of_the_summary(monkeypatch):
    """It goes through set_summary(), so it accumulates rather than replaces —
    `driveParityState`, `uploaded` and `books` share this dict."""
    db = _fake_db(monkeypatch)
    pstatus.start_run("scheduled")
    pstatus.set_summary(uploaded=3, driveParityState="ok")
    pstatus.ebook_manifest(pstatus.EBOOK_MANIFEST_BUILT, generated_at="t", count=9)
    s = _summary(db)
    assert s["uploaded"] == 3 and s["driveParityState"] == "ok"
    assert s["ebookManifestState"] == "built"


# ---------------------------------------------------------------------------
# the call sites — the halves a unit test of the recorder cannot see
# ---------------------------------------------------------------------------

def test_the_two_skip_paths_record_skipped():
    """⚠️ SOURCE-LEVEL, and deliberately so: exercising `_run_rebuild_only_body`
    or `_run_step_body` means running real pipeline steps against the live
    library, which this suite must never do. What is pinned is that neither
    entry point can quietly stop recording — the failure mode being guarded is
    somebody deleting the call, not the call misbehaving."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "scripts" / "sync_to_drive.py").read_text(
        encoding="utf-8"
    )
    for marker in ("--rebuild-only: STEP 1b is excluded by design",
                   "STEP 1b is not part of it"):
        assert marker in src, f"a skip path stopped recording its verdict: {marker}"
    assert src.count("EBOOK_MANIFEST_SKIPPED") == 2
    # built (read back OK), built (read-back failed), failed (rc), failed (exc)
    assert src.count("EBOOK_MANIFEST_BUILT") == 2
    assert src.count("EBOOK_MANIFEST_FAILED") == 2
