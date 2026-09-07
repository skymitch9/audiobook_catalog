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

✅ THE SIX HOLLOW TESTS NOW HAVE A FAILURE PATH — audit §4.2, fixed 2026-09-07.
Until today six of the eleven had no `assert`, no `self.fail` and no raise: they
walked the library, printed a `[REPORT]` block and passed unconditionally, so
`test_all_books_have_narrators` did not check that all books have narrators.
Each now asserts against a ceiling MEASURED on the live library that day (see
the FLOORS block below), so the suite is green now and goes red on regression.
Three that gate a NON-ZERO number were renamed to stop promising a check they
do not make: `..._authors_missing_drive_links_within_ceiling`,
`test_book_descriptions_within_ceiling`, `test_books_missing_genre_within_ceiling`.
The `[REPORT]` prints are kept — the numbers were the point of the originals.

✅ RUNNING THIS FILE NO LONGER WRITES TO `output_files/` — audit §4.4, fixed
2026-09-07. `extract_metadata()` still writes extracted cover art, but
`setUpClass` redirects `app.metadata.OUTPUT_DIR` to `COVER_OUTPUT_DIR` under the
OS temp dir for the life of the class, so a plain `pytest -q` on the pipeline box
no longer rewrites the real ~1,090 cover JPEGs. No app or pipeline code changed.
⚠️ It still READS the whole library and is the slowest setup in the estate
(~8.3 s), so it is still not free — but it is now side-effect free where it
matters. Verified by mtime sweep: 0 files under `output_files/` touched by a run.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

import app.metadata as app_metadata
from app.config import EXTS, OUTPUT_DIR, ROOT_DIR, SITE_DIR
from app.metadata import extract_metadata, walk_library

# ⚠️ AUDIT §4.4 (2026-09-07) — WHERE THE COVERS GO UNDER TEST.
#
# `extract_metadata()` -> `app.metadata._save_cover_for_file()` WRITES the
# embedded cover art of every book it reads. Before this constant existed it
# wrote into the real `output_files/covers/`, so a plain `pytest -q` on the
# pipeline box rewrote **1,090 cover JPEGs** — and `git status` could not see
# any of it, because `.gitignore:4` ignores `output_files/`. A clean
# `git status --short` is NOT proof that a suite is side-effect free; only an
# mtime sweep caught it.
#
# `setUpClass` redirects `app.metadata.OUTPUT_DIR` here for the whole class, so
# the writes land in the OS temp area instead. The app/pipeline code is
# UNCHANGED — this is a test-side patch of a module global, which works because
# `_save_cover_for_file` resolves `OUTPUT_DIR` at call time.
#
# Why a STABLE path rather than a fresh `mkdtemp()` per run: nothing here ever
# deletes a directory, so a per-run temp dir would accumulate ~200 MB a run.
# A single reused root is overwritten in place and stays bounded. The bytes are
# a deterministic function of the library, so two concurrent runs writing it
# write the same content.
COVER_OUTPUT_DIR = Path(tempfile.gettempdir()) / "audiobook_catalog_test_covers"


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


# ─────────────────────────────────────────────────────────────────────────────
# ⚠️ AUDIT §4.2 (2026-09-07) — THE MEASURED FLOORS.
#
# Until today six of these tests had NO failure path of any kind: no `assert`,
# no `self.fail`, no raise. They walked the real ~1,080-book library, built a
# list of problems, printed a `[REPORT]` block and passed unconditionally —
# `test_all_books_have_narrators` did not check that all books have narrators.
#
# Some of what they measure is a CODE invariant (every book has a title) and is
# asserted at 0 problems. The rest is a LIBRARY-CONTENT gap the owner already
# accepts — a book whose m4b carries no genre tag is not a bug in this repo —
# so those get an explicit floor instead of a demand for 100%.
#
# ⚠️ THE FLOORS ARE NOT ROUND NUMBERS OR GUESSES. Each is the count MEASURED on
# the date below against the live library, so the suite is green today and goes
# RED the moment coverage regresses. Raising a floor after a real improvement is
# welcome; LOWERING one is a deliberate act that needs a re-measurement and a
# note saying which books legitimately left the library.
#
# ⚠️ Every floor below is a CEILING ON PROBLEMS, not a floor on successes, and
# that is deliberate: an absolute floor on successes silently loosens as the
# library grows (1,090 books with covers still clears a floor of 1,090 once the
# library reaches 1,091), whereas a ceiling on the missing count stays exactly
# as tight tomorrow as it is today.
#
# MEASURED 2026-09-07 13:07 Phoenix, by running this file and reading its own
# `[REPORT]` lines. Library: **1,090 books**, 522 authors mapped.
#
#   books total ............................ 1090
#   missing embedded cover ................. 0     (100.0%)
#   cover_href naming no file .............. 0
#   missing author metadata ................ 0     (100.0%)
#   missing narrator metadata .............. 0     (100.0%)
#   missing duration metadata .............. 0     (100.0%)
#   missing genre metadata ................. 1     (99.9%)  — "Thesaurize"
#   missing description metadata ........... 86    (92.1%)  — 0 BookFunnel excl.
#   authors absent from author_drive_map ... 1              — "Funa"
# ─────────────────────────────────────────────────────────────────────────────
FLOORS_MEASURED_ON = "2026-09-07"

# Zero today, and zero is the real invariant: these four are produced for every
# book by the tag readers in `app/metadata.py`, so a non-zero count is a code or
# ingestion defect rather than a library-content gap.
MAX_BOOKS_MISSING_COVERS = 0
MAX_BOOKS_MISSING_AUTHORS = 0
MAX_BOOKS_MISSING_NARRATORS = 0
MAX_BOOKS_MISSING_DURATION = 0

# Non-zero today. These are CONTENT gaps in the source m4b tags — the owner
# accepts them, and demanding 100% would make the suite permanently red — so
# they are gated at exactly the count measured on FLOORS_MEASURED_ON.
MAX_BOOKS_MISSING_GENRE = 1  # "Thesaurize"
MAX_BOOKS_MISSING_DESCRIPTIONS = 86
MAX_AUTHORS_MISSING_DRIVE_LINKS = 1  # "Funa"


class TestCatalogCompleteness(unittest.TestCase):
    """Test that all books have required resources (covers, links)."""

    @classmethod
    def setUpClass(cls):
        """Load all books and author map once for all tests."""
        # ⚠️ AUDIT §4.4 — start the cover redirect BEFORE the first
        # `extract_metadata()` call, and hold it for the whole class. See
        # COVER_OUTPUT_DIR above for why this exists.
        COVER_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        cls.cover_output_dir = COVER_OUTPUT_DIR
        cls._cover_patch = mock.patch.object(app_metadata, "OUTPUT_DIR", COVER_OUTPUT_DIR)
        cls._cover_patch.start()
        try:
            cls._load_library()
        except BaseException:
            # unittest does NOT call tearDownClass when setUpClass raises, and
            # `SkipTest` below is one of the ways it raises — so the patch has
            # to be released here or it leaks into every later test module.
            cls._stop_cover_patch()
            raise

    @classmethod
    def _stop_cover_patch(cls):
        """Idempotent — called from both the setUpClass failure path and teardown."""
        patcher = getattr(cls, "_cover_patch", None)
        if patcher is not None:
            patcher.stop()
            cls._cover_patch = None

    @classmethod
    def tearDownClass(cls):
        cls._stop_cover_patch()

    @classmethod
    def _load_library(cls):
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
        """Every extracted cover_href resolves to a real file, and cover coverage holds its floor."""
        missing_covers = []
        extraction_errors = []

        for book in self.books:
            cover_href = book["metadata"].get("cover_href", "")
            if not cover_href:
                missing_covers.append(str(book["path"]))
            else:
                # ⚠️ The redirect target FIRST — under test that is where this
                # run's extraction actually wrote (audit §4.4). The real
                # OUTPUT_DIR and SITE_DIR stay as fallbacks so a cover staged
                # by an earlier pipeline run still counts; `stage_site_files()`
                # is what copies OUTPUT_DIR -> SITE_DIR.
                if not any(
                    (base / cover_href).exists()
                    for base in (self.cover_output_dir, OUTPUT_DIR, SITE_DIR)
                ):
                    extraction_errors.append(f"{book['path']} (cover file missing from all three cover dirs)")

        with_covers = len(self.books) - len(missing_covers)
        pct = (with_covers / len(self.books)) * 100
        print(f"\n[REPORT] Books with embedded covers: {with_covers}/{len(self.books)} ({pct:.1f}%)")
        if missing_covers:
            for path in missing_covers[:20]:
                print(f"  - {path}")
            if len(missing_covers) > 20:
                print(f"  ... and {len(missing_covers) - 20} more")

        if extraction_errors:
            print(f"\n[ERROR] {len(extraction_errors)} cover extraction failures:")
            for error in extraction_errors[:10]:
                print(f"  - {error}")
            if len(extraction_errors) > 10:
                print(f"  ... and {len(extraction_errors) - 10} more")

        # INVARIANT, measured 0 on 2026-09-07: a cover_href that names no file
        # is always a defect — the href is what the site renders.
        self.assertEqual(
            [], extraction_errors, f"{len(extraction_errors)} covers failed to extract properly"
        )

        self.assertLessEqual(
            len(missing_covers),
            MAX_BOOKS_MISSING_COVERS,
            f"cover coverage regressed: {len(missing_covers)} books have no embedded cover, "
            f"ceiling is {MAX_BOOKS_MISSING_COVERS} (measured {FLOORS_MEASURED_ON}). "
            f"First offenders: {missing_covers[:5]}",
        )

    def test_authors_missing_drive_links_within_ceiling(self):
        """Renamed 2026-09-07 (audit §4.2). It was `test_all_authors_have_drive_links`
        and it asserted nothing at all; 1 author legitimately has no map entry, so
        this is a THRESHOLD test and now says so in its name."""
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

        print(f"\n[REPORT] {len(missing_links)} authors missing from author_drive_map.json "
              f"(ceiling {MAX_AUTHORS_MISSING_DRIVE_LINKS}, measured {FLOORS_MEASURED_ON}):")
        for author in sorted(missing_links)[:20]:
            print(f"  - {author}")
        if len(missing_links) > 20:
            print(f"  ... and {len(missing_links) - 20} more")
        if missing_links:
            print("\n[TIP] Run 'python -m app.tools.generate_author_map' to add them")

        print(f"\n[OK] Author map loaded: {len(self.author_map)} authors mapped")

        self.assertLessEqual(
            len(missing_links),
            MAX_AUTHORS_MISSING_DRIVE_LINKS,
            f"author Drive-link coverage regressed: {len(missing_links)} authors are absent "
            f"from author_drive_map.json, ceiling is {MAX_AUTHORS_MISSING_DRIVE_LINKS} "
            f"(measured {FLOORS_MEASURED_ON}). New: {sorted(missing_links)[:5]}. "
            f"Fix with `python -m app.tools.generate_author_map`.",
        )

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

        if missing_authors:
            print(f"\n[REPORT] {len(missing_authors)} books missing author metadata:")
            for path in missing_authors[:20]:
                print(f"  - {path}")
            if len(missing_authors) > 20:
                print(f"  ... and {len(missing_authors) - 20} more")

        print(f"\n[OK] Books with authors: {len(self.books) - len(missing_authors)}/{len(self.books)}")

        # INVARIANT: 0 measured 2026-09-07. An author is what routes a book to
        # its Drive folder and its shelf, so a book without one is a real defect.
        self.assertLessEqual(
            len(missing_authors),
            MAX_BOOKS_MISSING_AUTHORS,
            f"{len(missing_authors)} books have no author metadata, ceiling is "
            f"{MAX_BOOKS_MISSING_AUTHORS} (measured {FLOORS_MEASURED_ON}): {missing_authors[:5]}",
        )

    def test_book_descriptions_within_ceiling(self):
        """Renamed 2026-09-07 (audit §4.2). It was `test_all_books_have_descriptions`
        and it asserted nothing; 86 books legitimately carry no description tag, so
        this is a THRESHOLD test and now says so in its name.
        (BookFunnel-sourced books are excluded — known metadata gaps.)"""
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

        self.assertLessEqual(
            len(missing),
            MAX_BOOKS_MISSING_DESCRIPTIONS,
            f"description coverage regressed: {len(missing)} books have no description, "
            f"ceiling is {MAX_BOOKS_MISSING_DESCRIPTIONS} (measured {FLOORS_MEASURED_ON}). "
            f"Sample: {missing[:5]}",
        )

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

        # INVARIANT: 0 measured 2026-09-07. The name of this test is now true.
        self.assertLessEqual(
            len(missing),
            MAX_BOOKS_MISSING_NARRATORS,
            f"narrator coverage regressed: {len(missing)} books have no narrator, ceiling is "
            f"{MAX_BOOKS_MISSING_NARRATORS} (measured {FLOORS_MEASURED_ON}). Sample: {missing[:5]}",
        )

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

        # INVARIANT: 0 measured 2026-09-07. ⚠️ `duration_hhmm` is load-bearing —
        # `catalog-twins` uses it to tell a duplicate EDITION from a SUBSTITUTION
        # (info/catalog-twins.md), so a book that loses it is a real defect.
        self.assertLessEqual(
            len(missing),
            MAX_BOOKS_MISSING_DURATION,
            f"duration coverage regressed: {len(missing)} books have no duration (or 0:00), "
            f"ceiling is {MAX_BOOKS_MISSING_DURATION} (measured {FLOORS_MEASURED_ON}). "
            f"Sample: {missing[:5]}",
        )

    def test_books_missing_genre_within_ceiling(self):
        """Renamed 2026-09-07 (audit §4.2). It was `test_all_books_have_genre` and it
        asserted nothing; 1 book legitimately carries no genre tag, so this is a
        THRESHOLD test and now says so in its name."""
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

        self.assertLessEqual(
            len(missing),
            MAX_BOOKS_MISSING_GENRE,
            f"genre coverage regressed: {len(missing)} books have no genre, ceiling is "
            f"{MAX_BOOKS_MISSING_GENRE} (measured {FLOORS_MEASURED_ON}). Sample: {missing[:5]}",
        )

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
