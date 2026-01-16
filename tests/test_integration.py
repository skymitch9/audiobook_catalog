"""
Integration tests for end-to-end workflows.
Tests complete processing pipelines.
"""

import shutil
import tempfile
import unittest
from pathlib import Path


class TestEndToEndWorkflow(unittest.TestCase):
    """Test complete workflows from file to output."""

    def setUp(self):
        """Set up test environment."""
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        """Clean up test environment."""
        if Path(self.test_dir).exists():
            shutil.rmtree(self.test_dir)

    def test_title_to_series_extraction_workflow(self):
        """Test complete workflow from title to series extraction."""
        from app.core.index_utils import normalize_index, sort_key_for_index
        from app.parsers.title import parse_series_and_index_from_title

        # Simulate processing a book title
        title = "The Gender Game 2: The Gender Secret"
        series, index = parse_series_and_index_from_title(title)

        # Verify series extraction
        self.assertEqual(series, "The Gender Game")
        self.assertEqual(index, "2")

        # Verify index can be sorted
        sort_key = sort_key_for_index(index)
        self.assertEqual(sort_key, 2.0)

    def test_multiple_books_sorting(self):
        """Test sorting multiple books in a series."""
        from app.core.index_utils import sort_key_for_index
        from app.parsers.title import parse_series_and_index_from_title

        titles = [
            "The Gender Game 3: The Gender Lie",
            "The Gender Game 1: The Gender Game",
            "The Gender Game 2: The Gender Secret",
        ]

        books = []
        for title in titles:
            series, index = parse_series_and_index_from_title(title)
            books.append({"title": title, "series": series, "index": index, "sort_key": sort_key_for_index(index)})

        # Sort by sort_key
        sorted_books = sorted(books, key=lambda x: x["sort_key"] or 0)

        # Verify correct order
        self.assertEqual(sorted_books[0]["index"], "1")
        self.assertEqual(sorted_books[1]["index"], "2")
        self.assertEqual(sorted_books[2]["index"], "3")


class TestDataValidation(unittest.TestCase):
    """Test data validation and error handling."""

    def test_malformed_title_handling(self):
        """Test handling of malformed titles."""
        from app.parsers.title import parse_series_and_index_from_title

        malformed_titles = [
            "",
            None,
            "   ",
            "123",
            "!@#$%^&*()",
        ]

        for title in malformed_titles:
            series, index = parse_series_and_index_from_title(title)
            # Should not crash, should return None or empty
            self.assertTrue(series is None or series == "")
            self.assertTrue(index is None or index == "")

    def test_edge_case_indices(self):
        """Test edge case index values."""
        from app.core.index_utils import normalize_index, sort_key_for_index

        edge_cases = [
            "0",
            "999",
            "1.0",
            "0.5",
            "MMXXIV",  # 2024 in Roman numerals
        ]

        for index in edge_cases:
            # Should not crash
            normalized = normalize_index(index)
            sort_key = sort_key_for_index(normalized)
            self.assertIsNotNone(normalized)


if __name__ == "__main__":
    unittest.main()
