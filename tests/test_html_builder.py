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
            "desc": "Test description",
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


class TestSafeUrl(unittest.TestCase):
    """Test _safe_url blocks dangerous URL schemes (XSS fix)."""

    def setUp(self):
        from app.web.html_builder import _safe_url
        self.safe_url = _safe_url

    def test_https_passes(self):
        url = "https://hardcover.app/books/dune"
        self.assertEqual(self.safe_url(url), url)

    def test_http_passes(self):
        url = "http://example.com/book"
        self.assertEqual(self.safe_url(url), url)

    def test_javascript_blocked(self):
        self.assertEqual(self.safe_url("javascript:alert(1)"), "")

    def test_javascript_mixed_case_blocked(self):
        self.assertEqual(self.safe_url("JavaScript:alert(1)"), "")

    def test_data_uri_blocked(self):
        self.assertEqual(self.safe_url("data:text/html,<script>evil()</script>"), "")

    def test_vbscript_blocked(self):
        self.assertEqual(self.safe_url("vbscript:evil()"), "")

    def test_empty_string_returns_empty(self):
        self.assertEqual(self.safe_url(""), "")


class TestRatingHtmlXSS(unittest.TestCase):
    """Test that javascript: URLs don't reach href attributes."""

    def setUp(self):
        from app.web.html_builder import _rating_html, _hardcover_chip
        self.rating_html = _rating_html
        self.hardcover_chip = _hardcover_chip

    def test_rating_html_javascript_url_omits_link(self):
        row = {
            "hardcover_rating": "4.5",
            "hardcover_ratings_count": "100",
            "hardcover_url": "javascript:alert(1)",
        }
        result = self.rating_html(row)
        self.assertNotIn("href", result)
        self.assertNotIn("javascript:", result)
        self.assertIn("4.5", result)

    def test_rating_html_valid_url_produces_anchor(self):
        row = {
            "hardcover_rating": "4.5",
            "hardcover_ratings_count": "100",
            "hardcover_url": "https://hardcover.app/books/dune",
        }
        result = self.rating_html(row)
        self.assertIn('href="https://hardcover.app/books/dune"', result)

    def test_hardcover_chip_javascript_url_uses_span_not_anchor(self):
        row = {
            "hardcover_rating": "4.5",
            "hardcover_ratings_count": "100",
            "hardcover_url": "javascript:evil()",
            "hardcover_match_confidence": "0.92",
        }
        result = self.hardcover_chip(row)
        self.assertNotIn("javascript:", result)
        self.assertNotIn("<a ", result)
        self.assertIn("<span", result)

    def test_hardcover_chip_valid_url_produces_anchor(self):
        row = {
            "hardcover_rating": "4.5",
            "hardcover_ratings_count": "100",
            "hardcover_url": "https://hardcover.app/books/dune",
            "hardcover_match_confidence": "0.92",
        }
        result = self.hardcover_chip(row)
        self.assertIn("<a ", result)
        self.assertIn("https://hardcover.app/books/dune", result)


if __name__ == "__main__":
    unittest.main()
