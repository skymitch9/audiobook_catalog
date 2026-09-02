# tests/test_ocr_supplements.py
# The DISAMBIGUATED identity for armed scan PDFs — KNOWN_ISSUES KI-8.
#
# 🔴 THE BUG THESE TESTS EXIST FOR, and it is a real one that was measured on
# the live state file (2026-09-02): the 16 `needs-ocr` rows are the PDF
# supplements OpenAudible downloads beside an audiobook — maps, family trees,
# ability cards, cover plates, 2 to 39 pages — and their identity is derived
# from the FILE, so they wear the title of the work they accompany. Twelve of
# the sixteen have that work already packed under a longer id:
#
#     supplement  the-way-of-kings                                (25 pages)
#     the work    the-way-of-kings-the-stormlight-archive-book-1  (3,163 chunks)
#
# The two ids differ, which is exactly what makes this dangerous rather than
# loud: a retrieval layer slugging a reader's "The Way of Kings" hits the
# SUPPLEMENT's id precisely, and answers a plot question out of twenty chunks
# of map labels with a citation that looks correct.
#
# So the tests below are written against outcomes, not spellings: what the
# armed row is called, that the OLD identity stops answering, that nothing is
# ever overwritten, and — the one that would have caught the whole class —
# that `build_queue` and the state file AGREE about which id the pack lands on.
# A supplement armed under a new id that `build_queue` never emits would sit
# `pending` for ever while every row said the feature was working.

from __future__ import annotations

import pytest

from app.core.book_ocr import SOURCE_PDF_OCR
from app.core.ingest_queue import (
    SOURCE_PDF_OCR_FALLBACK, STATUS_DONE, STATUS_FAILED, STATUS_NEEDS_OCR,
    STATUS_PENDING, STATUS_SUPERSEDED, SUPPLEMENT_SUFFIX, TIER_NEEDS_OCR,
    apply_supplement_requeue, build_queue, supplement_identity,
    supplement_rekey,
)


def _state(**rows):
    return {"version": 1, "books": dict(rows), "runs": []}


def _held(title, **extra):
    row = {"status": STATUS_NEEDS_OCR, "title": title, "source": SOURCE_PDF_OCR}
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# the formula
# ---------------------------------------------------------------------------

class TestSupplementIdentity:

    def test_it_says_supplement_in_the_title_and_in_the_id(self):
        title, book_id = supplement_identity("The Way of Kings")
        assert title == "The Way of Kings (supplement)"
        assert book_id == "the-way-of-kings-supplement"

    def test_the_id_is_derived_from_the_new_title_not_appended_to_the_old_id(self):
        # ⚠️ book_id is a PERSISTED KEY — it names the object in the gated
        # bucket and the row publish_index serves. Two ways to build one is how
        # two keys start disagreeing, so the id must come from the SAME
        # book_id_from_title every other id in the queue comes from.
        from app.core.review_join import book_id_from_title

        for raw in ("I'm from the Sun", "He Who Fights with Monsters 12- A LitRPG Adventure",
                    "Atlas of the Heart"):
            title, book_id = supplement_identity(raw)
            assert book_id == book_id_from_title(title)

    def test_the_new_id_can_never_equal_the_old_one(self):
        # The whole point is a DISTINCT identity; a title that slugged to the
        # same id would silently reinstate the collision.
        from app.core.review_join import book_id_from_title

        for raw in ("The Way of Kings", "Rhythm of War", "Fae and Fare",
                    "Jake's Magical Market 3", "Wind and Truth"):
            _, new_id = supplement_identity(raw)
            assert new_id != book_id_from_title(raw)

    def test_the_source_string_copy_agrees_with_book_ocr(self):
        # ingest_queue keeps its own copy of "pdf-ocr" so it stays importable
        # without PyMuPDF (build_queue takes its classifier by injection for
        # that reason). This is what makes the copy safe rather than a drift
        # waiting to happen.
        assert SOURCE_PDF_OCR_FALLBACK == SOURCE_PDF_OCR


# ---------------------------------------------------------------------------
# the arming primitive
# ---------------------------------------------------------------------------

class TestApplySupplementRequeue:

    def test_it_arms_a_new_row_and_retires_the_old_identity(self):
        state = _state(**{"the-way-of-kings": _held("The Way of Kings",
                                                   reason="image-scan: 638 chars")})
        out = apply_supplement_requeue(state, ["the-way-of-kings"])

        assert out["armed"] == ["the-way-of-kings-supplement"]
        new = state["books"]["the-way-of-kings-supplement"]
        assert new["status"] == STATUS_PENDING          # armed: _ocr_sort_tier reads this
        assert new["title"] == "The Way of Kings (supplement)"
        assert new["source"] == SOURCE_PDF_OCR
        assert new["supplement_of"] == "the-way-of-kings"
        # The failure reason survives the move, like apply_requeue's.
        assert new["previous_reason"] == "image-scan: 638 chars"

        old = state["books"]["the-way-of-kings"]
        assert old["status"] == STATUS_SUPERSEDED
        assert old["superseded_by"] == "the-way-of-kings-supplement"

    def test_the_old_row_is_kept_not_deleted(self):
        # A row that says where the content went is the difference between a
        # decision and an amnesia: publish_index serves every row, so the old
        # id keeps answering "this moved", not nothing.
        state = _state(**{"rhythm-of-war": _held("Rhythm of War")})
        apply_supplement_requeue(state, ["rhythm-of-war"])
        assert "rhythm-of-war" in state["books"]

    def test_superseded_is_not_requeueable(self):
        # ⚠️ The retired identity must not be resurrectable by the ordinary
        # retry control, or two rows would race for one PDF.
        from app.core.ingest_queue import REQUEUABLE_STATUSES, apply_requeue

        assert STATUS_SUPERSEDED not in REQUEUABLE_STATUSES
        state = _state(**{"rhythm-of-war": _held("Rhythm of War")})
        apply_supplement_requeue(state, ["rhythm-of-war"])
        out = apply_requeue(state, ["rhythm-of-war"])
        assert out["requeued"] == []
        assert out["skipped_other"] == ["rhythm-of-war"]

    def test_done_is_untouchable(self):
        # Same safety property as apply_requeue's, and it matters more here: a
        # packed book re-identified would orphan its pack in the bucket under a
        # key nothing indexes.
        state = _state(**{"atlas-of-the-heart": {"status": STATUS_DONE,
                                                 "title": "Atlas of the Heart",
                                                 "chunks": 12}})
        out = apply_supplement_requeue(state, ["atlas-of-the-heart"])
        assert out["armed"] == []
        assert out["skipped_done"] == ["atlas-of-the-heart"]
        assert state["books"]["atlas-of-the-heart"]["status"] == STATUS_DONE

    def test_a_failed_row_is_not_silently_re_identified(self):
        # `failed` books belong to --requeue-failed, which keeps their
        # identity. Re-identifying one here would be a second control quietly
        # doing a different thing.
        state = _state(**{"get-it-done": {"status": STATUS_FAILED,
                                          "title": "Get It Done"}})
        out = apply_supplement_requeue(state, ["get-it-done"])
        assert out["armed"] == [] and out["skipped_other"] == ["get-it-done"]

    def test_an_unknown_id_is_dropped_not_invented(self):
        state = _state()
        out = apply_supplement_requeue(state, ["no-such-book"])
        assert out["unknown"] == ["no-such-book"]
        assert state["books"] == {}

    def test_a_taken_supplement_id_is_REFUSED_never_overwritten(self):
        # 🔴 The one outcome that would be worse than the bug: clobbering a row
        # that already exists under the supplement id.
        state = _state(**{
            "the-way-of-kings": _held("The Way of Kings"),
            "the-way-of-kings-supplement": {"status": STATUS_DONE,
                                            "title": "The Way of Kings (supplement)",
                                            "chunks": 20},
        })
        out = apply_supplement_requeue(state, ["the-way-of-kings"])
        assert out["armed"] == []
        assert out["skipped_collision"] == ["the-way-of-kings"]
        assert state["books"]["the-way-of-kings-supplement"]["chunks"] == 20
        assert state["books"]["the-way-of-kings"]["status"] == STATUS_NEEDS_OCR

    def test_arming_twice_is_not_two_identities(self):
        state = _state(**{"wind-and-truth": _held("Wind and Truth")})
        apply_supplement_requeue(state, ["wind-and-truth"])
        out = apply_supplement_requeue(state, ["wind-and-truth"])
        assert out["armed"] == []
        assert out["skipped_already"] == ["wind-and-truth"]
        assert len([k for k in state["books"] if k.startswith("wind-and-truth")]) == 2

    def test_every_bucket_is_always_present(self):
        # The caller logs the buckets with different words; a missing key would
        # be a KeyError in the reporting path, i.e. at the worst moment.
        out = apply_supplement_requeue(_state(), [])
        assert set(out) == {"armed", "unknown", "skipped_done", "skipped_other",
                            "skipped_already", "skipped_collision"}


# ---------------------------------------------------------------------------
# the re-key, and the queue agreeing with the state file
# ---------------------------------------------------------------------------

class TestSupplementRekey:

    def test_an_unarmed_book_keeps_its_own_identity(self):
        state = _state(**{"fae-and-fare": _held("Fae and Fare")})
        assert supplement_rekey(state, "fae-and-fare", "Fae and Fare") == (
            "fae-and-fare", "Fae and Fare")

    def test_an_armed_book_answers_under_the_new_identity(self):
        state = _state(**{"fae-and-fare": _held("Fae and Fare")})
        apply_supplement_requeue(state, ["fae-and-fare"])
        assert supplement_rekey(state, "fae-and-fare", "Fae and Fare") == (
            "fae-and-fare-supplement", "Fae and Fare (supplement)")

    def test_a_book_this_processor_never_saw_is_left_alone(self):
        assert supplement_rekey(_state(), "unheard-of", "Unheard Of") == (
            "unheard-of", "Unheard Of")


class TestBuildQueueHonoursTheArmedIdentity:
    """⚠️ THE TEST THAT MATTERS MOST. Everything downstream keys off the
    QueueItem: `_ocr_hold_reason` reads the row for `item.book_id`, `pack_one`
    marks it and uploads `text/<item.book_id>.json.gz`, and `publish_index`
    serves the state key. If `build_queue` kept emitting the OLD id, the armed
    row would sit `pending` for ever while every surface said the feature
    worked — the same invisible stall the sort_tier fix was written to end.
    """

    @staticmethod
    def _queue(state, monkeypatch, tmp_path):
        from app.core import ingest_queue

        pdf = tmp_path / "Brandon Sanderson" / "wok.pdf"
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(b"%PDF-1.4\n")
        monkeypatch.setattr(ingest_queue, "load_ebooks", lambda: [
            {"path": "Brandon Sanderson/wok.pdf", "format": "pdf",
             "title": "The Way of Kings"},
        ])
        monkeypatch.setattr(ingest_queue, "ebooks_root", lambda: tmp_path)
        monkeypatch.setattr(ingest_queue, "load_catalog", lambda: [])
        monkeypatch.setattr(ingest_queue, "load_additions_log", lambda: {})
        return build_queue(state=state, review_counts={},
                           pdf_classifier=lambda p: {"ok": False,
                                                     "reason": "image-scan"})

    def test_before_arming_the_item_wears_the_works_title(self, monkeypatch, tmp_path):
        state = _state(**{"the-way-of-kings": _held("The Way of Kings")})
        item, = self._queue(state, monkeypatch, tmp_path)
        assert (item.book_id, item.title) == ("the-way-of-kings", "The Way of Kings")
        assert item.tier == TIER_NEEDS_OCR

    def test_after_arming_the_item_carries_the_supplement_identity(self, monkeypatch, tmp_path):
        state = _state(**{"the-way-of-kings": _held("The Way of Kings")})
        apply_supplement_requeue(state, ["the-way-of-kings"])
        item, = self._queue(state, monkeypatch, tmp_path)
        assert item.book_id == "the-way-of-kings-supplement"
        assert item.title == "The Way of Kings (supplement)"

    def test_the_lane_label_does_not_move(self, monkeypatch, tmp_path):
        # ⚠️ `tier` is a CROSS-REPO CONTRACT (ingest_queue_summary.LANE_BY_TIER
        # -> the sibling status page). Re-keying is an identity change, not a
        # lane change, and it must not quietly become one.
        state = _state(**{"the-way-of-kings": _held("The Way of Kings")})
        apply_supplement_requeue(state, ["the-way-of-kings"])
        item, = self._queue(state, monkeypatch, tmp_path)
        assert item.tier == TIER_NEEDS_OCR

    def test_arming_still_promotes_it_out_of_the_tail(self, monkeypatch, tmp_path):
        # The sort_tier fix reads the status of the id the item now carries; if
        # the re-key and that read disagreed, arming would go back to doing
        # nothing at all.
        from app.core.ingest_queue import OCR_ARMED_SORT_TIER

        state = _state(**{"the-way-of-kings": _held("The Way of Kings")})
        apply_supplement_requeue(state, ["the-way-of-kings"])
        item, = self._queue(state, monkeypatch, tmp_path)
        assert item.sort_tier == OCR_ARMED_SORT_TIER

    def test_a_packed_supplement_leaves_the_queue(self, monkeypatch, tmp_path):
        # is_done must be asked about the id the pack ACTUALLY landed on, or
        # every night would re-OCR a finished supplement.
        state = _state(**{"the-way-of-kings": _held("The Way of Kings")})
        apply_supplement_requeue(state, ["the-way-of-kings"])
        state["books"]["the-way-of-kings-supplement"]["status"] = STATUS_DONE
        assert self._queue(state, monkeypatch, tmp_path) == []

    def test_the_works_own_id_is_never_claimed_by_the_supplement(self, monkeypatch, tmp_path):
        # The actual KI-8 hazard, stated as an outcome: after arming, nothing
        # in the queue answers to the work's identity.
        state = _state(**{"the-way-of-kings": _held("The Way of Kings")})
        apply_supplement_requeue(state, ["the-way-of-kings"])
        ids = {i.book_id for i in self._queue(state, monkeypatch, tmp_path)}
        assert "the-way-of-kings" not in ids


# ---------------------------------------------------------------------------
# the CLI
# ---------------------------------------------------------------------------

class TestRequeueOcrAsSupplement:

    @staticmethod
    def _armed(monkeypatch, state, **kwargs):
        from app.tools import ingest_books

        monkeypatch.setattr(ingest_books, "ocr_available", lambda: (True, "stub 1.0"))
        return ingest_books.requeue_ocr(dry_run=True, state=state, **kwargs)

    def test_the_flag_routes_to_the_disambiguating_primitive(self, monkeypatch):
        state = _state(**{"the-wandering-inn": _held("The Wandering Inn")})
        out = self._armed(monkeypatch, state, as_supplement=True)
        assert out["armed"] == ["the-wandering-inn-supplement"]

    def test_without_the_flag_the_old_behaviour_is_untouched(self, monkeypatch):
        state = _state(**{"the-wandering-inn": _held("The Wandering Inn")})
        out = self._armed(monkeypatch, state)
        assert out["requeued"] == ["the-wandering-inn"]
        assert state["books"]["the-wandering-inn"]["status"] == STATUS_PENDING

    def test_no_engine_refuses_before_any_identity_is_written(self, monkeypatch):
        # ⚠️ Same refusal as the plain arming, and it must fire FIRST: arming
        # books for a lane that cannot read them turns a named blocker into a
        # book that looks queued and never moves — and a rewritten identity
        # would make that state harder to undo, not easier.
        from app.tools import ingest_books

        monkeypatch.setattr(ingest_books, "ocr_available",
                            lambda: (False, "rapidocr-onnxruntime not installed"))
        state = _state(**{"the-wandering-inn": _held("The Wandering Inn")})
        out = ingest_books.requeue_ocr(dry_run=True, state=state, as_supplement=True)
        assert out["armed"] == [] and "refused" in out
        assert state["books"]["the-wandering-inn"]["status"] == STATUS_NEEDS_OCR

    def test_the_bulk_selector_skips_an_already_moved_identity(self, monkeypatch):
        state = _state(**{"wind-and-truth": _held("Wind and Truth"),
                          "fae-and-fare": _held("Fae and Fare")})
        self._armed(monkeypatch, state, as_supplement=True)
        # Both are now superseded; a second sweep must find nothing to do
        # rather than re-arming the retired ids into a permanent stall.
        out = self._armed(monkeypatch, state, as_supplement=True)
        assert out["armed"] == []

    def test_dry_run_writes_no_state_file(self, monkeypatch, tmp_path):
        from app.core import ingest_queue
        from app.tools import ingest_books

        path = tmp_path / "ingest_state.json"
        monkeypatch.setattr(ingest_queue, "STATE_PATH", path)
        monkeypatch.setattr(ingest_books, "STATE_PATH", path)
        state = _state(**{"wind-and-truth": _held("Wind and Truth")})
        self._armed(monkeypatch, state, as_supplement=True)
        assert not path.exists()

    def test_the_modifier_alone_is_refused_rather_than_ignored(self, monkeypatch):
        # A flag that silently does nothing reads exactly like a flag that
        # worked. Exit 1 and a sentence, not a status print.
        from app.tools import ingest_books

        assert ingest_books.main(["--as-supplement"]) == 1

    def test_the_suffix_is_stated_once(self):
        # If this ever needs to change it is a MIGRATION of a persisted key,
        # and this assertion is the tripwire that says so.
        assert SUPPLEMENT_SUFFIX == " (supplement)"


@pytest.mark.parametrize("title,expected", [
    ("The Way of Kings", "the-way-of-kings-supplement"),
    ("Rhythm of War", "rhythm-of-war-supplement"),
    ("The Wandering Inn", "the-wandering-inn-supplement"),
    ("Jake's Magical Market 3", "jake-s-magical-market-3-supplement"),
    ("He Who Fights with Monsters 10", "he-who-fights-with-monsters-10-supplement"),
])
def test_the_real_shelfs_ids_do_not_collide_with_their_packed_works(title, expected):
    # The five worst real cases, named. Each of these works IS packed, under a
    # longer id built from the catalog's fuller title, and the supplement's
    # short id used to be a prefix of it — which is what let a slugged query
    # land on the wrong one.
    from app.core.review_join import book_id_from_title

    new_title, new_id = supplement_identity(title)
    assert new_id == expected
    assert not book_id_from_title(title).startswith(new_id)
    assert new_title.endswith(SUPPLEMENT_SUFFIX)
