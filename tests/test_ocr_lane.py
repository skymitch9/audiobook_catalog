# tests/test_ocr_lane.py
# The OCR lane, end to end against a SYNTHETIC scanned PDF.
#
# ⚠️ THE FIXTURE IS A REAL IMAGE SCAN, NOT A MOCK OF ONE. `_image_scan_pdf`
# renders text to a pixmap and puts the PIXMAP into a new PDF, so the file that
# reaches the lane has no text layer at all - `classify_pdf` classifies it as a
# scan for the same measured reason the estate's own 16 files are classified as
# scans, and the only way any text comes back out is if OCR genuinely read it.
# A fixture that quietly kept a text layer would pass every test here while
# proving nothing.
#
# Two tiers of test, on purpose:
#   * a STUB engine (`_stub_engine`) exercises the lane's logic - chapter
#     anchoring, provenance, the quality bars, the budget abort - and runs
#     everywhere, including a CI box with no OCR wheels;
#   * the REAL engine is exercised too, and skipped with a named reason where it
#     is absent. Without the real half, "OCR works" would be a statement about a
#     stub.

from __future__ import annotations

import json
import warnings

import pytest

warnings.filterwarnings("ignore", message=r".*fitz.*")

from app.core import book_ocr
from app.core.book_ocr import (
    OCR_MIN_CHARS_PER_PAGE, OCR_MIN_MEAN_CONFIDENCE, OCR_MIN_TOTAL_CHARS,
    OcrQuality, OcrRefused, SOURCE_PDF_OCR, extract_pdf_ocr, ocr_available,
    quality_refusal,
)
from app.core.book_text import classify_pdf
from app.core.ingest_queue import (
    STATUS_DONE, STATUS_FAILED, STATUS_NEEDS_OCR, STATUS_PENDING, QueueItem,
    TIER_NEEDS_OCR,
)

ENGINE_OK, ENGINE_WORDS = ocr_available()
needs_engine = pytest.mark.skipif(
    not ENGINE_OK, reason=f"no OCR engine on this interpreter: {ENGINE_WORDS}")


PAGE_ONE = "The lamp burned low in the tower window."
PAGE_TWO = "She counted the stairs on the way down."


def _image_scan_pdf(path, pages=(PAGE_ONE, PAGE_TWO), toc=None, dpi=150):
    """Render text -> images -> a PDF with NO text layer. Returns the path."""
    import fitz

    typed = fitz.open()
    for body in pages:
        page = typed.new_page(width=612, height=792)
        page.insert_textbox(fitz.Rect(60, 300, 552, 500), body,
                            fontsize=28, fontname="helv")

    scanned = fitz.open()
    for index in range(typed.page_count):
        png = typed[index].get_pixmap(dpi=dpi).tobytes("png")
        page = scanned.new_page(width=612, height=792)
        page.insert_image(fitz.Rect(0, 0, 612, 792), stream=png)
    if toc:
        scanned.set_toc(toc)
    scanned.save(str(path))
    scanned.close()
    typed.close()
    return str(path)


def _stub_engine(per_page):
    """`png -> [(text, confidence)]`, one canned answer per page in order."""
    calls = {"n": 0}

    def run(_png):
        index = calls["n"]
        calls["n"] += 1
        return per_page[index] if index < len(per_page) else []

    return run


def _prose(words, confidence=0.98):
    """One page's worth of stub lines totalling well over every quality bar."""
    return [(f"{words} line {i} of readable prose about a tower and a lamp.",
             confidence) for i in range(8)]


# --------------------------------------------------------------------------


class TestTheFixtureIsReallyAScan:
    """If the fixture had a text layer every other test here would be a lie."""

    def test_the_synthetic_pdf_classifies_as_an_image_scan(self, tmp_path):
        verdict = classify_pdf(_image_scan_pdf(tmp_path / "scan.pdf"))
        assert verdict["ok"] is False
        assert "image-scan" in verdict["reason"]
        assert verdict["pages"] == 2

    def test_it_carries_no_text_layer_at_all(self, tmp_path):
        import fitz

        doc = fitz.open(_image_scan_pdf(tmp_path / "scan.pdf"))
        try:
            assert sum(len(p.get_text("text") or "") for p in doc) == 0
        finally:
            doc.close()


class TestReadingOrder:
    """⚠️ Shuffled lines read as nonsense and NOTHING downstream reports it."""

    def _box(self, left, top, height=20):
        return [[left, top], [left + 100, top], [left + 100, top + height],
                [left, top + height]]

    def test_boxes_are_banded_by_row_then_ordered_left_to_right(self):
        raw = [
            (self._box(300, 100), "second", 0.9),
            (self._box(10, 400), "third", 0.9),
            (self._box(10, 102), "first", 0.9),
        ]
        assert [t for t, _ in book_ocr._sorted_reading_order(raw)] == [
            "first", "second", "third"]

    def test_a_malformed_entry_is_dropped_not_fatal(self):
        raw = [(self._box(10, 10), "kept", 0.9), None, ("bad",)]
        assert [t for t, _ in book_ocr._sorted_reading_order(raw)] == ["kept"]

    def test_empty_input_is_empty_output(self):
        assert book_ocr._sorted_reading_order([]) == []
        assert book_ocr._sorted_reading_order(None) == []


class TestChapterAnchors:
    def test_no_outline_gives_one_chapter_per_page_with_an_exact_page_anchor(self, tmp_path):
        path = _image_scan_pdf(tmp_path / "scan.pdf")
        book, quality = extract_pdf_ocr(
            path, "b", "Book", engine=_stub_engine([_prose("alpha"), _prose("beta")]))
        assert [c.page for c in book.chapters] == [1, 2]
        assert [c.title for c in book.chapters] == ["Page 1", "Page 2"]
        assert "page-based chapter anchors" in book.notes
        assert quality.pages == 2

    def test_an_outline_is_preferred_and_says_so(self, tmp_path):
        path = _image_scan_pdf(tmp_path / "scan.pdf",
                               toc=[[1, "Front Matter", 1], [1, "The Tower", 2]])
        book, _ = extract_pdf_ocr(
            path, "b", "Book", engine=_stub_engine([_prose("alpha"), _prose("beta")]))
        assert [c.title for c in book.chapters] == ["Front Matter", "The Tower"]
        assert [c.page for c in book.chapters] == [1, 2]
        assert "outline-based chapter anchors" in book.notes

    def test_a_blank_page_starts_no_chapter(self, tmp_path):
        path = _image_scan_pdf(tmp_path / "scan.pdf")
        book, quality = extract_pdf_ocr(
            path, "b", "Book", engine=_stub_engine([[], _prose("beta")]))
        assert [c.page for c in book.chapters] == [2]
        assert quality.pages == 2 and quality.pages_with_text == 1


class TestProvenance:
    """⚠️ A pack's origin must never be ambiguous - `pdf-text` came off a real
    text layer, `pdf-ocr` was read off an image by a machine."""

    def test_the_source_is_pdf_ocr(self, tmp_path):
        book, _ = extract_pdf_ocr(
            _image_scan_pdf(tmp_path / "scan.pdf"), "b", "Book",
            engine=_stub_engine([_prose("alpha"), _prose("beta")]))
        assert book.source == SOURCE_PDF_OCR == "pdf-ocr"

    def test_the_notes_carry_the_engine_the_dpi_and_the_measurement(self, tmp_path):
        book, _ = extract_pdf_ocr(
            _image_scan_pdf(tmp_path / "scan.pdf"), "b", "Book",
            engine=_stub_engine([_prose("alpha"), _prose("beta")]),
            dpi=300, engine_name="rapidocr-onnxruntime")
        joined = " | ".join(book.notes)
        assert "rapidocr-onnxruntime" in joined
        assert "300 dpi" in joined
        assert "mean confidence" in joined

    def test_the_pack_carries_the_source_and_the_notes(self, tmp_path):
        from app.core.book_chunker import chunk_book
        from app.core.ingest_pack import build_pack

        book, _ = extract_pdf_ocr(
            _image_scan_pdf(tmp_path / "scan.pdf"), "b", "Book",
            engine=_stub_engine([_prose("alpha"), _prose("beta")]))
        chunks, refs = chunk_book(book)
        pack = build_pack(book, chunks, refs)
        assert pack["source"] == "pdf-ocr"
        assert any("READ OFF PAGE IMAGES" in n for n in pack["notes"])
        assert all(c.get("page") in (1, 2) for c in pack["chunks"])
        # ⚠️ The chunk contract is unchanged: 800/100, chapter-anchored.
        assert pack["chunk_chars"] == 800 and pack["chunk_overlap"] == 100
        assert pack["ingester_version"] == 1

    def test_no_ord_ceiling_leaks_into_an_ocr_pack(self, tmp_path):
        from app.core.book_chunker import chunk_book
        from app.core.ingest_pack import build_pack

        book, _ = extract_pdf_ocr(
            _image_scan_pdf(tmp_path / "scan.pdf"), "b", "Book",
            engine=_stub_engine([_prose("alpha"), _prose("beta")]))
        chunks, refs = chunk_book(book)
        blob = json.dumps(build_pack(book, chunks, refs))
        for banned in ("ceiling", "max_ord", "ord_ceiling", "visible_until"):
            assert banned not in blob


class TestQualityGate:
    """⚠️ A pack of OCR noise poisons GABI SILENTLY, and each bar names itself."""

    def _q(self, **kw):
        base = dict(pages=10, pages_with_text=10, chars=4000, lines=200,
                    mean_confidence=0.97, garbage_ratio=0.0)
        base.update(kw)
        return OcrQuality(**base)

    def test_a_clean_read_passes(self):
        assert quality_refusal(self._q()) is None

    def test_a_cover_plate_is_refused_by_the_total_char_floor(self):
        # MEASURED 2026-09-01: `The Wandering Inn`'s PDF is 2 pages of cover art
        # and yields 29 chars of stylised lettering across both.
        words = quality_refusal(self._q(pages=2, pages_with_text=2, chars=29))
        assert words and str(OCR_MIN_TOTAL_CHARS) in words
        assert "cover/art plate" in words

    def test_a_thin_read_is_refused_by_the_chars_per_page_bar(self):
        words = quality_refusal(self._q(pages=40, chars=2000))
        assert words and "chars/page" in words
        assert f"{OCR_MIN_CHARS_PER_PAGE:.0f}" in words

    def test_low_confidence_is_refused_and_says_the_number(self):
        words = quality_refusal(self._q(mean_confidence=0.42))
        assert words and "0.420" in words
        assert f"{OCR_MIN_MEAN_CONFIDENCE:.2f}" in words

    def test_garbage_characters_are_refused(self):
        words = quality_refusal(self._q(garbage_ratio=0.4))
        assert words and "garbage-character ratio" in words

    def test_a_page_count_of_zero_is_refused_before_anything_else(self):
        assert "no pages" in quality_refusal(self._q(pages=0))

    def test_reading_nothing_at_all_is_its_own_refusal(self):
        words = quality_refusal(self._q(pages_with_text=0, chars=0))
        assert "read no text at all" in words

    def test_the_garbage_ratio_counts_mojibake_not_ordinary_punctuation(self):
        assert book_ocr._garbage_ratio("It's a fine day - really, \u201cfine\u201d\u2026") == 0.0
        assert book_ocr._garbage_ratio("\x00\x01\x02abc") > 0.4


class TestBudget:
    """⚠️ A PARTIAL READ IS NEVER PACKED - the abort raises, it does not return
    what it has. A book GABI has 'read' half of is worse than one it has not
    read at all, because nothing downstream can tell."""

    def test_a_budget_overrun_raises_rather_than_returning_half_a_book(self, tmp_path):
        path = _image_scan_pdf(tmp_path / "scan.pdf",
                               pages=(PAGE_ONE, PAGE_TWO, PAGE_ONE))

        def slow(_png):
            import time

            time.sleep(0.05)
            return _prose("alpha")

        with pytest.raises(OcrRefused) as exc:
            book_ocr.ocr_pages(path, engine=slow, budget_seconds=0.01)
        assert "budget" in str(exc.value)
        assert "partial read is never packed" in str(exc.value)


class TestOcrHold:
    """OCR is OPT-IN PER BOOK. Nothing is read until somebody arms it."""

    def _item(self):
        return QueueItem("scan-1", "A Scan", TIER_NEEDS_OCR, SOURCE_PDF_OCR,
                         "C:/nope.pdf")

    def test_a_missing_engine_holds_with_the_install_line(self, monkeypatch):
        from app.tools import ingest_books as ib

        monkeypatch.setattr(ib, "ocr_available", lambda: (False, "no OCR engine; install with `pip install x`"))
        words = ib._ocr_hold_reason({"books": {"scan-1": {"status": STATUS_PENDING}}},
                                    self._item())
        assert "install" in words

    def test_an_unarmed_book_is_held_and_told_how_to_arm_it(self, monkeypatch):
        from app.tools import ingest_books as ib

        monkeypatch.setattr(ib, "ocr_available", lambda: (True, "stub"))
        words = ib._ocr_hold_reason(
            {"books": {"scan-1": {"status": STATUS_NEEDS_OCR}}}, self._item())
        assert "--requeue-ocr" in words

    def test_a_book_with_no_state_row_at_all_is_held(self, monkeypatch):
        # ⚠️ A scan PDF that appears on the shelf tomorrow is RECORDED with a
        # named blocker, exactly as before this build - never OCR'd unattended.
        from app.tools import ingest_books as ib

        monkeypatch.setattr(ib, "ocr_available", lambda: (True, "stub"))
        assert ib._ocr_hold_reason({"books": {}}, self._item())

    def test_an_armed_book_proceeds(self, monkeypatch):
        from app.tools import ingest_books as ib

        monkeypatch.setattr(ib, "ocr_available", lambda: (True, "stub"))
        assert ib._ocr_hold_reason(
            {"books": {"scan-1": {"status": STATUS_PENDING}}}, self._item()) is None


class TestPackOneRefusesBadOcr:
    def _book(self, tmp_path, engine):
        return extract_pdf_ocr(_image_scan_pdf(tmp_path / "scan.pdf"), "scan-1",
                               "A Scan", engine=engine)

    def test_a_noise_read_is_marked_failed_and_uploads_nothing(self, monkeypatch, tmp_path):
        from app.tools import ingest_books as ib

        book, _ = self._book(tmp_path, _stub_engine([[("x", 0.1)], [("y", 0.1)]]))
        monkeypatch.setattr(ib, "extract_for", lambda *a, **k: book)

        def _no_upload(*a, **k):
            raise AssertionError("a refused OCR read must upload nothing")

        monkeypatch.setattr(ib, "upload_pack", _no_upload)
        monkeypatch.setattr(ib, "PACKS_DIR", tmp_path / "packs")
        state = {"books": {}}
        item = QueueItem("scan-1", "A Scan", TIER_NEEDS_OCR, SOURCE_PDF_OCR, "x.pdf")
        assert ib.pack_one(item, {}, state) is None
        entry = state["books"]["scan-1"]
        assert entry["status"] == STATUS_FAILED
        assert entry["reason"].startswith("OCR quality gate:")
        # ⚠️ The numbers are ON the row, so a future session can see WHY without
        # re-running the read.
        assert entry["ocr_pages"] == 2 and "ocr_mean_confidence" in entry

    def test_a_good_read_records_the_measurement_beside_the_pack(self, monkeypatch, tmp_path):
        from app.tools import ingest_books as ib

        book, _ = self._book(tmp_path, _stub_engine([_prose("alpha"), _prose("beta")]))
        monkeypatch.setattr(ib, "extract_for", lambda *a, **k: book)
        monkeypatch.setattr(ib, "upload_pack", lambda gz, bid: f"text/{bid}.json.gz")
        monkeypatch.setattr(ib, "PACKS_DIR", tmp_path / "packs")
        state = {"books": {}}
        item = QueueItem("scan-1", "A Scan", TIER_NEEDS_OCR, SOURCE_PDF_OCR, "x.pdf")
        stats = ib.pack_one(item, {}, state)
        assert stats and stats["chunks"] > 0
        entry = state["books"]["scan-1"]
        assert entry["status"] == STATUS_DONE
        assert entry["source"] == "pdf-ocr"
        assert entry["ocr_pages"] == 2
        assert entry["ocr_mean_confidence"] >= 0.9


class TestRequeueOcr:
    """`--requeue-ocr` rides `apply_requeue`, so `done` stays untouchable and the
    previous status/reason survive - one primitive owns every move to pending."""

    def _state(self):
        return {"version": 1, "books": {
            "scan-a": {"status": STATUS_NEEDS_OCR, "title": "Scan A",
                       "blocker": "OCR processor not built",
                       "reason": "image-scan: 0 chars total"},
            "scan-b": {"status": STATUS_NEEDS_OCR, "title": "Scan B"},
            "finished": {"status": STATUS_DONE, "title": "Done Book"},
            "already": {"status": STATUS_PENDING, "title": "Pending Book"},
        }}

    def _patched(self, monkeypatch, saved, engine=(True, "stub-engine")):
        from app.tools import ingest_books as ib

        monkeypatch.setattr(ib, "ocr_available", lambda: engine)
        monkeypatch.setattr(ib, "load_state", lambda *a, **k: self._state())
        monkeypatch.setattr(ib, "save_state", lambda s, *a, **k: saved.append(s))
        return ib

    def test_it_arms_every_needs_ocr_book(self, monkeypatch):
        saved = []
        out = self._patched(monkeypatch, saved).requeue_ocr()
        assert sorted(out["requeued"]) == ["scan-a", "scan-b"]
        assert saved and saved[0]["books"]["scan-a"]["status"] == STATUS_PENDING

    def test_a_done_book_is_never_armed(self, monkeypatch):
        saved = []
        out = self._patched(monkeypatch, saved).requeue_ocr()
        assert "finished" not in out["requeued"]
        assert saved[0]["books"]["finished"]["status"] == STATUS_DONE

    def test_the_previous_status_and_reason_survive(self, monkeypatch):
        saved = []
        self._patched(monkeypatch, saved).requeue_ocr()
        entry = saved[0]["books"]["scan-a"]
        assert entry["previous_status"] == STATUS_NEEDS_OCR
        assert entry["previous_reason"] == "image-scan: 0 chars total"

    def test_dry_run_writes_nothing(self, monkeypatch):
        saved = []
        out = self._patched(monkeypatch, saved).requeue_ocr(dry_run=True)
        assert sorted(out["requeued"]) == ["scan-a", "scan-b"]
        assert saved == [], "--dry-run must not write the state file"

    def test_named_books_only(self, monkeypatch):
        saved = []
        out = self._patched(monkeypatch, saved).requeue_ocr(book_ids=["scan-b"])
        assert out["requeued"] == ["scan-b"]
        assert saved[0]["books"]["scan-a"]["status"] == STATUS_NEEDS_OCR

    def test_an_unknown_id_is_dropped_without_creating_a_phantom(self, monkeypatch):
        saved = []
        out = self._patched(monkeypatch, saved).requeue_ocr(book_ids=["no-such-book"])
        assert out["unknown"] == ["no-such-book"]
        assert saved == []

    def test_it_refuses_outright_when_no_engine_is_installed(self, monkeypatch):
        # ⚠️ Arming books for a lane that cannot read them turns a clear "held,
        # here is the blocker" row into a book that looks queued and never moves.
        saved = []
        ib = self._patched(monkeypatch, saved, engine=(False, "no OCR engine here"))
        out = ib.requeue_ocr()
        assert out["requeued"] == [] and out["refused"] == "no OCR engine here"
        assert saved == []

    def test_a_live_run_holding_the_lock_blocks_the_write(self, monkeypatch):
        from app.tools import ingest_books as ib

        applied = []
        monkeypatch.setattr(ib, "requeue_ocr", lambda **kw: applied.append(kw))

        class _Held:
            acquired = False

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(ib, "_Lock", _Held)
        assert ib.main(["--requeue-ocr"]) == 1
        assert applied == [], "nothing may be written while a run holds the lock"

    def test_dry_run_needs_no_lock_so_it_works_mid_run(self, monkeypatch):
        from app.tools import ingest_books as ib

        applied = []
        monkeypatch.setattr(ib, "requeue_ocr", lambda **kw: applied.append(kw))

        def _boom(*a, **k):
            raise AssertionError("--dry-run must not touch the lock")

        monkeypatch.setattr(ib, "_Lock", _boom)
        assert ib.main(["--requeue-ocr", "--dry-run"]) == 0
        assert applied == [{"dry_run": True, "book_ids": None}]


# --------------------------------------------------------------------------
# the real engine
# --------------------------------------------------------------------------

@needs_engine
class TestTheRealEngine:
    def test_it_reads_the_synthetic_scan_back(self, tmp_path):
        book, quality = extract_pdf_ocr(
            _image_scan_pdf(tmp_path / "scan.pdf", dpi=200), "b", "Book")
        text = book.full_text.lower()
        assert "tower" in text and "lamp" in text
        assert "stairs" in text
        assert quality.pages == 2 and quality.pages_with_text == 2
        assert quality.mean_confidence > OCR_MIN_MEAN_CONFIDENCE
        assert quality.garbage_ratio == 0.0

    def test_a_cover_only_scan_is_measured_as_one_and_refused(self, tmp_path):
        _, quality = extract_pdf_ocr(
            _image_scan_pdf(tmp_path / "cover.pdf", pages=("TOWER",), dpi=200),
            "b", "Book")
        assert quality_refusal(quality) is not None
