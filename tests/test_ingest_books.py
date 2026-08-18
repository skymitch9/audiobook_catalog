"""Tests for the nightly book-knowledge ingester.

Grouped by the thing that would break if the test were deleted:

  * chunker boundaries     - a persisted-key contract (every stored `ord`)
  * window math            - the owner's 12am-8am Phoenix promise, no DST
  * GPU-guard parsing      - the "don't start above 50%" clause, fail-safe
  * CPU-guard parsing      - the same fail-safe on the processor (2026-08-18)
  * the deadline gate      - "by 630 the ingestion is done" (2026-08-18)
  * control contract       - the dashboard pause the owner asked for
  * priority ordering      - "start with books that have reviews"
  * pack shape             - ingester_version, and the sha over content only
"""

from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core import ingest_control as ic
from app.core.book_chunker import (
    CHUNK_CHARS, CHUNK_OVERLAP, chunk_book, split_text,
)
from app.core.book_text import ExtractedBook, ExtractedChapter, clean_text
from app.core.ingest_pack import (
    INGESTER_VERSION, PackRefused, build_pack, content_digest, pack_stats,
    write_pack_gz,
)
from app.core.ingest_queue import (
    QueueItem, TIER_EPUB, TIER_NEEDS_OCR, TIER_PDF_TEXT, TIER_REST_AUDIO,
    TIER_REVIEWED_AUDIO, TIER_TWIN, _sort_key, build_twin_index,
    strip_series_boilerplate,
)

PHX = ic.PHOENIX

# ⚠️ Captured at import, BEFORE the autouse fixture below stubs it out. The
# tests that exercise the reader itself need the real function; every other
# test needs it stubbed, and there is no third option.
_real_recent_realtime_factors = ic.recent_realtime_factors


def phx(hour, minute=0, day=18):
    return datetime(2026, 8, day, hour, minute, tzinfo=PHX)


@pytest.fixture(autouse=True)
def _deterministic_machine(monkeypatch):
    """⚠️ NO TEST IN THIS FILE MAY READ THE REAL MACHINE.

    Two of the five gates measure the box they run on, and both were added
    2026-08-18. Without this fixture every `decide_start` test would shell out
    to `typeperf` (1.2 s each, and absent on the POSIX CI runner, where an
    unreadable CPU correctly means BUSY and would fail every start test for the
    right reason at the wrong time) and would read whatever transcripts happen
    to be on this developer's disk. Both are stubbed to a quiet, known machine;
    tests that care about a busy or unreadable one override these.
    """
    monkeypatch.setattr(ic, "cpu_utilisation", lambda *a, **k: 5.0)
    monkeypatch.setattr(ic, "recent_realtime_factors", lambda *a, **k: [50.0])


def book(*chapters):
    return ExtractedBook(book_id="b", title="B", source="epub", chapters=list(chapters))


# ---------------------------------------------------------------- chunker ---

class TestChunker:
    def test_short_text_is_one_chunk(self):
        assert split_text("hello world") == ["hello world"]

    def test_empty_text_yields_nothing(self):
        assert split_text("") == []
        assert split_text("   \n  ") == []

    def test_chunks_respect_the_size_ceiling(self):
        text = " ".join(f"word{i}" for i in range(4000))
        for piece in split_text(text):
            assert len(piece) <= CHUNK_CHARS

    def test_cuts_land_on_word_boundaries(self):
        """⚠️ A mid-word cut breaks the lexical path this design makes primary:
        `Willpower` sliced into `Willpo` + `wer` matches no detector."""
        text = " ".join(f"token{i:04d}" for i in range(2000))
        for piece in split_text(text):
            assert not piece.startswith("oken")
            for token in piece.split():
                assert token.startswith("token"), f"word split across a chunk: {token!r}"

    def test_overlap_is_real(self):
        text = " ".join(f"w{i:05d}" for i in range(2000))
        pieces = split_text(text, size=800, overlap=100)
        assert len(pieces) > 2
        # Consecutive chunks must share text, or the +/-1 stitch has nothing to
        # de-overlap and a block spanning a boundary is lost.
        first_words = set(pieces[0].split())
        assert first_words & set(pieces[1].split())

    def test_pathological_single_token_terminates(self):
        """One unbroken 10k-char token must not loop forever."""
        pieces = split_text("x" * 10_000)
        assert pieces and sum(len(p) for p in pieces) >= 10_000 - len(pieces) * CHUNK_OVERLAP

    def test_rejects_impossible_parameters(self):
        with pytest.raises(ValueError):
            split_text("abc", size=0)
        with pytest.raises(ValueError):
            split_text("abc", size=100, overlap=100)

    def test_no_chunk_spans_a_chapter_boundary(self):
        """⚠️ The load-bearing invariant. A straddling chunk cannot be scoped
        (it belongs to two chapters) and cannot be cited."""
        a = ExtractedChapter(0, "One", "alpha " * 400, spine_index=0)
        b = ExtractedChapter(1, "Two", "bravo " * 400, spine_index=1)
        chunks, refs = chunk_book(book(a, b))
        for chunk in chunks:
            assert not ("alpha" in chunk.text and "bravo" in chunk.text)
        assert {c.chapter_index for c in chunks} == {0, 1}
        assert len(refs) == 2

    def test_ords_are_dense_and_ordered(self):
        chunks, _ = chunk_book(book(
            ExtractedChapter(0, "One", "alpha " * 300),
            ExtractedChapter(1, "Two", "bravo " * 300),
        ))
        assert [c.ord for c in chunks] == list(range(len(chunks)))

    def test_chapter_refs_bound_their_chunks(self):
        chunks, refs = chunk_book(book(
            ExtractedChapter(0, "One", "alpha " * 300),
            ExtractedChapter(1, "Two", "bravo " * 300),
        ))
        for ref in refs:
            span = [c for c in chunks if ref.first_chunk <= c.ord <= ref.last_chunk]
            assert span and all(c.chapter_index == ref.index for c in span)

    def test_persisted_key_constants_are_the_measured_ones(self):
        """⚠️ 800/100 is a MIGRATION to change (design section 7.3.1), not a
        tuning knob. If this fails, every stored ord and every reader's spoiler
        ceiling changed meaning."""
        assert (CHUNK_CHARS, CHUNK_OVERLAP) == (800, 100)

    def test_transcript_chunks_carry_real_timestamps(self):
        words = [{"w": f"w{i} ", "s": float(i), "e": float(i) + 0.9} for i in range(600)]
        chapter = ExtractedChapter(0, "Ch1", "".join(w["w"] for w in words),
                                   start_sec=0.0, end_sec=600.0, words=words)
        chunks, _ = chunk_book(ExtractedBook("b", "B", "transcript", [chapter]))
        assert all(c.start_sec is not None for c in chunks)
        assert chunks[0].start_sec <= chunks[-1].start_sec


class TestCleanText:
    def test_preserves_single_newlines(self):
        """Stat blocks are runs of short lines; flattening them destroys the one
        structural signal the LitRPG question class depends on."""
        assert clean_text("Strength: 7\nAgility: 8") == "Strength: 7\nAgility: 8"

    def test_caps_blank_line_runs(self):
        assert clean_text("a\n\n\n\n\nb") == "a\n\nb"


# ----------------------------------------------------------------- window ---

class TestWindow:
    def test_phoenix_has_no_dst(self):
        """⚠️ The whole window rests on this. Arizona does not shift, so a fixed
        -07:00 is correct; a zone that shifted would need a tz database."""
        assert ic.PHOENIX.utcoffset(None) == timedelta(hours=-7)
        january = datetime(2026, 1, 15, 12, tzinfo=ic.PHOENIX)
        july = datetime(2026, 7, 15, 12, tzinfo=ic.PHOENIX)
        assert january.utcoffset() == july.utcoffset() == timedelta(hours=-7)

    @pytest.mark.parametrize("hour,inside", [
        (0, True), (1, True), (3, True), (7, True),
        (8, False), (9, False), (12, False), (19, False), (23, False),
    ])
    def test_window_hours(self, hour, inside):
        assert ic.in_window(phx(hour)) is inside

    def test_no_new_starts_after_0745(self):
        assert ic.may_start_new_book(phx(7, 44)) is True
        assert ic.may_start_new_book(phx(7, 45)) is False
        assert ic.may_start_new_book(phx(7, 59)) is False

    def test_0745_cutoff_does_not_close_the_window(self):
        """A book already running at 07:45 finishes; only NEW starts stop."""
        assert ic.in_window(phx(7, 50)) is True
        assert ic.may_start_new_book(phx(7, 50)) is False

    def test_batch_16_only_inside_the_window(self):
        assert ic.batch_size_for(phx(2)) == 16
        assert ic.batch_size_for(phx(14)) == 8

    def test_utc_conversion_is_explicit(self):
        """19:00 UTC is 12:00 Phoenix - outside the window. A naive local read on
        a re-homed machine would get this wrong."""
        utc_now = datetime(2026, 8, 18, 19, 0, tzinfo=timezone.utc)
        assert ic.in_window(utc_now.astimezone(ic.PHOENIX)) is False

    def test_seconds_until_open_is_never_negative(self):
        for hour in range(24):
            assert ic.seconds_until_window_open(phx(hour)) >= 0


# -------------------------------------------------------------- GPU guard ---

class TestGpuGuard:
    def test_parses_single_gpu(self):
        assert ic.parse_gpu_utilisation("42\n") == 42

    def test_takes_the_max_across_gpus(self):
        assert ic.parse_gpu_utilisation("12\n88\n3\n") == 88

    def test_tolerates_units_and_padding(self):
        assert ic.parse_gpu_utilisation(" 37 %, 1024 \n") == 37

    def test_unparseable_is_none_not_zero(self):
        """⚠️ The fail-safe. None must never read as 'idle, go ahead'."""
        assert ic.parse_gpu_utilisation("") is None
        assert ic.parse_gpu_utilisation("N/A\n") is None
        assert ic.parse_gpu_utilisation("no devices found") is None

    def test_unknown_utilisation_blocks_a_start(self, monkeypatch):
        monkeypatch.setattr(ic, "gpu_utilisation", lambda *a, **k: None)
        assert ic.gpu_is_free() is False

    @pytest.mark.parametrize("pct,free", [(0, True), (49, True), (50, True), (51, False), (99, False)])
    def test_threshold_is_fifty(self, monkeypatch, pct, free):
        monkeypatch.setattr(ic, "gpu_utilisation", lambda *a, **k: pct)
        assert ic.gpu_is_free() is free

    def test_sustained_free_needs_every_poll(self, monkeypatch):
        readings = iter([10, 90])
        monkeypatch.setattr(ic, "gpu_utilisation", lambda *a, **k: next(readings))
        assert ic.gpu_sustained_free(polls=2, sleep=lambda _s: None) is False

    def test_sustained_free_passes_when_all_polls_are_free(self, monkeypatch):
        readings = iter([10, 12])
        monkeypatch.setattr(ic, "gpu_utilisation", lambda *a, **k: next(readings))
        assert ic.gpu_sustained_free(polls=2, sleep=lambda _s: None) is True


# -------------------------------------------------------------- CPU guard ---

class TestCpuGuardParsing:
    """The readers. ⚠️ psutil is NOT installed on the interpreter the nightly
    task uses (measured 2026-08-18), so these two parsers are the guard."""

    TYPEPERF = ('\n'
                '"(PDH-CSV 4.0)","\\\\SKYFI-ELIZA\\Processor(_Total)\\% Processor Time"\n'
                '"08/18/2026 14:15:28.791","11.466608"\n'
                'Exiting, please wait...\n'
                'The command completed successfully.\n')

    def test_parses_a_real_typeperf_sample(self):
        assert ic.parse_typeperf_cpu(self.TYPEPERF) == pytest.approx(11.4666, abs=1e-3)

    def test_the_header_row_is_not_a_reading(self):
        """Its last field is the counter's NAME. If that ever parsed as a
        number the guard would read a machine that does not exist."""
        header_only = ('"(PDH-CSV 4.0)","\\\\HOST\\Processor(_Total)\\% Processor Time"\n')
        assert ic.parse_typeperf_cpu(header_only) is None

    def test_takes_the_max_across_samples(self):
        raw = ('"(PDH-CSV 4.0)","\\\\H\\Processor(_Total)\\% Processor Time"\n'
               '"08/18/2026 14:15:28.791","9.0"\n'
               '"08/18/2026 14:15:29.791","91.5"\n')
        assert ic.parse_typeperf_cpu(raw) == 91.5

    @pytest.mark.parametrize("raw", ["", "Error: the counter is not valid.\n",
                                     '"(PDH-CSV 4.0)"\n', '"a","not-a-number"\n'])
    def test_unparseable_is_none_not_zero(self, raw):
        """⚠️ The fail-safe, same as the GPU parser's. None must never read as
        'the processor is idle, go ahead'."""
        assert ic.parse_typeperf_cpu(raw) is None

    def test_non_finite_counters_are_refused(self):
        assert ic.parse_typeperf_cpu('"a","nan"\n') is None
        assert ic.parse_typeperf_cpu('"a","inf"\n') is None
        assert ic.parse_typeperf_cpu('"a","-3"\n') is None

    def test_parses_wmic_and_takes_the_max_socket(self):
        assert ic.parse_wmic_cpu("\nLoadPercentage=57\n\n") == 57.0
        assert ic.parse_wmic_cpu("LoadPercentage=3\nLoadPercentage=88\n") == 88.0

    def test_wmic_without_the_field_is_none(self):
        assert ic.parse_wmic_cpu("Invalid GET Expression.") is None


class TestCpuGuard:
    def test_one_free_poll_is_enough_to_allow(self, monkeypatch):
        """The night's common case must be cheap: 138 EPUB starts cannot each
        pay 30 seconds to be told the machine is idle."""
        slept = []
        monkeypatch.setattr(ic, "cpu_utilisation", lambda *a, **k: 4.0)
        reading = ic.cpu_guard(sleep=slept.append)
        assert reading.busy is False and reading.polls == 1 and slept == []

    def test_a_refusal_needs_two_busy_polls(self, monkeypatch):
        readings = iter([99.0, 98.0])
        monkeypatch.setattr(ic, "cpu_utilisation", lambda *a, **k: next(readings))
        reading = ic.cpu_guard(sleep=lambda _s: None)
        assert reading.busy is True and reading.polls == 2
        assert reading.waited_s == ic.CPU_POLL_SECONDS

    def test_our_own_finished_burst_does_not_block_the_next_book(self, monkeypatch):
        """⚠️ THE SUBTLETY THE GPU GUARD DOES NOT HAVE. This gate is asked its
        question the instant after the ingester packed, gzipped and uploaded the
        previous book. A single sample there measures OUR OWN exhaust; a second
        poll 30 s later does not, because that burst is over in seconds."""
        readings = iter([96.0, 8.0])
        monkeypatch.setattr(ic, "cpu_utilisation", lambda *a, **k: next(readings))
        reading = ic.cpu_guard(sleep=lambda _s: None)
        assert reading.busy is False and reading.pct == 8.0

    @pytest.mark.parametrize("pct,busy", [(0, False), (74, False), (75, False),
                                          (76, True), (100, True)])
    def test_the_threshold_is_seventy_five(self, monkeypatch, pct, busy):
        monkeypatch.setattr(ic, "cpu_utilisation", lambda *a, **k: float(pct))
        assert ic.cpu_guard(sleep=lambda _s: None).busy is busy

    def test_unreadable_counts_as_busy(self, monkeypatch):
        """⚠️ A machine with no typeperf, no wmic and no psutil must refuse to
        start, not assume an idle processor."""
        monkeypatch.setattr(ic, "cpu_utilisation", lambda *a, **k: None)
        reading = ic.cpu_guard(sleep=lambda _s: None)
        assert reading.busy is True and reading.pct is None
        assert "unreadable" in ic.cpu_busy_words(reading)
        assert "BUSY" in ic.cpu_busy_words(reading)

    def test_a_busy_cpu_blocks_a_start_with_words(self, monkeypatch):
        monkeypatch.setattr(ic, "gpu_utilisation", lambda *a, **k: 2)
        monkeypatch.setattr(ic, "cpu_utilisation", lambda *a, **k: 93.0)
        decision = ic.decide_start(ic.ControlState(), now=phx(2), needs_gpu=True,
                                   sleep=lambda _s: None, audio_seconds=3600)
        assert decision.may_start is False
        assert "93%" in decision.reason and decision.cpu_pct == 93.0

    def test_epub_work_is_gpu_exempt_but_not_cpu_exempt(self, monkeypatch):
        """The conversion phase is not the only CPU-heavy thing here - parsing
        138 EPUBs is the processor's work too, and it competes with a person."""
        monkeypatch.setattr(ic, "cpu_utilisation", lambda *a, **k: 91.0)
        decision = ic.decide_start(ic.ControlState(), now=phx(2), needs_gpu=False,
                                   sleep=lambda _s: None)
        assert decision.may_start is False and "91%" in decision.reason

    def test_the_gpu_is_polled_before_the_cpu(self, monkeypatch):
        """Order is cost: nvidia-smi is one fast subprocess, the CPU guard can
        sleep 30 s. A refusal the GPU can make must not pay for the CPU's."""
        monkeypatch.setattr(ic, "gpu_utilisation", lambda *a, **k: 99)

        def explode(*a, **k):
            raise AssertionError("the CPU must not be polled after the GPU refused")

        monkeypatch.setattr(ic, "cpu_utilisation", explode)
        assert ic.decide_start(ic.ControlState(), now=phx(2), needs_gpu=True,
                               audio_seconds=3600).may_start is False


# --------------------------------------------------------------- deadline ---

class TestRealtimeFactor:
    """⚠️ Every number here leans one way: OVER-ESTIMATE the book. An
    over-estimate costs a start that would have fitted; an under-estimate runs
    the GPU through the hour the owner reserved."""

    def test_the_statistic_is_the_worst_recent_book_not_the_mean(self):
        """A night that started at 85x and is now doing 32x is a 32x machine.
        A mean would say 58x and start a book on a number that stopped being
        true an hour ago."""
        factor, words = ic.realtime_factor([85.0, 80.0, 32.0])
        assert factor == pytest.approx(32.0 / ic.RT_SAFETY_DIVISOR)
        assert "32.0x" in words

    def test_a_clean_stretch_cannot_buy_optimism_past_the_ceiling(self):
        factor, _ = ic.realtime_factor([85.4, 85.3, 84.8])
        assert factor == ic.RT_CEILING

    def test_one_catastrophic_outlier_cannot_stall_the_queue_forever(self):
        factor, _ = ic.realtime_factor([2.0])
        assert factor == ic.RT_FLOOR

    def test_no_receipts_yet_is_pessimistic_but_usable(self):
        factor, words = ic.realtime_factor([])
        assert factor == ic.RT_DEFAULT and "no completed transcripts" in words

    def test_the_safety_margin_always_makes_the_book_slower(self):
        for measured in (31.8, 45.7, 55.3, 59.8):
            factor, _ = ic.realtime_factor([measured])
            assert factor < measured

    def test_factors_are_read_from_the_transcript_head_newest_run_first(self, tmp_path):
        """⚠️ `meta` is a transcript's FIRST key and the file is ~13 MB, so this
        reads 4 KB. It also orders by the RECORDED run_utc, not by mtime - mtime
        only chooses which files to open."""
        for stem, run, factor in (("old", "2026-08-18T10:00:00Z", 80.0),
                                  ("new", "2026-08-18T20:00:00Z", 31.8)):
            payload = json.dumps({"meta": {"run_utc": run, "realtime_factor": factor},
                                  "segments": [{"x": 1}] * 5000})
            (tmp_path / f"{stem}.json").write_text(payload, encoding="utf-8")
        assert _real_recent_realtime_factors(limit=1, directory=tmp_path) == [31.8]
        assert _real_recent_realtime_factors(directory=tmp_path) == [31.8, 80.0]

    def test_a_corrupt_or_empty_transcript_dir_is_no_samples_not_a_crash(self, tmp_path):
        (tmp_path / "torn.json").write_text("{not json at all", encoding="utf-8")
        assert _real_recent_realtime_factors(directory=tmp_path) == []
        assert _real_recent_realtime_factors(directory=tmp_path / "nope") == []


class TestDeadlineGate:
    """Owner, 2026-08-18: *"if 630 hits and its about to start a new book, dont,
    if 630 hits and its almost done with a book finish, if it'll finish before 7
    finish"*. The soft line stops new starts; the estimate decides whether a
    start may spend the buffer between the soft line and the hard stop."""

    EVENING = ic.ControlState(pause_windows=[
        {"from": "2026-08-18T19:00:00-07:00", "until": "2026-08-19T00:00:00-07:00",
         "reason": "owner: evening reserved"}])

    def test_a_book_that_fits_before_the_pause_window_starts(self):
        # 18:00, a 6 h book at 38x + 8 min = ~17 min -> ~18:17, before 19:00.
        estimate = ic.estimate_against_deadline(
            self.EVENING, phx(18), audio_seconds=6 * 3600, factors=[50.0])
        assert estimate.fits is True and "OK" in estimate.words

    def test_a_book_that_would_run_past_the_window_is_refused_with_words(self):
        estimate = ic.estimate_against_deadline(
            self.EVENING, phx(18, 30), audio_seconds=30 * 3600, factors=[50.0])
        assert estimate.fits is False
        assert "would finish" in estimate.words and "19:00" in estimate.words
        assert "holding" in estimate.words

    def test_the_boundary_is_the_window_close_not_the_no_new_starts_line(self):
        """⚠️ 07:45 stops new starts; 08:00 is what a finish is measured
        against. Conflating them would throw away the tolerance the owner
        explicitly told us to spend."""
        boundary, label = ic.next_boundary(ic.ControlState(), phx(6))
        assert boundary == phx(8) and "window close" in label

    def test_outside_the_window_with_no_pause_ahead_there_is_no_boundary(self):
        boundary, label = ic.next_boundary(ic.ControlState(), phx(14))
        assert boundary is None and "no boundary" in label

    def test_the_nearest_boundary_wins(self):
        state = ic.ControlState(pause_windows=[
            {"from": "2026-08-18T06:00:00-07:00", "until": "2026-08-18T07:00:00-07:00"}])
        boundary, _ = ic.next_boundary(state, phx(5))
        assert boundary == phx(6), "a 06:00 pause beats the 08:00 window close"

    def test_paused_until_is_not_a_boundary(self):
        """⚠️ It is when a pause ENDS. Treating it as a deadline would refuse
        every start for a reason that has already expired."""
        state = ic.ControlState(paused_until="2026-08-18T03:00:00-07:00")
        boundary, _ = ic.next_boundary(state, phx(4))
        assert boundary == phx(8)

    @pytest.mark.parametrize("hour,minute,fits", [
        (2, 0, True), (6, 0, True), (7, 0, True), (7, 33, True),
        (7, 34, False), (7, 44, False),
    ])
    def test_the_boundary_closes_in_as_the_window_ends(self, hour, minute, fits):
        """A 12.1 h book (the shelf's measured median) at the stubbed 38.5x plus
        8 minutes of overhead is 26.9 minutes: it still fits at 07:33 and lands
        past 08:00 from 07:34 on."""
        estimate = ic.estimate_against_deadline(
            ic.ControlState(), phx(hour, minute),
            audio_seconds=12.1 * 3600, factors=[50.0])
        assert estimate.fits is fits

    def test_an_unknown_runtime_is_assumed_long_not_zero(self):
        """⚠️ A book with no duration must not look instantaneous and start at
        07:44. All 1,079 catalog rows parse today, so this is the contingency."""
        estimate = ic.estimate_against_deadline(
            ic.ControlState(), phx(7, 30), audio_seconds=None, factors=[50.0])
        assert estimate.fits is False and "unknown" in estimate.words

        early = ic.estimate_against_deadline(
            ic.ControlState(), phx(1), audio_seconds=None, factors=[50.0])
        assert early.fits is True, "an unknown runtime must not stall the whole night"

    @pytest.mark.parametrize("bad", [0, -1, None])
    def test_a_zero_or_negative_runtime_is_treated_as_unknown(self, bad):
        estimate = ic.estimate_against_deadline(
            ic.ControlState(), phx(7, 30), audio_seconds=bad, factors=[50.0])
        assert estimate.fits is False

    def test_overhead_is_counted_on_top_of_transcription(self):
        estimate = ic.estimate_against_deadline(
            ic.ControlState(), phx(2), audio_seconds=3600, factors=[50.0])
        assert estimate.est_seconds == pytest.approx(
            3600 / estimate.factor + ic.OVERHEAD_SECONDS)

    def test_the_deadline_refuses_before_any_subprocess_runs(self, monkeypatch):
        """It is pure arithmetic, so it must not cost an nvidia-smi."""
        def explode(*a, **k):
            raise AssertionError("no machine poll may happen after a deadline refusal")

        monkeypatch.setattr(ic, "gpu_utilisation", explode)
        monkeypatch.setattr(ic, "cpu_utilisation", explode)
        decision = ic.decide_start(ic.ControlState(), now=phx(7, 40), needs_gpu=True,
                                   audio_seconds=20 * 3600, factors=[50.0])
        assert decision.may_start is False and "holding" in decision.reason

    def test_cpu_only_work_is_not_deadline_gated(self, monkeypatch):
        """EPUB extraction is seconds. Estimating it would refuse a job that
        cannot possibly overrun."""
        monkeypatch.setattr(ic, "cpu_utilisation", lambda *a, **k: 5.0)
        decision = ic.decide_start(ic.ControlState(), now=phx(7, 44), needs_gpu=False,
                                   sleep=lambda _s: None)
        assert decision.may_start is True

    def test_a_guard_wait_that_outlives_the_boundary_gives_up(self, monkeypatch):
        """⚠️ THE HOLE THE RE-CHECK CLOSES. Two GPU polls two minutes apart plus
        a CPU confirm is 4.5 minutes; a start cleared at 07:29 must not begin at
        07:33 once the estimate no longer fits."""
        monkeypatch.setattr(ic, "gpu_utilisation", lambda *a, **k: 3)
        readings = iter([99.0, 9.0])
        monkeypatch.setattr(ic, "cpu_utilisation", lambda *a, **k: next(readings))
        state = ic.ControlState(pause_windows=[
            {"from": "2026-08-18T02:30:00-07:00", "until": "2026-08-18T03:00:00-07:00"}])
        # The boundary is 1,800 s away. Size the book so the estimate is exactly
        # 1,790 s: it fits at 02:00 by ten seconds, and does not once the CPU
        # guard has spent 30 s confirming its first reading was our own burst.
        factor = 50.0 / ic.RT_SAFETY_DIVISOR
        decision = ic.decide_start(state, now=phx(2, 0, day=18), needs_gpu=True,
                                   audio_seconds=(1790 - ic.OVERHEAD_SECONDS) * factor,
                                   sleep=lambda _s: None, factors=[50.0])
        assert decision.may_start is False
        assert "guard waiting" in decision.reason

    def test_an_allowed_start_says_when_it_expects_to_finish(self, monkeypatch):
        monkeypatch.setattr(ic, "gpu_utilisation", lambda *a, **k: 3)
        decision = ic.decide_start(ic.ControlState(), now=phx(2), needs_gpu=True,
                                   audio_seconds=6 * 3600, factors=[50.0],
                                   sleep=lambda _s: None)
        assert decision.may_start is True
        assert "would finish" in decision.reason and decision.est_finish is not None
        assert decision.boundary == phx(8)


class TestDurationSource:
    def test_hhmm_parses_to_seconds(self):
        from app.core.ingest_queue import parse_duration_hhmm

        assert parse_duration_hhmm("10:07") == 36420.0
        assert parse_duration_hhmm("1:02:03") == 3723.0

    @pytest.mark.parametrize("bad", [None, "", "  ", "unknown", "10", "10:7", "abc:def"])
    def test_an_unparseable_runtime_is_none_not_zero(self, bad):
        """⚠️ None means UNKNOWN and the deadline gate assumes a long book.
        Zero would mean instantaneous, and would start at 07:44."""
        from app.core.ingest_queue import parse_duration_hhmm

        assert parse_duration_hhmm(bad) is None

    def test_zero_runtime_is_unknown_too(self):
        from app.core.ingest_queue import parse_duration_hhmm

        assert parse_duration_hhmm("0:00") is None


# ---------------------------------------------------------------- control ---

class TestControlContract:
    def test_absent_control_is_permissive(self):
        assert ic.control_blocks_start(ic.ControlState(), phx(2)) is None

    def test_paused_blocks_with_words(self):
        reason = ic.control_blocks_start(ic.ControlState(paused=True), phx(2))
        assert reason and "paused" in reason.lower()

    def test_unreadable_control_is_treated_as_paused(self):
        """⚠️ Fail safe. The owner's stop must not depend on the network."""
        state = ic.ControlState(readable=False, error="timeout")
        reason = ic.control_blocks_start(state, phx(2))
        assert reason and "PAUSED" in reason

    def test_paused_until_expires(self):
        state = ic.ControlState(paused_until="2026-08-19T00:00:00-07:00")
        assert ic.control_blocks_start(state, phx(20)) is not None
        assert ic.control_blocks_start(state, phx(1, day=19)) is None

    def test_naive_timestamps_are_read_as_phoenix(self):
        """⚠️ Reading a bare '19:00' as UTC would start the pause 7 hours early."""
        parsed = ic.parse_iso("2026-08-18T19:00")
        assert parsed.utcoffset() == timedelta(hours=-7)

    def test_unparseable_timestamp_is_no_constraint_not_now(self):
        assert ic.parse_iso("not a date") is None
        assert ic.control_blocks_start(ic.ControlState(paused_until="garbage"), phx(2)) is None

    def test_pause_window_blocks_only_inside_it(self):
        state = ic.ControlState(pause_windows=[
            {"from": "2026-08-18T19:00:00-07:00", "until": "2026-08-19T00:00:00-07:00"}])
        assert ic.control_blocks_start(state, phx(18)) is None      # before
        assert ic.control_blocks_start(state, phx(20)) is not None  # inside
        assert ic.control_blocks_start(state, phx(1, day=19)) is None  # after

    def test_dont_check_is_separate_from_pause(self):
        """The owner asked for both: 'don't even check to start until x time'."""
        state = ic.ControlState(dont_check_until="2026-08-19T00:00:00-07:00")
        assert ic.control_defers_check(state, phx(20)) is not None
        assert ic.control_blocks_start(state, phx(20)) is None

    def test_dont_check_short_circuits_before_the_gpu(self, monkeypatch):
        def explode(*a, **k):
            raise AssertionError("GPU must not be polled while dont_check_until is live")

        monkeypatch.setattr(ic, "gpu_utilisation", explode)
        state = ic.ControlState(dont_check_until="2026-08-19T00:00:00-07:00")
        decision = ic.decide_start(state, now=phx(20), needs_gpu=True)
        assert decision.may_start is False


class TestDecideStart:
    def test_cpu_work_is_guard_exempt_but_window_bound(self, monkeypatch):
        monkeypatch.setattr(ic, "gpu_utilisation", lambda *a, **k: 99)
        assert ic.decide_start(ic.ControlState(), now=phx(2), needs_gpu=False).may_start is True
        assert ic.decide_start(ic.ControlState(), now=phx(14), needs_gpu=False).may_start is False

    def test_in_window_busy_gpu_refuses(self, monkeypatch):
        monkeypatch.setattr(ic, "gpu_utilisation", lambda *a, **k: 80)
        decision = ic.decide_start(ic.ControlState(), now=phx(2), needs_gpu=True)
        assert decision.may_start is False and "80%" in decision.reason

    def test_in_window_idle_gpu_starts_at_batch_16(self, monkeypatch):
        monkeypatch.setattr(ic, "gpu_utilisation", lambda *a, **k: 4)
        decision = ic.decide_start(ic.ControlState(), now=phx(2), needs_gpu=True)
        assert decision.may_start is True and decision.batch_size == 16

    def test_opportunistic_daytime_uses_batch_8(self, monkeypatch):
        monkeypatch.setattr(ic, "gpu_utilisation", lambda *a, **k: 5)
        decision = ic.decide_start(ic.ControlState(), now=phx(14), needs_gpu=True,
                                   sleep=lambda _s: None)
        assert decision.may_start is True
        assert decision.opportunistic is True and decision.batch_size == 8

    def test_opportunistic_can_be_disabled(self, monkeypatch):
        monkeypatch.setattr(ic, "gpu_utilisation", lambda *a, **k: 1)
        decision = ic.decide_start(ic.ControlState(), now=phx(14), needs_gpu=True,
                                   allow_opportunistic=False)
        assert decision.may_start is False

    def test_pause_beats_an_idle_gpu_in_the_window(self, monkeypatch):
        monkeypatch.setattr(ic, "gpu_utilisation", lambda *a, **k: 0)
        decision = ic.decide_start(ic.ControlState(paused=True), now=phx(2), needs_gpu=True)
        assert decision.may_start is False


# --------------------------------------------------------------- priority ---

def item(**kw):
    base = dict(book_id="b", title="T", tier=TIER_REVIEWED_AUDIO, source="transcript")
    base.update(kw)
    return QueueItem(**base)


class TestPriority:
    def test_tiers_sort_before_everything_else(self):
        items = [item(tier=TIER_NEEDS_OCR), item(tier=TIER_EPUB),
                 item(tier=TIER_REVIEWED_AUDIO), item(tier=TIER_PDF_TEXT),
                 item(tier=TIER_TWIN), item(tier=TIER_REST_AUDIO)]
        assert [i.tier for i in sorted(items, key=_sort_key)] == [1, 2, 3, 4, 5, 6]

    def test_reviewed_audio_orders_by_review_count_desc(self):
        items = [item(title="a", review_count=1), item(title="b", review_count=9),
                 item(title="c", review_count=4)]
        assert [i.title for i in sorted(items, key=_sort_key)] == ["b", "c", "a"]

    def test_rest_audio_orders_newest_first(self):
        items = [item(tier=TIER_REST_AUDIO, title="old", added_at="2019-01-01"),
                 item(tier=TIER_REST_AUDIO, title="new", added_at="2026-08-01")]
        assert [i.title for i in sorted(items, key=_sort_key)] == ["new", "old"]

    def test_unknown_added_date_sorts_last_not_first(self):
        """⚠️ Unknown must not masquerade as newest - that would silently
        promote every book the additions log has never seen."""
        items = [item(tier=TIER_REST_AUDIO, title="unknown", added_at=None),
                 item(tier=TIER_REST_AUDIO, title="known", added_at="2019-01-01")]
        assert [i.title for i in sorted(items, key=_sort_key)] == ["known", "unknown"]

    def test_needs_ocr_is_last(self):
        """Owner: complicated PDFs come after every reviewed audiobook."""
        items = [item(tier=TIER_NEEDS_OCR), item(tier=TIER_REST_AUDIO)]
        assert sorted(items, key=_sort_key)[-1].tier == TIER_NEEDS_OCR


class TestTwinJoin:
    def test_strips_series_boilerplate(self):
        assert strip_series_boilerplate("Fourth Wing - Empyrean, Book 1") == "fourth wing"
        assert strip_series_boilerplate("The Primal Hunter 2 - A LitRPG Adventure") \
            == "primal hunter 2"

    def test_keeps_a_subtitle_that_is_part_of_the_identity(self):
        """⚠️ The series-tail rule REQUIRES a ', Book N' marker precisely so it
        cannot eat a real subtitle. Losing this makes two different books join."""
        assert strip_series_boilerplate("Legion: The Many Lives of Stephen Leeds") \
            == "legion the many lives of stephen leeds"

    def test_joins_on_the_manifests_own_audiobook_title(self):
        index = build_twin_index([
            {"format": "epub", "title": "Tress", "audiobook_title": "Tress of the Emerald Sea",
             "path": "a/b.epub"}])
        assert "tress of the emerald sea" in index

    def test_ignores_pdfs(self):
        assert build_twin_index([{"format": "pdf", "title": "X", "path": "x.pdf"}]) == {}

    def test_refuses_a_degenerate_short_residue(self):
        """A title that strips to two characters must not become a join key that
        matches half the shelf."""
        index = build_twin_index([{"format": "epub", "title": "A Novel", "path": "x.epub"}])
        assert all(len(k) >= 4 for k in index)


# ------------------------------------------------------------- pack shape ---

def sample_book(source="epub"):
    return ExtractedBook("the-book", "The Book", source, [
        ExtractedChapter(0, "One", "alpha " * 300, spine_index=0),
        ExtractedChapter(1, "Two", "bravo " * 300, spine_index=1),
    ])


class TestPackShape:
    def test_every_pack_is_stamped(self):
        b = sample_book()
        chunks, refs = chunk_book(b)
        pack = build_pack(b, chunks, refs)
        assert pack["ingester_version"] == INGESTER_VERSION
        assert pack["source"] == "epub"
        assert pack["chunk_chars"] == CHUNK_CHARS
        assert pack["chunk_overlap"] == CHUNK_OVERLAP
        assert pack["book_id"] == "the-book"

    def test_source_records_which_pipeline_built_it(self):
        for source in ("epub", "pdf-text", "transcript"):
            b = sample_book(source)
            chunks, refs = chunk_book(b)
            assert build_pack(b, chunks, refs)["source"] == source

    def test_no_pack_stores_an_ord_ceiling(self):
        """⚠️ Design section 4.3: a ceiling is DERIVED every turn and never
        persisted, because an ord means different things at different chunk
        sizes and a carried ceiling leaks chapters silently."""
        b = sample_book()
        chunks, refs = chunk_book(b)
        blob = json.dumps(build_pack(b, chunks, refs))
        for forbidden in ("ceiling", "max_ord", "ord_ceiling", "visible_until"):
            assert forbidden not in blob

    def test_digest_is_over_content_not_the_artifact(self):
        """⚠️ The docs build re-PUT 1.2 MB forever because its hash covered a
        generated_at. Two packs of identical content must digest identically."""
        b = sample_book()
        chunks, _ = chunk_book(b)
        assert content_digest("x", "epub", chunks) == content_digest("x", "epub", chunks)

    def test_digest_changes_with_text(self):
        b = sample_book()
        chunks, _ = chunk_book(b)
        before = content_digest("x", "epub", chunks)
        chunks[0].text += " changed"
        assert content_digest("x", "epub", chunks) != before

    def test_digest_changes_with_ingester_version(self, monkeypatch):
        b = sample_book()
        chunks, _ = chunk_book(b)
        before = content_digest("x", "epub", chunks)
        monkeypatch.setattr("app.core.ingest_pack.INGESTER_VERSION", 99)
        assert content_digest("x", "epub", chunks) != before

    def test_oversized_book_is_refused_not_truncated(self):
        big = ExtractedBook("big", "Big", "transcript",
                            [ExtractedChapter(0, "One", "x " * 15_000_000)])
        chunks, refs = chunk_book(big)
        with pytest.raises(PackRefused):
            build_pack(big, chunks, refs)

    def test_transcript_packs_carry_a_per_book_alias_map(self):
        """⚠️ Per BOOK, never per series - `Villy` splits four ways in book 3."""
        b = ExtractedBook("t", "T", "transcript", [
            ExtractedChapter(0, "One", "Thane went with Thane and Thane again.")])
        chunks, refs = chunk_book(b)
        assert "alias_candidates" in build_pack(b, chunks, refs)

    def test_epub_packs_have_no_alias_map(self):
        b = sample_book("epub")
        chunks, refs = chunk_book(b)
        assert "alias_candidates" not in build_pack(b, chunks, refs)

    def test_gzip_round_trips(self, tmp_path):
        b = sample_book()
        chunks, refs = chunk_book(b)
        pack = build_pack(b, chunks, refs)
        path = write_pack_gz(pack, tmp_path)
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            assert json.load(fh)["book_id"] == "the-book"

    def test_identical_content_gzips_to_identical_bytes(self, tmp_path):
        """A gzip mtime would make the artifact differ every run and defeat the
        sha-skip the same way the docs build's did."""
        b = sample_book()
        chunks, refs = chunk_book(b)
        pack = build_pack(b, chunks, refs)
        pack.pop("ingested_at")
        a = write_pack_gz({**pack, "ingested_at": "fixed"}, tmp_path / "a")
        c = write_pack_gz({**pack, "ingested_at": "fixed"}, tmp_path / "b")
        assert a.read_bytes() == c.read_bytes()

    def test_stats_report_a_real_gzip_ratio(self, tmp_path):
        b = sample_book()
        chunks, refs = chunk_book(b)
        pack = build_pack(b, chunks, refs)
        stats = pack_stats(pack, write_pack_gz(pack, tmp_path))
        assert 0 < stats["gzip_ratio"] < 1
        assert stats["chunks"] == len(chunks)


class TestAliasMap:
    def test_sentence_initial_capitals_are_not_names(self):
        """⚠️ Measured on a real transcript: a naive scan returned `The` 1,419
        times and buried `Viper` at 224. English capitalises every sentence, so
        without this the candidate list is grammar, not characters."""
        from app.core.book_text import build_alias_map

        text = ("The hunter moved. The viper watched. But Jake saw Jake and "
                "Jake again. And then the Jake matter settled.")
        aliases = build_alias_map(
            ExtractedBook("b", "B", "transcript",
                          [ExtractedChapter(0, "One", text)]),
            min_count=2)
        assert "The" not in aliases and "But" not in aliases and "And" not in aliases
        assert aliases.get("Jake", 0) >= 2

    def test_counts_are_returned_sorted_desc(self):
        from app.core.book_text import build_alias_map

        text = "a Alpha b Alpha c Alpha d Bravo e Bravo f Charlie"
        aliases = build_alias_map(
            ExtractedBook("b", "B", "transcript",
                          [ExtractedChapter(0, "One", text)]), min_count=2)
        assert list(aliases) == ["Alpha", "Bravo"]


class TestTranscriptIndex:
    def test_source_is_read_from_the_file_head(self, tmp_path):
        """⚠️ A transcript is ~13 MB and `meta` is its first key. An earlier
        version json.load()ed every one per queue item — 1,200 × 9 full parses,
        which looked exactly like a hang."""
        from app.tools.ingest_books import _transcript_source

        path = tmp_path / "t.json"
        payload = ('{"meta": {"source_m4b": "C:\\\\books\\\\A Book.m4b"}, '
                   '"segments": [' + ",".join(['{"x":1}'] * 20000) + "]}")
        path.write_text(payload, encoding="utf-8")
        assert _transcript_source(path).endswith("A Book.m4b")

    def test_missing_file_returns_empty_not_an_exception(self, tmp_path):
        from app.tools.ingest_books import _transcript_source

        assert _transcript_source(tmp_path / "nope.json") == ""

    def test_non_ascii_path_survives_the_head_read(self, tmp_path):
        """⚠️ Regression, 2026-08-18 first daytime run: the head-read decoded
        the captured JSON string with `.encode().decode("unicode_escape")`,
        which mojibakes every non-ASCII character (UTF-8 bytes re-read as
        Latin-1). `Sorcerer's Stone`'s curly apostrophe became `â€™`, the
        normalised index key missed, and a freshly-transcribed book failed
        its own pack with "no transcript on disk"."""
        from app.core.review_join import normalise_title
        from app.tools.ingest_books import _transcript_source

        # ⚠️ PureWindowsPath, not Path: the stored path IS a Windows path, and
        # on a POSIX CI runner Path() would treat the backslashes as filename
        # characters — the exact platform-blindness that broke CI on 2026-08-18.
        from pathlib import PureWindowsPath

        path = tmp_path / "t.json"
        src = "C:\\books\\Harry Potter and the Sorcerer\u2019s Stone.m4b"
        payload = json.dumps(
            {"meta": {"source_m4b": src},
             "segments": [{"x": 1}] * 20000})
        path.write_text(payload, encoding="utf-8")
        got = _transcript_source(path)
        assert got == src
        assert normalise_title(PureWindowsPath(got).stem) == normalise_title(
            "Harry Potter and the Sorcerer's Stone")


class TestCustody:
    @pytest.mark.skipif(
        os.name != "nt",
        reason="guards a Windows path on the Windows machine; on a POSIX "
               "runner 'C:\\...' is a RELATIVE path that resolves under the "
               "repo and fails for the wrong reason (CI, 2026-08-18)")
    def test_training_data_lives_outside_every_repo(self):
        """⚠️ THE MECHANICAL GUARD. Transcripts are derived full text of books
        the household owns and this repo is PUBLIC. A path outside every
        repository cannot be committed by any command run inside one - which a
        .gitignore entry cannot promise."""
        from app.core.ingest_queue import PACKS_DIR, TRAINING_ROOT, TRANSCRIPTS_DIR

        repo = Path(__file__).resolve().parents[1]
        for path in (TRAINING_ROOT, TRANSCRIPTS_DIR, PACKS_DIR):
            assert repo not in path.resolve().parents
            assert path.resolve() != repo


# --------------------------------------------------------------------------
# the served index carries titles (the empty-title bug, 2026-08-18)
# --------------------------------------------------------------------------

class TestPackTitles:
    """`publish_index()` serves whatever `ingest_state.json` holds, and the
    state never recorded a title - so all 182 rows of the published index
    reached the serving layer with no `title` key at all.

    Two halves, and both are tested here: `pack_one()` now records the title at
    pack time (going forward), and `scripts/backfill_pack_titles.py` reads it
    out of the packs for rows written before that (the one-time migration)."""

    def _pack(self, tmp_path, book_id, title):
        payload = {"book_id": book_id, "title": title, "source": "epub"}
        path = tmp_path / f"{book_id}.json.gz"
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return path

    def test_title_comes_from_the_pack(self, tmp_path):
        from scripts.backfill_pack_titles import title_from_pack

        self._pack(tmp_path, "a-killer-s-mind", "A Killer's Mind")
        assert title_from_pack("a-killer-s-mind", tmp_path) == "A Killer's Mind"

    def test_a_missing_pack_is_none_not_a_deslugged_guess(self, tmp_path):
        """⚠️ De-slugging cannot restore an apostrophe, a colon or a capital
        inside a word. A missing title is visibly missing; a wrong one is not."""
        from scripts.backfill_pack_titles import title_from_pack

        assert title_from_pack("never-packed", tmp_path) is None

    @pytest.mark.parametrize("title", [None, "", "   ", 42, ["a"]])
    def test_a_blank_or_non_string_title_is_refused(self, tmp_path, title):
        from scripts.backfill_pack_titles import title_from_pack

        path = tmp_path / "b.json.gz"
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            json.dump({"book_id": "b", "title": title}, fh)
        assert title_from_pack("b", tmp_path) is None

    def test_a_corrupt_pack_never_raises(self, tmp_path):
        from scripts.backfill_pack_titles import title_from_pack

        (tmp_path / "torn.json.gz").write_bytes(b"not a gzip at all")
        assert title_from_pack("torn", tmp_path) is None

    def test_backfill_fills_only_what_is_missing_and_is_idempotent(self, tmp_path):
        from scripts.backfill_pack_titles import backfill

        self._pack(tmp_path, "one", "Book One")
        self._pack(tmp_path, "two", "Book Two")
        state = {"books": {
            "one": {"status": "done"},
            "two": {"status": "done", "title": "Hand-corrected Title"},
            "three": {"status": "needs-ocr"},          # no pack: never packed
        }}

        report = backfill(state, tmp_path)
        assert state["books"]["one"]["title"] == "Book One"
        assert state["books"]["two"]["title"] == "Hand-corrected Title", \
            "an existing title must never be overwritten from disk"
        assert "title" not in state["books"]["three"]
        assert [b for b, _ in report["filled"]] == ["one"]
        assert report["unresolved"] == ["three"]

        again = backfill(state, tmp_path)
        assert again["filled"] == [], "re-running must be a no-op"

    def test_a_done_pack_records_its_title_the_same_way_the_pack_spells_it(self):
        """The state's title and the pack's title must be ONE string, or a
        backfilled row and a freshly packed row disagree about the same book."""
        book = ExtractedBook(
            book_id="wintersteel", title="Wintersteel", source="epub",
            chapters=[ExtractedChapter(index=0, title="c0", text="hello there")],
        )
        chunks, refs = chunk_book(book)
        pack = build_pack(book, chunks, refs)
        assert pack["title"] == book.title


# --------------------------------------------------------------------------
# the queue summary the status board reads
# --------------------------------------------------------------------------

class TestQueueSummary:
    """The lanes /status/processing shows. `build_queue()` stays the only place
    that DECIDES a tier; this only counts what it returned."""

    def _item(self, tier, needs_gpu=False, bid="b"):
        return QueueItem(bid, "T", tier, "epub", needs_gpu=needs_gpu)

    def test_counts_every_lane_including_the_empty_ones(self):
        from app.core.ingest_queue_summary import build_queue_summary

        summary = build_queue_summary([
            self._item(TIER_REVIEWED_AUDIO, needs_gpu=True),
            self._item(TIER_REVIEWED_AUDIO, needs_gpu=True),
            self._item(TIER_REST_AUDIO, needs_gpu=True),
            self._item(TIER_NEEDS_OCR),
        ])
        assert summary["lanes"]["audiobook-with-review"] == 2
        assert summary["lanes"]["audiobook"] == 1
        assert summary["lanes"]["deferred-pdf"] == 1
        # ⚠️ Present-and-zero, not absent. Absent means "unknown" to the reader.
        assert summary["lanes"]["epub"] == 0
        assert summary["lanes"]["twin"] == 0

    def test_the_cross_check_the_reader_relies_on_holds(self):
        """processing-board.mjs shows the split ONLY when reviewed + rest equals
        the GPU bucket. If that ever stops holding, the page silently stops
        splitting - so it is pinned here, in the repo that produces both."""
        from app.core.ingest_queue_summary import build_queue_summary

        summary = build_queue_summary([
            self._item(TIER_REVIEWED_AUDIO, needs_gpu=True),
            self._item(TIER_REST_AUDIO, needs_gpu=True),
            self._item(TIER_REST_AUDIO, needs_gpu=True),
            self._item(TIER_EPUB),
            self._item(TIER_NEEDS_OCR),
        ])
        lanes = summary["lanes"]
        assert lanes["audiobook-with-review"] + lanes["audiobook"] == summary["gpu"] == 3
        assert summary["cpu"] == 2
        assert summary["total"] == 5

    def test_an_unknown_tier_becomes_its_own_lane_rather_than_vanishing(self):
        from app.core.ingest_queue_summary import build_queue_summary

        summary = build_queue_summary([self._item(99)])
        assert summary["lanes"]["tier-99"] == 1
        assert summary["total"] == 1

    def test_writing_is_atomic_and_never_raises(self, tmp_path):
        """⚠️ A reporting artefact must never be able to stop an ingest run."""
        from app.core.ingest_queue_summary import build_queue_summary, write_queue_summary

        path = tmp_path / "queue_summary.json"
        write_queue_summary(build_queue_summary([self._item(TIER_EPUB)]), path)
        assert json.loads(path.read_text(encoding="utf-8"))["lanes"]["epub"] == 1
        assert not list(tmp_path.glob("*.tmp")), "no temp file may be left behind"

        # An unwritable destination costs the page its split and the run nothing.
        write_queue_summary({"x": 1}, tmp_path / "no" / "such" / "\0bad")


# --------------------------------------------------------------------------
# the third copy of the transcripts (owner: "we lose this data we lose it all")
# --------------------------------------------------------------------------

class TestTranscriptBackup:
    """Transcripts are the GPU-hours artifact and had only two copies, both on
    this machine's blast radius. These pin the properties that make the third
    copy trustworthy AND harmless: deterministic bytes, real idempotence, and a
    failure that can never stop an ingest run."""

    def _json(self, tmp_path, stem="b", segments=None):
        path = tmp_path / f"{stem}.json"
        payload = {"meta": {"source_m4b": "x.m4b"},
                   "segments": segments if segments is not None else
                   [{"start": 0.0, "text": " hello "}]}
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_render_txt_matches_the_workers_format_exactly(self):
        """⚠️ THE RECOVERY PATH for the .txt we deliberately do not upload. The
        format string is copied from _whisper_worker.py and must not be tidied."""
        from app.core.ingest_transcripts import render_txt

        segs = [{"start": 0.0, "text": "first"},
                {"start": 3661.5, "text": "  second  "}]
        assert render_txt(segs) == "[00:00:00.000] first\n[01:01:01.500] second\n"

    def test_gzip_is_deterministic_so_the_digest_can_be_trusted(self, tmp_path):
        """mtime=0, same as write_pack_gz. Without it the container changes every
        run even when the content does not, and every backfill re-uploads."""
        from app.core.ingest_transcripts import gzip_bytes

        path = self._json(tmp_path)
        assert gzip_bytes(path) == gzip_bytes(path)

    def test_gzip_round_trips_to_the_original_bytes(self, tmp_path):
        from app.core.ingest_transcripts import gzip_bytes

        path = self._json(tmp_path)
        assert gzip.decompress(gzip_bytes(path)) == path.read_bytes()

    def test_a_matching_digest_skips_the_upload(self, tmp_path, monkeypatch):
        import app.core.ingest_transcripts as it

        calls = []
        monkeypatch.setattr(it, "r2_put", lambda key, p: calls.append(key))
        path = self._json(tmp_path)
        ledger = {}

        first = it.upload_transcript(path, ledger)
        assert first["status"] == "uploaded" and len(calls) == 1
        second = it.upload_transcript(path, ledger)
        assert second["status"] == "skipped", "an unchanged transcript must not re-upload"
        assert len(calls) == 1

        # ...and --force overrides it, for a bucket somebody emptied.
        third = it.upload_transcript(path, ledger, force=True)
        assert third["status"] == "uploaded" and len(calls) == 2

    def test_the_ledger_is_written_only_after_a_successful_put(self, tmp_path, monkeypatch):
        """⚠️ It may under-claim (costing a re-upload) but must never over-claim,
        or a lost object looks backed up forever."""
        import app.core.ingest_transcripts as it

        def boom(key, p):
            raise RuntimeError("wrangler said no")

        monkeypatch.setattr(it, "r2_put", boom)
        ledger = {}
        res = it.upload_transcript(self._json(tmp_path), ledger)
        assert res["status"] == "failed"
        assert ledger == {}, "a failed put must leave no trace claiming success"

    def test_upload_never_raises(self, tmp_path, monkeypatch):
        """A backup step that can stop an ingest run is a liability, not a
        safety net - the books are the job."""
        import app.core.ingest_transcripts as it

        monkeypatch.setattr(it, "r2_put", lambda key, p: (_ for _ in ()).throw(OSError("disk")))
        res = it.upload_transcript(self._json(tmp_path), {})
        assert res["status"] == "failed" and "error" in res

        missing = it.upload_transcript(tmp_path / "nope.json", {})
        assert missing["status"] == "failed"

    def test_only_transcript_json_is_ever_uploaded(self, tmp_path):
        """⚠️ whisper-venv/ and work/ (a 2 GB intermediate WAV) are siblings of
        the transcripts dir and are never globbed - the exclusion is structural,
        not a list somebody has to remember."""
        from app.core.ingest_transcripts import transcripts_on_disk

        self._json(tmp_path, "keep")
        (tmp_path / "keep.txt").write_text("derived", encoding="utf-8")
        (tmp_path / "huge.wav").write_bytes(b"\0" * 16)
        (tmp_path / "work").mkdir()
        self._json(tmp_path / "work", "intermediate")

        assert [p.name for p in transcripts_on_disk(tmp_path)] == ["keep.json"]

    def test_the_key_lands_under_the_gated_prefix(self):
        """Same bucket and privacy class as the packs. A transcript is the book
        as text; a public bucket would publish it."""
        from app.core.ingest_transcripts import transcript_key

        assert transcript_key("a_book") == "transcripts/a_book.json.gz"

    def test_backfill_reports_every_outcome(self, tmp_path, monkeypatch):
        import app.core.ingest_transcripts as it

        monkeypatch.setattr(it, "r2_put", lambda key, p: None)
        self._json(tmp_path, "one")
        self._json(tmp_path, "two")
        ledger_path = tmp_path / "ledger.json"

        out = it.backfill(tmp_path, ledger_path)
        assert len(out["uploaded"]) == 2 and out["total"] == 2
        again = it.backfill(tmp_path, ledger_path)
        assert len(again["skipped"]) == 2 and again["uploaded"] == []

    def test_the_ledger_is_never_uploaded_as_a_transcript(self, tmp_path, monkeypatch):
        """⚠️ Found by the test above, not by review: with the ledger inside the
        scanned directory the glob picked it up, uploaded it, and thereby
        CHANGED it - so it re-uploaded itself on every run, forever."""
        import app.core.ingest_transcripts as it

        monkeypatch.setattr(it, "r2_put", lambda key, p: None)
        self._json(tmp_path, "real")
        ledger_path = tmp_path / "ledger.json"

        it.backfill(tmp_path, ledger_path)
        assert ledger_path.exists()
        second = it.backfill(tmp_path, ledger_path)
        assert second["uploaded"] == [], "the ledger must not become a transcript"
        assert [p.name for p in it.transcripts_on_disk(tmp_path, ledger_path)] == ["real.json"]
