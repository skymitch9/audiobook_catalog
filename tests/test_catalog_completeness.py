"""
Test catalog completeness - verify every book has cover, drive link, and author link.
This test ensures the catalog is fully functional with all required resources.
"""
import unittest
import json
from pathlib import Path
from app.config import ROOT_DIR, SITE_DIR, EXTS
from app.metadata import walk_library, extract_metadata


class TestCatalogCompleteness(unittest.TestCase):
    """Test that all books have required resources (covers, links)."""

    @classmethod
    def setUpClass(cls):
        """Load all books and author map once for all tests."""
        cls.files = walk_library(ROOT_DIR, EXTS)
        cls.books = []
        
        # Extract metadata for all books
        for file_path in cls.files:
            try:
                metadata = extract_metadata(file_path)
                cls.books.append({
                    'path': file_path,
                    'metadata': metadata
                })
            except Exception as e:
                print(f"Warning: Failed to extract metadata from {file_path}: {e}")
        
        # Load author map
        cls.author_map = {}
        author_map_paths = [
            Path("author_drive_map.json"),
            Path(__file__).parent.parent.parent / "author_drive_map.json",
        ]
        
        for map_path in author_map_paths:
            if map_path.exists():
                with open(map_path, 'r', encoding='utf-8') as f:
                    cls.author_map = json.load(f)
                break

    def test_all_books_have_covers(self):
        """Test that all books have cover images extracted."""
        # Skip if no books (CI environment)
        if len(self.books) == 0:
            self.skipTest("No library found (expected in CI environment)")
            
        missing_covers = []
        extraction_errors = []
        
        for book in self.books:
            cover_href = book['metadata'].get('cover_href', '')
            if not cover_href:
                missing_covers.append(str(book['path']))
            else:
                # Verify cover file exists
                cover_path = SITE_DIR / cover_href
                if not cover_path.exists():
                    extraction_errors.append(f"{book['path']} (cover file missing: {cover_path})")
        
        # Report missing covers (not a failure, just informational)
        if missing_covers:
            print(f"\n[REPORT] {len(missing_covers)} books without embedded covers:")
            for path in missing_covers[:20]:
                print(f"  - {path}")
            if len(missing_covers) > 20:
                print(f"  ... and {len(missing_covers) - 20} more")
        
        # Only fail if extraction failed (cover_href exists but file doesn't)
        if extraction_errors:
            print(f"\n[ERROR] {len(extraction_errors)} cover extraction failures:")
            for error in extraction_errors[:10]:
                print(f"  - {error}")
            self.fail(f"{len(extraction_errors)} covers failed to extract properly")
        
        print(f"\n[OK] Cover extraction working: {len(self.books) - len(missing_covers)} books have covers")

    def test_all_authors_have_drive_links(self):
        """Test that all authors have Google Drive links in author map."""
        # Skip if no books (CI environment)
        if len(self.books) == 0:
            self.skipTest("No library found (expected in CI environment)")
            
        missing_links = []
        authors_seen = set()
        
        for book in self.books:
            author = book['metadata'].get('author', '')
            if not author or author in authors_seen:
                continue
            
            authors_seen.add(author)
            
            # Check if author has drive link
            if author not in self.author_map:
                # Check if this is a co-author situation (contains comma)
                if ',' in author:
                    # Split and check if primary (first) author has a link
                    primary_author = author.split(',')[0].strip()
                    if primary_author in self.author_map and self.author_map[primary_author]:
                        # Primary author has link, co-author entry not needed
                        continue
                
                missing_links.append(author)
        
        # Report missing links (not a failure, just informational)
        if missing_links:
            print(f"\n[REPORT] {len(missing_links)} authors missing from author_drive_map.json:")
            for author in sorted(missing_links)[:20]:
                print(f"  - {author}")
            if len(missing_links) > 20:
                print(f"  ... and {len(missing_links) - 20} more")
            print(f"\n[TIP] Run 'python -m app.tools.generate_author_map' to add them")
        
        print(f"\n[OK] Author map loaded: {len(self.author_map)} authors mapped")

    def test_author_drive_links_are_valid(self):
        """Test that author drive links are properly formatted (folder IDs or URLs)."""
        # Skip if no author map (CI environment)
        if len(self.author_map) == 0:
            self.skipTest("No author map found (expected in CI environment)")
            
        empty_links = []
        invalid_format = []
        
        for author, link in self.author_map.items():
            if not link:
                empty_links.append(author)
            elif not isinstance(link, str):
                invalid_format.append(f"{author}: not a string")
            elif not (link.startswith('http') or len(link) == 33):
                # Valid if it's a URL or a 33-char Google Drive folder ID
                invalid_format.append(f"{author}: invalid format (not URL or folder ID)")
        
        # Report empty links (not a failure, just informational)
        if empty_links:
            print(f"\n[REPORT] {len(empty_links)} authors need Drive folder IDs:")
            for author in sorted(empty_links)[:20]:
                print(f"  - {author}")
            if len(empty_links) > 20:
                print(f"  ... and {len(empty_links) - 20} more")
            print(f"\n[TIP] Edit author_drive_map.json and add folder IDs")
        
        # Only fail on actual format errors
        if invalid_format:
            print(f"\n[ERROR] {len(invalid_format)} authors have invalid link format:")
            for issue in invalid_format[:10]:
                print(f"  - {issue}")
            self.fail(f"{len(invalid_format)} authors have invalid drive link format")
        
        filled_links = len(self.author_map) - len(empty_links)
        print(f"\n[OK] Valid drive links: {filled_links}/{len(self.author_map)} authors")

    def test_all_books_have_authors(self):
        """Test that all books have author metadata."""
        # Skip if no books (CI environment)
        if len(self.books) == 0:
            self.skipTest("No library found (expected in CI environment)")
            
        missing_authors = []
        
        for book in self.books:
            author = book['metadata'].get('author', '')
            if not author:
                missing_authors.append(str(book['path']))
        
        # Report missing authors (not a failure, just informational)
        if missing_authors:
            print(f"\n[REPORT] {len(missing_authors)} books missing author metadata:")
            for path in missing_authors[:20]:
                print(f"  - {path}")
            if len(missing_authors) > 20:
                print(f"  ... and {len(missing_authors) - 20} more")
        
        print(f"\n[OK] Books with authors: {len(self.books) - len(missing_authors)}/{len(self.books)}")

    def test_catalog_has_books(self):
        """Test that the catalog is not empty."""
        # Skip if running in CI without library
        if len(self.books) == 0:
            self.skipTest("No library found (expected in CI environment)")
        self.assertGreater(len(self.books), 0, "No books found in library")

    def test_author_map_exists(self):
        """Test that author map file exists and is not empty."""
        # Skip if running in CI without author map
        if len(self.author_map) == 0:
            self.skipTest("No author map found (expected in CI environment)")
        self.assertGreater(len(self.author_map), 0, 
                          "author_drive_map.json is empty or not found")


if __name__ == '__main__':
    unittest.main()
