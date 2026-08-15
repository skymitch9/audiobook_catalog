"""Tests for app/core/series_gaps.py — the per-series owned-volumes / gap
summary. See that module's docstring for the interpretation this pins:
ranged indices ("3-4") expand to every whole number they span, gaps are only
ever computed BETWEEN owned numbers (never against a claimed series total),
and a series needs at least two distinct numeric indices before it gets a
summary at all.
"""
from __future__ import annotations

import unittest

from app.core.series_gaps import compute_series_gaps


def row(series: str, index_display: str) -> dict:
    return {"title": f"{series} {index_display}", "series": series, "series_index_display": index_display}


class TestComputeSeriesGaps(unittest.TestCase):
    def test_contiguous_run_reports_no_gap(self):
        rows = [row("Alpha", str(n)) for n in (1, 2, 3, 4, 5, 6, 7, 8)]
        gaps = compute_series_gaps(rows)
        self.assertEqual(gaps["Alpha"], "Volumes 1-8 owned")
        self.assertNotIn("gap", gaps["Alpha"])

    def test_a_single_gap_matches_the_worked_example_in_the_brief(self):
        rows = [row("Beta", str(n)) for n in (1, 2, 3, 4, 5, 6, 8)]
        gaps = compute_series_gaps(rows)
        self.assertEqual(gaps["Beta"], "Volumes 1-6, 8 owned — gap: 7")

    def test_multiple_gaps(self):
        rows = [row("Gamma", str(n)) for n in (1, 3, 5, 7)]
        gaps = compute_series_gaps(rows)
        self.assertEqual(gaps["Gamma"], "Volumes 1, 3, 5, 7 owned — gap: 2, 4, 6")

    def test_ranged_index_is_interpreted_as_owning_both_endpoints(self):
        # Documented interpretation: "3-4" means this catalog owns both 3
        # and 4 (an omnibus/bind-up), not only sort_key_for_index()'s low
        # end. Owning 1, 2, (3-4 combined), 6 -> gap is only 5.
        rows = [row("Delta", "1"), row("Delta", "2"), row("Delta", "3-4"), row("Delta", "6")]
        gaps = compute_series_gaps(rows)
        self.assertEqual(gaps["Delta"], "Volumes 1-4, 6 owned — gap: 5")

    def test_a_wide_reversed_or_fractional_range_falls_back_to_its_endpoints(self):
        # "9999-1" is nonsensical as a span (reversed) - treated as two
        # discrete points (9999, 1) rather than expanded or dropped.
        rows = [row("Zeta", "1"), row("Zeta", "9999-1")]
        gaps = compute_series_gaps(rows)
        self.assertIn("Zeta", gaps)
        self.assertIn("1", gaps["Zeta"])
        self.assertIn("9999", gaps["Zeta"])

    def test_non_numeric_and_blank_indices_do_not_crash_and_are_ignored(self):
        rows = [
            row("Epsilon", "1"),
            row("Epsilon", ""),
            row("Epsilon", "N/A"),
            row("Epsilon", "3"),
            {"title": "No index field at all", "series": "Epsilon"},
        ]
        gaps = compute_series_gaps(rows)
        self.assertEqual(gaps["Epsilon"], "Volumes 1, 3 owned — gap: 2")

    def test_a_series_with_only_one_numeric_book_has_no_summary(self):
        rows = [row("Solo", "1"), row("Solo", "")]
        gaps = compute_series_gaps(rows)
        self.assertNotIn("Solo", gaps)

    def test_a_series_with_zero_numeric_books_has_no_summary(self):
        rows = [row("Blank", ""), row("Blank", "N/A")]
        gaps = compute_series_gaps(rows)
        self.assertNotIn("Blank", gaps)

    def test_rows_with_no_series_are_skipped_entirely(self):
        rows = [{"title": "Standalone", "series": "", "series_index_display": "1"}]
        gaps = compute_series_gaps(rows)
        self.assertEqual(gaps, {})

    def test_duplicate_rows_for_the_same_volume_do_not_fabricate_ownership(self):
        # Two files/formats of the same volume must not look like two
        # distinct owned numbers.
        rows = [row("Dup", "1"), row("Dup", "1")]
        gaps = compute_series_gaps(rows)
        self.assertNotIn("Dup", gaps)  # still only ONE distinct number owned

    def test_a_fractional_prequel_novella_is_shown_as_owned_and_never_a_gap(self):
        rows = [row("Novella", "0.5"), row("Novella", "1"), row("Novella", "2"), row("Novella", "3")]
        gaps = compute_series_gaps(rows)
        # 0.5 does not collapse into the 1-3 integer run (only consecutive
        # integers merge into a dash-range), but it is present and it is
        # never treated as a gap.
        self.assertEqual(gaps["Novella"], "Volumes 0.5, 1-3 owned")

    def test_result_is_a_plain_dict_of_strings_keyed_by_series(self):
        rows = [row("Alpha", "1"), row("Alpha", "2")]
        gaps = compute_series_gaps(rows)
        self.assertIsInstance(gaps, dict)
        for k, v in gaps.items():
            self.assertIsInstance(k, str)
            self.assertIsInstance(v, str)

    def test_does_not_mutate_input_rows(self):
        rows = [row("Alpha", "1"), row("Alpha", "2")]
        before = [dict(r) for r in rows]
        compute_series_gaps(rows)
        self.assertEqual(rows, before)


if __name__ == "__main__":
    unittest.main()
