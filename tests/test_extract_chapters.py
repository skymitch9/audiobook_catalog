"""Tests for app/tools/extract_chapters.py — pure logic only (no library, no network)."""

import unittest

from app.tools.extract_chapters import detect_parts


def chapters(*titles):
    return [{"title": t, "start_min": i * 10.0} for i, t in enumerate(titles)]


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
