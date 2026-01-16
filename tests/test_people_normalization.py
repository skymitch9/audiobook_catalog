"""
Unit tests for people name normalization (authors, narrators).
Tests proper formatting and deduplication of names.
"""
import unittest
from app.core.people import normalize_people_list


class TestPeopleNormalization(unittest.TestCase):
    """Test normalization of author and narrator names."""

    def test_single_name(self):
        """Test single name normalization."""
        result = normalize_people_list("John Smith")
        self.assertEqual(result, "John Smith")

    def test_multiple_names_comma_separated(self):
        """Test comma-separated names."""
        result = normalize_people_list("John Smith, Jane Doe")
        self.assertEqual(result, "John Smith, Jane Doe")

    def test_multiple_names_semicolon_separated(self):
        """Test semicolon-separated names."""
        result = normalize_people_list("John Smith; Jane Doe")
        self.assertEqual(result, "John Smith, Jane Doe")

    def test_multiple_names_and_separated(self):
        """Test 'and' separated names."""
        result = normalize_people_list("John Smith and Jane Doe")
        self.assertEqual(result, "John Smith, Jane Doe")

    def test_mixed_separators(self):
        """Test mixed separators."""
        result = normalize_people_list("John Smith, Jane Doe & Bob Johnson")
        self.assertEqual(result, "John Smith, Jane Doe, Bob Johnson")

    def test_duplicate_removal(self):
        """Test that duplicates are removed."""
        result = normalize_people_list("John Smith, John Smith, Jane Doe")
        self.assertEqual(result, "John Smith, Jane Doe")

    def test_case_normalization(self):
        """Test proper case normalization."""
        result = normalize_people_list("john smith")
        self.assertEqual(result, "John Smith")
        
        result = normalize_people_list("JANE DOE")
        self.assertEqual(result, "Jane Doe")

    def test_whitespace_handling(self):
        """Test extra whitespace removal."""
        result = normalize_people_list("  John   Smith  ,  Jane   Doe  ")
        self.assertEqual(result, "John Smith, Jane Doe")

    def test_empty_input(self):
        """Test empty input."""
        self.assertIsNone(normalize_people_list(""))
        self.assertIsNone(normalize_people_list(None))
        self.assertIsNone(normalize_people_list("   "))

    def test_special_characters(self):
        """Test names with special characters."""
        result = normalize_people_list("O'Brien, Mary-Jane Smith")
        self.assertIsNotNone(result)

    def test_initials(self):
        """Test names with initials."""
        result = normalize_people_list("J.K. Rowling")
        self.assertEqual(result, "J.K. Rowling")

    def test_multiple_word_names(self):
        """Test names with multiple words."""
        result = normalize_people_list("Martin Luther King Jr.")
        self.assertIsNotNone(result)

    def test_order_preservation(self):
        """Test that order is preserved."""
        result = normalize_people_list("Alice, Bob, Charlie")
        self.assertEqual(result, "Alice, Bob, Charlie")


if __name__ == '__main__':
    unittest.main()
