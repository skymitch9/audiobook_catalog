"""
Unit tests for HTML generation and escaping.
Tests proper HTML escaping and structure generation.
"""
import unittest
from app.web.html_builder import _esc


class TestHTMLEscaping(unittest.TestCase):
    """Test HTML escaping for security and correctness."""

    def test_basic_escaping(self):
        """Test basic HTML character escaping."""
        self.assertEqual(_esc("<script>"), "&lt;script&gt;")
        self.assertEqual(_esc("&"), "&amp;")
        self.assertEqual(_esc('"'), "&quot;")
        self.assertEqual(_esc("'"), "&#x27;")

    def test_combined_escaping(self):
        """Test escaping of combined special characters."""
        result = _esc('<div class="test">')
        self.assertIn("&lt;", result)
        self.assertIn("&gt;", result)
        self.assertIn("&quot;", result)

    def test_xss_prevention(self):
        """Test XSS attack prevention."""
        malicious = '<script>alert("XSS")</script>'
        result = _esc(malicious)
        self.assertNotIn("<script>", result)
        self.assertNotIn("</script>", result)

    def test_empty_string(self):
        """Test empty string handling."""
        self.assertEqual(_esc(""), "")

    def test_none_handling(self):
        """Test None handling."""
        self.assertEqual(_esc(None), "")

    def test_normal_text(self):
        """Test that normal text is unchanged."""
        text = "The Lord of the Rings"
        self.assertEqual(_esc(text), text)

    def test_unicode_handling(self):
        """Test Unicode character handling."""
        text = "Café résumé"
        result = _esc(text)
        self.assertIn("Café", result)

    def test_numbers(self):
        """Test number handling."""
        self.assertEqual(_esc(123), "123")
        self.assertEqual(_esc(45.67), "45.67")


class TestCoverButton(unittest.TestCase):
    """Test cover button HTML generation."""

    def test_cover_button_with_data(self):
        """Test cover button generation with complete data."""
        from app.web.html_builder import _cover_button
        
        book_data = {
            "cover_href": "covers/test.jpg",
            "title": "Test Book",
            "series": "Test Series",
            "series_index_display": "1",
            "author": "Test Author",
            "narrator": "Test Narrator",
            "year": "2024",
            "genre": "Fiction",
            "duration_hhmm": "10:30",
            "desc": "Test description"
        }
        
        result = _cover_button(book_data)
        self.assertIn("button", result)
        self.assertIn("cover-btn", result)
        self.assertIn("Test Book", result)

    def test_cover_button_without_cover(self):
        """Test cover button with missing cover."""
        from app.web.html_builder import _cover_button
        
        book_data = {"cover_href": ""}
        result = _cover_button(book_data)
        self.assertEqual(result, "")


if __name__ == '__main__':
    unittest.main()
