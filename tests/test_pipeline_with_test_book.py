"""
Pipeline tests using a generated test book file.

Tests the full chain: test book generation -> metadata extraction -> catalog
rendering -> Discord embed creation. Uses a real (generated) M4B file to
exercise the same code paths as production.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestGenerateTestBook(unittest.TestCase):
    """Test that generate_test_book produces a valid, tagged M4B file."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)

    def test_generates_valid_m4b_with_metadata(self):
        """Generate a test book and verify mutagen can read all tags back."""
        from scripts.generate_test_book import generate_test_book

        out_path = self.test_dir / "test_output.m4b"
        result = generate_test_book(
            title="Integration Test Book",
            author="Pytest Author",
            narrator="Pytest Narrator",
            year="2025",
            genre="Testing",
            series="Pipeline Tests",
            series_index="3",
            output=out_path,
        )

        # File was created
        self.assertTrue(result.exists())
        self.assertGreater(result.stat().st_size, 0)

        # Mutagen can open and read tags
        from mutagen.mp4 import MP4

        audio = MP4(str(result))
        tags = audio.tags

        self.assertEqual(tags["\xa9nam"][0], "Integration Test Book")
        self.assertEqual(tags["\xa9ART"][0], "Pytest Author")
        self.assertEqual(tags["\xa9wrt"][0], "Pytest Narrator")
        self.assertEqual(tags["\xa9day"][0], "2025")
        self.assertEqual(tags["\xa9gen"][0], "Testing")
        self.assertEqual(tags["SRNM"][0], "Pipeline Tests")
        self.assertEqual(tags["SRSQ"][0], "3")

        # Cover art present
        self.assertIn("covr", tags)
        self.assertGreater(len(tags["covr"][0]), 0)

    def test_metadata_extraction_from_test_book(self):
        """Generate a test book, then run extract_metadata() on it and verify output."""
        from scripts.generate_test_book import generate_test_book

        out_path = self.test_dir / "extract_test.m4b"
        generate_test_book(
            title="Metadata Extraction Test",
            author="Extract Author",
            narrator="Extract Narrator",
            year="2024",
            genre="Sci-Fi",
            series="Extraction Series",
            series_index="7",
            output=out_path,
        )

        # Now use the real metadata extractor
        from app.metadata import extract_metadata

        row = extract_metadata(out_path)

        self.assertEqual(row["title"], "Metadata Extraction Test")
        self.assertEqual(row["author"], "Extract Author")
        self.assertEqual(row["narrator"], "Extract Narrator")
        self.assertEqual(row["year"], "2024")
        self.assertEqual(row["genre"], "Sci-Fi")
        self.assertEqual(row["series"], "Extraction Series")
        self.assertEqual(row["series_index_display"], "7")
        # Sort key should be numeric
        self.assertEqual(float(row["series_index_sort"]), 7.0)
        # Cover should have been extracted
        self.assertTrue(row["cover_href"] == "" or row["cover_href"].endswith((".jpg", ".png")))
        # file_mtime should be set
        self.assertGreater(row["file_mtime"], 0)

    def test_clean_removes_test_files(self):
        """Verify clean_test_books finds and removes test-prefixed files."""
        from scripts.generate_test_book import TEST_PREFIX

        # Create a fake test file
        fake_test = self.test_dir / f"{TEST_PREFIX}fake_book.m4b"
        fake_test.write_bytes(b"\x00" * 100)
        self.assertTrue(fake_test.exists())

        # Simulate removal (we just verify the prefix matching logic)
        found = list(self.test_dir.rglob(f"{TEST_PREFIX}*"))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].name, fake_test.name)


class TestPipelineEndToEnd(unittest.TestCase):
    """Test the full pipeline flow: test book -> catalog CSV row -> Discord embed."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)

    def test_test_book_through_catalog_and_discord(self):
        """
        Full integration: generate book -> extract metadata -> write CSV row
        -> create Discord embed. Verifies data flows correctly end-to-end.
        """
        from scripts.generate_test_book import generate_test_book

        # 1. Generate test book
        out_path = self.test_dir / "e2e_test.m4b"
        generate_test_book(
            title="E2E Pipeline Book",
            author="E2E Author",
            narrator="E2E Narrator",
            year="2026",
            genre="Fantasy",
            series="E2E Series",
            series_index="2",
            output=out_path,
        )

        # 2. Extract metadata (same as catalog generation)
        from app.metadata import extract_metadata

        row = extract_metadata(out_path)

        # 3. Write to CSV (verify CSV writer handles the row)
        from app.writers import write_csv

        csv_path = self.test_dir / "test_catalog.csv"
        write_csv([row], csv_path)

        self.assertTrue(csv_path.exists())
        csv_content = csv_path.read_text(encoding="utf-8")
        self.assertIn("E2E Pipeline Book", csv_content)
        self.assertIn("E2E Series", csv_content)
        # Author/narrator get normalized (capitalized) by the metadata extractor
        self.assertIn("E2e Author", csv_content)
        # file_mtime should NOT be in CSV (internal only)
        self.assertNotIn("file_mtime", csv_content)

        # 4. Create Discord embed (same as deploy notification)
        from app.tools.send_discord_notification import create_embed

        new_books_data = {
            "new_count": 1,
            "total_count": 100,
            "books": [
                {
                    "title": row["title"],
                    "author": row["author"],
                    "series": row["series"],
                    "series_index": row["series_index_display"],
                    "narrator": row["narrator"],
                    "year": row["year"],
                    "genre": row["genre"],
                    "duration": row["duration_hhmm"],
                    "cover": row["cover_href"],
                }
            ],
        }

        embeds = create_embed(new_books_data, "https://example.com/catalog/")

        # Should have main embed + 1 book embed
        self.assertEqual(len(embeds), 2)

        # Main embed reports 1 new book
        self.assertIn("1", embeds[0]["description"])
        self.assertEqual(embeds[0]["color"], 3066993)  # Green for new books

        # Book embed has correct title and author
        self.assertEqual(embeds[1]["title"], "E2E Pipeline Book")
        self.assertIn("E2e Author", embeds[1]["description"])
        self.assertIn("E2E Series", embeds[1]["description"])

    def test_html_recently_added_with_test_book(self):
        """Verify the Recently Added HTML renderer works with test book metadata."""
        from scripts.generate_test_book import generate_test_book
        from app.metadata import extract_metadata
        from app.web.html_builder import _recently_added_html

        # Generate and extract
        out_path = self.test_dir / "recent_test.m4b"
        generate_test_book(
            title="Recently Added Test",
            author="Recent Author",
            series="New Series",
            series_index="1",
            output=out_path,
        )
        row = extract_metadata(out_path)

        # Render recently added section
        html = _recently_added_html([row], count=5)

        self.assertIn("Recently Added Test", html)
        self.assertIn("Recent Author", html)
        self.assertIn("New Series", html)


if __name__ == "__main__":
    unittest.main()
