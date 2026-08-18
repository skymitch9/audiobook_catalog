"""Tests for app/tools/extract_chapters.py — pure logic only (no library, no network).

Two things are pinned here beyond part detection, both added with the
audio-player phase 0a `start_sec` change (2026-08-17):

  1. ⚠️ **THE PRECISION.** `start_sec` must carry ffprobe's `start_time`
     UNROUNDED, and `start_min` must keep its 0.1-minute rounding untouched —
     the book clubs read `start_min` and the player will read `start_sec`, and
     `{chapter, offsetSec}` is a persisted key, so a later correction of these
     boundaries is a migration rather than an edit. The ffprobe call is
     stubbed: the point is the PARSE, not whether ffprobe works.
  2. **The incremental backfill.** `needs_precision` decides whether the whole
     1,073-file library gets re-probed on a pipeline run. It must say yes once
     and no forever after, and never for a source that has no timestamps.
"""

import json
import subprocess
import unittest
from unittest import mock

from app.tools import extract_chapters as ec
from app.tools.extract_chapters import detect_parts, needs_precision


def chapters(*titles):
    return [{"title": t, "start_min": i * 10.0} for i, t in enumerate(titles)]


# A real ffprobe reply, copied verbatim from
# "Disney Junior- Doc McStuffins- Toy Hospital.m4b" (measured 2026-08-17).
# ⚠️ 269.574 s rounds to 4.5 min == 270.0 s: 0.426 s of error, in a file whose
# chapter is 251 s long. That gap is the entire reason `start_sec` exists.
FFPROBE_REPLY = json.dumps({"chapters": [
    {"start_time": "0.000000", "tags": {"title": "Opening Credits"}},
    {"start_time": "18.075000", "tags": {"title": "Disney Junior: Doc McStuffins: Toy Hospital"}},
    {"start_time": "269.574000", "tags": {"title": "End Credits"}},
]})


class FfprobePrecisionTestCase(unittest.TestCase):
    """`start_sec` is exact; `start_min` is unchanged and still 6 s coarse."""

    def parse(self, payload=FFPROBE_REPLY):
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=payload, stderr="")
        with mock.patch.object(ec.shutil, "which", return_value="ffprobe"), \
             mock.patch.object(ec.subprocess, "run", return_value=completed):
            return ec.chapters_from_ffprobe(ec.Path("does-not-matter.m4b"))

    def test_start_sec_is_the_exact_ffprobe_value(self):
        got = self.parse()
        self.assertEqual([c["start_sec"] for c in got], [0.0, 18.075, 269.574])

    def test_start_min_keeps_its_original_rounding(self):
        # The book-club Start Read modal reads this field. Additive change only.
        self.assertEqual([c["start_min"] for c in self.parse()], [0.0, 0.3, 4.5])

    def test_start_sec_is_more_precise_than_start_min_can_express(self):
        end_credits = self.parse()[2]
        self.assertNotEqual(end_credits["start_sec"], end_credits["start_min"] * 60)
        self.assertAlmostEqual(end_credits["start_min"] * 60 - end_credits["start_sec"], 0.426, places=3)

    def test_both_fields_are_none_when_the_time_is_unreadable(self):
        payload = json.dumps({"chapters": [{"start_time": "not-a-number", "tags": {"title": "X"}}]})
        self.assertEqual(self.parse(payload), [{"title": "X", "start_min": None, "start_sec": None}])


class NeedsPrecisionTestCase(unittest.TestCase):
    def entry(self, source="m4b", chs=None):
        return {"source": source, "chapters": chs if chs is not None else [], "parts": []}

    def test_old_m4b_entry_without_start_sec_qualifies(self):
        self.assertTrue(needs_precision(self.entry(chs=[{"title": "A", "start_min": 0.3}])))

    def test_backfilled_entry_does_not_qualify_again(self):
        self.assertFalse(needs_precision(
            self.entry(chs=[{"title": "A", "start_min": 0.3, "start_sec": 18.075}])))

    def test_llm_and_none_sources_never_qualify(self):
        # They carry no timestamps at all, so a re-probe buys nothing and an
        # llm re-run would spend a paid API call per book.
        self.assertFalse(needs_precision(self.entry(source="llm", chs=[{"title": "A", "start_min": None}])))
        self.assertFalse(needs_precision(self.entry(source="none")))

    def test_a_chapter_with_no_start_min_is_not_improvable(self):
        self.assertFalse(needs_precision(self.entry(chs=[{"title": "A", "start_min": None}])))

    def test_a_partially_backfilled_entry_still_qualifies(self):
        self.assertTrue(needs_precision(self.entry(chs=[
            {"title": "A", "start_min": 0.0, "start_sec": 0.0},
            {"title": "B", "start_min": 4.5},
        ])))

    def test_junk_is_not_a_backfill_candidate(self):
        for junk in (None, [], "m4b", {"chapters": [{"start_min": 1.0}]}):
            self.assertFalse(needs_precision(junk))


class DetectPartsTestCase(unittest.TestCase):
    def test_groups_chapters_under_part_headings(self):
        parts = detect_parts(chapters(
            "Part One", "Chapter 1", "Chapter 2",
            "Part Two", "Chapter 3", "Chapter 4", "Epilogue",
        ))
        self.assertEqual(
            parts,
            [
                {"label": "Part One", "start_index": 0, "end_index": 2},
                {"label": "Part Two", "start_index": 3, "end_index": 6},
            ],
        )

    def test_recognizes_book_and_numeric_variants(self):
        parts = detect_parts(chapters("Book 1", "Ch 1", "Book 2", "Ch 2"))
        self.assertEqual([p["label"] for p in parts], ["Book 1", "Book 2"])

    def test_no_parts_for_plain_chapter_lists(self):
        self.assertEqual(detect_parts(chapters("Chapter 1", "Chapter 2", "Chapter 3")), [])

    def test_single_part_heading_is_not_a_split(self):
        self.assertEqual(detect_parts(chapters("Part One", "Chapter 1", "Chapter 2")), [])

    def test_does_not_match_part_mid_title(self):
        self.assertEqual(detect_parts(chapters("The Party Begins", "Departure", "A Part of Me")), [])


if __name__ == "__main__":
    unittest.main()
