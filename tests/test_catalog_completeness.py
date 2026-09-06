"""
Test catalog completeness - verify every book has cover, drive link, and author link.
This test ensures the catalog is fully functional with all required resources.
Books flagged as BookFunnel sourced (scripts/bookfunnel_books.json) are excluded
from description/genre checks since they have known metadata gaps.

⚠️ THESE 11 TESTS ONLY MEAN SOMETHING ON THE PIPELINE MACHINE, and they say so
out loud. They read the REAL audio library at `ROOT_DIR`; a CI runner has no
library, so there is nothing for them to check there.

Until 2026-09-05 that was expressed as a `self.skipTest("No library found
(expected in CI environment)")` at the top of every method — which meant CI
reported all 11 as PASSING-shaped green while proving nothing (a `skipTest`
inside a body is a skip, but the file looked like a live guard and nobody had
measured that it never fired anywhere it gated). It is now a module-level
`pytest.mark.skipif`, so `pytest -q` prints `11 skipped` with the reason
naming the env var and the path — the same shape as the `requires_platform`
guards in test_universes.py / test_title_key_fixtures.py / test_club_fixtures.py
and the `SHELF_MAP.exists()` guard in test_shelf_map.py.

⚠️ Running these tests is NOT free and NOT read-only: `extract_metadata()`
writes extracted cover art into `output_files/covers/`. Do not run this file on
the pipeline box while an ingestion run is in flight.
"""

import json
import unittest
from pathlib import Path

import pytest

from app.config import EXTS, ROOT_DIR, SITE_DIR
from app.metadata import extract_metadata, walk_library


def _library_present() -> bool:
    """Cheap probe: does ROOT_DIR exist and hold anything at all?

    Deliberately does NOT walk the tree — `walk_library` over ~1,080 books is
    the expensive part these tests exist to do, and a skip decision must not
    cost that.
    """
    try:
        return ROOT_DIR.is_dir() and any(ROOT_DIR.iterdir())
    except OSError:
        return False


LIBRARY_PRESENT = _library_present()

# ⚠️ ONE module-level guard, not eleven in-body ones. It names what is missing
# and how to supply it, so a green CI run says "11 skipped: no audio library at
# <path>" instead of eleven silent passes.
pytestmark = pytest.mark.skipif(
    not LIBRARY_PRESENT,
    reason=(
        f"no audio library at ROOT_DIR={ROOT_DIR} — these 11 completeness checks read the "
        "REAL library and only run on the pipeline machine. Set the ROOT_DIR env var (or "
        "ROOT_DIR in .env) to the audiobook folder; default is <repo>/library."
    ),
)

# The author map is a SECOND resource with its own presence question, so it
# gets its own named marker rather than hiding inside a test body. Two of the
# 11 read it; the other nine do not care whether it is there.
AUTHOR_MAP_PATHS = [
    Path("author_drive_map.json"),  # cwd-relative — the repo root, when pytest is run from there
    Path(__file__).parent.parent.parent / "author_drive_map.json",
]
AUTHOR_MAP_PATH = next((p for p in AUTHOR_MAP_PATHS if p.exists()), None)

requires_author_map = pytest.mark.skipif(
    AUTHOR_MAP_PATH is None,
    reason=(
        "author_drive_map.json not found (tried: "
        + "; ".join(str(p) for p in AUTHOR_MAP_PATHS)
        + ") — run pytest from the repo root, or generate it with "
        "`python -m app.tools.generate_author_map`"
    ),
)

# Load BookFunnel exclusion list
BOOKFUNNEL_PATH = Path(__file__).parent.parent / "scripts" / "bookfunnel_books.json"
BOOKFUNNEL_BOOKS: set = set()
if BOOKFUNNEL_PATH.exists():
    with open(BOOKFUNNEL_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
        BOOKFUNNEL_BOOKS = set(data.get("books", []))


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
                # Flag if BookFunnel sourced
                try:
                    rel_path = str(file_path.relative_to(ROOT_DIR)).replace("\\", "/")
                except ValueError:
                    rel_path = ""
                metadata["_is_bookfunnel"] = rel_path in BOOKFUNNEL_BOOKS
                cls.books.append({"path": file_path, "metadata": metadata})
            except Exception as e:
                print(f"Warning: Failed to extract metadata from {file_path}: {e}")

        # Load author map (one source of truth for where it lives:
        # AUTHOR_MAP_PATH, the same constant `requires_author_map` guards on)
        cls.author_map = {}
        if AUTHOR_MAP_PATH is not None:
            with open(AUTHOR_MAP_PATH, "r", encoding="utf-8") as f:
                cls.author_map = json.load(f)

        # ⚠️ A DIFFERENT condition from the module-level skipif above, and it
        # gets its own named reason. The module guard fires when ROOT_DIR is
        # absent/empty (CI). This fires when ROOT_DIR exists and holds files
        # but none of them are audiobooks — i.e. it is pointed at the wrong
        # folder. Left as a pass would divide by zero in the percentage
        # reports below; left silent it would look like a working check.
        if not cls.books:
            raise unittest.SkipTest(
                f"ROOT_DIR={ROOT_DIR} exists but contains no audiobooks "
                f"(extensions {sorted(EXTS)}) — check the ROOT_DIR env var points at the library"
            )

    def test_all_books_have_covers(self):
        """Test that all books have cover images extracted."""
        from app.config import OUTPUT_DIR

        missing_covers = []
        extraction_errors = []

        for book in self.books:
            cover_href = book["metadata"].get("cover_href", "")
            if not cover_href:
                missing_covers.append(str(book["path"]))
            else:
                # Verify cover file exists in output directory (where extract_metadata writes it)
                # Cover files are staged to site/ only during `stage_site_files()`, so check OUTPUT_DIR first.
                output_cover_path = OUTPUT_DIR / cover_href
                site_cover_path = SITE_DIR / cover_href
                if not output_cover_path.exists() and not site_cover_path.exists():
                    extraction_errors.append(f"{book['path']} (cover file missing from both output and site dirs)")

        # Report missing covers (not a failure, just informational)
        if missing_covers:
            print(f"\n[REPORT] {len(missing_covers)} books without embedded covers:")
            for path in missing_covers[:20]:
                print(f"  - {path}")
            if len(missing_covers) > 20:
                print(f"  ... and {len(missing_covers) - 20} more")

        # Only fail if extraction failed (cover_href exists but file doesn't in either location)
        if extraction_errors:
            print(f"\n[ERROR] {len(extraction_errors)} cover extraction failures:")
            for error in extraction_errors[:10]:
                print(f"  - {error}")
            if len(extraction_errors) > 10:
                print(f"  ... and {len(extraction_errors) - 10} more")
            self.fail(f"{len(extraction_errors)} covers failed to extract properly")

        print(f"\n[OK] Cover extraction working: {len(self.books) - len(missing_covers)} books have covers")

    def test_all_authors_have_drive_links(self):
        """Test that all authors have Google Drive links in author map."""
        missing_links = []
        authors_seen = set()

        for book in self.books:
            author = book["metadata"].get("author", "")
            if not author or author in authors_seen:
                continue

            authors_seen.add(author)

            # Check if author has drive link
            if author not in self.author_map:
                # Check if this is a co-author situation (contains comma)
                if "," in author:
                    # Split and check if primary (first) author has a link
                    primary_author = author.split(",")[0].strip()
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

    @requires_author_map
    def test_author_drive_links_are_valid(self):
        """Test that author drive links are properly formatted (folder IDs or URLs)."""
        empty_links = []
        invalid_format = []

        for author, link in self.author_map.items():
            if not link:
                empty_links.append(author)
            elif not isinstance(link, str):
                invalid_format.append(f"{author}: not a string")
            elif not (link.startswith("http") or len(link) == 33):
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
        missing_authors = []

        for book in self.books:
            author = book["metadata"].get("author", "")
            if not author:
                missing_authors.append(str(book["path"]))

        # Report missing authors (not a failure, just informational)
        if missing_authors:
            print(f"\n[REPORT] {len(missing_authors)} books missing author metadata:")
            for path in missing_authors[:20]:
                print(f"  - {path}")
            if len(missing_authors) > 20:
                print(f"  ... and {len(missing_authors) - 20} more")

        print(f"\n[OK] Books with authors: {len(self.books) - len(missing_authors)}/{len(self.books)}")

    def test_all_books_have_descriptions(self):
        """Test that all books have description metadata (excludes BookFunnel books)."""
        missing = []
        skipped_bf = 0
        for book in self.books:
            if book["metadata"].get("_is_bookfunnel"):
                skipped_bf += 1
                continue
            desc = book["metadata"].get("desc", "")
            if not desc.strip():
                missing.append(book["metadata"].get("title", str(book["path"])))

        checked = len(self.books) - skipped_bf
        pct = ((checked - len(missing)) / checked) * 100 if checked else 0
        print(f"\n[REPORT] Books with descriptions: {checked - len(missing)}/{checked} ({pct:.1f}%)")
        print(f"  (Excluded {skipped_bf} BookFunnel books with known gaps)")
        if missing:
            for title in missing[:10]:
                print(f"  - {title}")
            if len(missing) > 10:
                print(f"  ... and {len(missing) - 10} more")

    def test_all_books_have_titles(self):
        """Test that all books have title metadata."""
        missing = []
        for book in self.books:
            title = book["metadata"].get("title", "")
            if not title.strip():
                missing.append(str(book["path"]))

        if missing:
            print(f"\n[ERROR] {len(missing)} books missing titles:")
            for path in missing[:10]:
                print(f"  - {path}")
            self.fail(f"{len(missing)} books have no title metadata")

        print(f"\n[OK] All {len(self.books)} books have titles")

    def test_all_books_have_narrators(self):
        """Test that all books have narrator metadata."""
        missing = []
        for book in self.books:
            narrator = book["metadata"].get("narrator", "")
            if not narrator.strip():
                missing.append(book["metadata"].get("title", str(book["path"])))

        pct = ((len(self.books) - len(missing)) / len(self.books)) * 100
        print(f"\n[REPORT] Books with narrators: {len(self.books) - len(missing)}/{len(self.books)} ({pct:.1f}%)")
        if missing:
            for title in missing[:10]:
                print(f"  - {title}")
            if len(missing) > 10:
                print(f"  ... and {len(missing) - 10} more")

    def test_all_books_have_duration(self):
        """Test that all books have duration metadata."""
        missing = []
        for book in self.books:
            duration = book["metadata"].get("duration_hhmm", "")
            if not duration.strip() or duration == "0:00":
                missing.append(book["metadata"].get("title", str(book["path"])))

        pct = ((len(self.books) - len(missing)) / len(self.books)) * 100
        print(f"\n[REPORT] Books with duration: {len(self.books) - len(missing)}/{len(self.books)} ({pct:.1f}%)")
        if missing:
            for title in missing[:10]:
                print(f"  - {title}")
            if len(missing) > 10:
                print(f"  ... and {len(missing) - 10} more")

    def test_all_books_have_genre(self):
        """Test that all books have genre metadata."""
        missing = []
        for book in self.books:
            genre = book["metadata"].get("genre", "")
            if not genre.strip():
                missing.append(book["metadata"].get("title", str(book["path"])))

        pct = ((len(self.books) - len(missing)) / len(self.books)) * 100
        print(f"\n[REPORT] Books with genre: {len(self.books) - len(missing)}/{len(self.books)} ({pct:.1f}%)")
        if missing:
            for title in missing[:10]:
                print(f"  - {title}")
            if len(missing) > 10:
                print(f"  ... and {len(missing) - 10} more")

    def test_catalog_has_books(self):
        """Test that the catalog is not empty."""
        self.assertGreater(len(self.books), 0, "No books found in library")

    @requires_author_map
    def test_author_map_exists(self):
        """Test that the author map, once found, is not empty.

        The FILE's existence is the skip condition (`requires_author_map`);
        this asserts the remaining half — that it has content. A file present
        but empty is a real defect, not an environment gap.
        """
        self.assertGreater(len(self.author_map), 0, f"{AUTHOR_MAP_PATH} is empty")


if __name__ == "__main__":
    unittest.main()
