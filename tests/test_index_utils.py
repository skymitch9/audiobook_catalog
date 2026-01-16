"""
Unit tests for index normalization and sorting.
Tests conversion of various index formats to sortable values.
"""
import unittest
from app.core.index_utils import normalize_index, sort_key_for_index


class TestIndexNormalization(unittest.TestCase):
    """Test index normalization from various formats."""

    def test_numeric_index(self):
        """Test simple numeric indices."""
        self.assertEqual(normalize_index("1"), "1")
        self.assertEqual(normalize_index("42"), "42")
        self.assertEqual(normalize_index("100"), "100")

    def test_decimal_index(self):
        """Test decimal indices (for novellas, etc.)."""
        self.assertEqual(normalize_index("2.5"), "2.5")
        self.assertEqual(normalize_index("1.1"), "1.1")

    def test_roman_numerals(self):
        """Test Roman numeral conversion."""
        self.assertEqual(normalize_index("I"), "1")
        self.assertEqual(normalize_index("IV"), "4")
        self.assertEqual(normalize_index("IX"), "9")
        self.assertEqual(normalize_index("X"), "10")
        self.assertEqual(normalize_index("L"), "50")
        self.assertEqual(normalize_index("C"), "100")

    def test_word_numbers(self):
        """Test word-based number conversion."""
        self.assertEqual(normalize_index("one"), "1")
        self.assertEqual(normalize_index("two"), "2")
        self.assertEqual(normalize_index("ten"), "10")
        self.assertEqual(normalize_index("twenty"), "20")

    def test_compound_word_numbers(self):
        """Test compound word numbers."""
        self.assertEqual(normalize_index("twenty-one"), "21")
        self.assertEqual(normalize_index("thirty five"), "35")

    def test_range_index(self):
        """Test range indices (e.g., '1-3')."""
        self.assertEqual(normalize_index("1-3"), "1-3")
        self.assertEqual(normalize_index("2.5-3.5"), "2.5-3.5")

    def test_empty_index(self):
        """Test empty index."""
        self.assertEqual(normalize_index(""), "")
        self.assertEqual(normalize_index(None), "")

    def test_whitespace_handling(self):
        """Test whitespace trimming."""
        self.assertEqual(normalize_index("  5  "), "5")
        self.assertEqual(normalize_index("\t10\n"), "10")

    def test_case_insensitive(self):
        """Test case insensitivity."""
        self.assertEqual(normalize_index("ONE"), "1")
        self.assertEqual(normalize_index("Three"), "3")
        self.assertEqual(normalize_index("iv"), "4")


class TestIndexSorting(unittest.TestCase):
    """Test sort key generation for indices."""

    def test_numeric_sort_keys(self):
        """Test sort keys for numeric indices."""
        self.assertEqual(sort_key_for_index("1"), 1.0)
        self.assertEqual(sort_key_for_index("10"), 10.0)
        self.assertEqual(sort_key_for_index("100"), 100.0)

    def test_decimal_sort_keys(self):
        """Test sort keys for decimal indices."""
        self.assertEqual(sort_key_for_index("2.5"), 2.5)
        self.assertEqual(sort_key_for_index("1.1"), 1.1)

    def test_roman_numeral_sort_keys(self):
        """Test sort keys for Roman numerals."""
        self.assertEqual(sort_key_for_index("I"), 1.0)
        self.assertEqual(sort_key_for_index("V"), 5.0)
        self.assertEqual(sort_key_for_index("X"), 10.0)

    def test_word_number_sort_keys(self):
        """Test sort keys for word numbers."""
        self.assertEqual(sort_key_for_index("one"), 1.0)
        self.assertEqual(sort_key_for_index("five"), 5.0)

    def test_range_sort_keys(self):
        """Test sort keys for ranges (should use first number)."""
        self.assertEqual(sort_key_for_index("1-3"), 1.0)
        self.assertEqual(sort_key_for_index("5-7"), 5.0)

    def test_invalid_sort_keys(self):
        """Test sort keys for invalid indices."""
        self.assertIsNone(sort_key_for_index(""))
        self.assertIsNone(sort_key_for_index("invalid"))
        self.assertIsNone(sort_key_for_index(None))

    def test_sort_order(self):
        """Test that sort keys produce correct ordering."""
        indices = ["10", "2", "1", "20", "3"]
        sorted_indices = sorted(indices, key=lambda x: sort_key_for_index(x) or 0)
        self.assertEqual(sorted_indices, ["1", "2", "3", "10", "20"])


if __name__ == '__main__':
    unittest.main()
