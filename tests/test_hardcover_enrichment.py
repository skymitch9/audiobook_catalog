import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.enrich.hardcover import (
    HardcoverCache,
    HardcoverMatch,
    apply_cached_fields_to_row,
    apply_match_to_row,
    cache_key_for_row,
    choose_best_match,
    score_candidate,
)


class HardcoverEnrichmentTests(unittest.TestCase):
    def test_cache_key_is_stable_for_same_book(self):
        row = {"title": "Project Hail Mary", "author": "Andy Weir", "series": "", "duration_hhmm": "16:10"}
        same = {"author": "Andy Weir", "duration_hhmm": "16:10", "title": "Project Hail Mary", "series": ""}
        self.assertEqual(cache_key_for_row(row), cache_key_for_row(same))

    def test_score_prefers_matching_title_author_and_duration(self):
        row = {"title": "Project Hail Mary", "author": "Andy Weir", "duration_hhmm": "16:10"}
        good = {"title": "Project Hail Mary", "authors": ["Andy Weir"], "audio_seconds": 58200, "has_audiobook": True}
        bad = {"title": "The Martian", "authors": ["Andy Weir"], "audio_seconds": 36000, "has_audiobook": True}
        self.assertGreater(score_candidate(row, good), score_candidate(row, bad))

    def test_choose_best_match_returns_highest_candidate(self):
        row = {"title": "The Way of Kings", "author": "Brandon Sanderson", "duration_hhmm": "45:30"}
        candidates = [
            {"title": "Words of Radiance", "authors": ["Brandon Sanderson"], "audio_seconds": 175000, "has_audiobook": True},
            {"title": "The Way of Kings", "authors": ["Brandon Sanderson"], "audio_seconds": 163800, "has_audiobook": True},
        ]
        match = choose_best_match(row, candidates)
        self.assertIsNotNone(match)
        self.assertEqual(match.candidate["title"], "The Way of Kings")

    def test_apply_match_adds_hardcover_fields_without_overwriting_local_tags(self):
        row = {
            "title": "Book",
            "author": "Local Author",
            "desc": "Local desc",
            "year": "2020",
            "genre": "Fantasy",
            "series": "Local Series",
        }
        match = HardcoverMatch(
            candidate={
                "id": 123,
                "slug": "book",
                "rating": 4.25,
                "ratings_count": 99,
                "has_audiobook": True,
                "audio_seconds": 36000,
                "description": "Remote desc",
                "release_year": "2021",
                "genres": ["Sci-Fi"],
                "series": ["Remote Series"],
                "source": "test",
            },
            confidence=0.91,
        )
        out = apply_match_to_row(row, match)
        self.assertEqual(out["desc"], "Local desc")
        self.assertEqual(out["year"], "2020")
        self.assertEqual(out["genre"], "Fantasy")
        self.assertEqual(out["series"], "Local Series")
        self.assertEqual(out["hardcover_book_id"], "123")
        self.assertEqual(out["hardcover_series"], "Remote Series")
        self.assertTrue(out["hardcover_url"].endswith("/book"))
        self.assertEqual(out["hardcover_match_confidence"], "0.91")

    def test_apply_match_fills_blank_local_fields_only(self):
        row = {"title": "Book", "author": "Local Author", "desc": "", "year": "", "genre": "", "series": ""}
        match = HardcoverMatch(
            candidate={
                "id": 123,
                "slug": "book",
                "description": "Remote desc",
                "release_year": "2021",
                "genres": ["Sci-Fi"],
                "series": ["Remote Series"],
                "source": "test",
            },
            confidence=0.91,
        )
        out = apply_match_to_row(row, match)
        self.assertEqual(out["desc"], "Remote desc")
        self.assertEqual(out["year"], "2021")
        self.assertEqual(out["genre"], "Sci-Fi")
        self.assertEqual(out["series"], "Remote Series")
        self.assertEqual(out["hardcover_series"], "Remote Series")

    def test_cache_persists_lookup_fields(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "hardcover.json"
            cache = HardcoverCache(path)
            cache.set("key", {"status": "matched", "fields": {"hardcover_book_id": "123"}})
            cache.flush()

            reloaded = HardcoverCache(path)
            self.assertEqual(reloaded.get("key")["fields"]["hardcover_book_id"], "123")

    def test_cached_fill_fields_do_not_overwrite_local_values(self):
        row = {"title": "Book", "desc": "Local desc", "year": "2020", "genre": "Fantasy", "series": "Local Series"}
        fields = {
            "hardcover_book_id": "123",
            "desc": "Remote desc",
            "year": "2021",
            "genre": "Sci-Fi",
            "series": "Remote Series",
        }
        out = apply_cached_fields_to_row(row, fields)
        self.assertEqual(out["desc"], "Local desc")
        self.assertEqual(out["year"], "2020")
        self.assertEqual(out["genre"], "Fantasy")
        self.assertEqual(out["series"], "Local Series")
        self.assertEqual(out["hardcover_book_id"], "123")

    def test_score_candidate_without_has_audiobook_field(self):
        """Candidates from the API no longer include has_audiobook; scoring must not crash."""
        row = {"title": "Dune", "author": "Frank Herbert", "duration_hhmm": "21:30"}
        candidate = {"title": "Dune", "authors": ["Frank Herbert"], "audio_seconds": 77400}
        score = score_candidate(row, candidate)
        self.assertGreater(score, 0.0)

    def test_score_candidate_has_audiobook_absent_vs_present_same_title(self):
        """Removing has_audiobook from the candidate does not change the title/author/duration score."""
        row = {"title": "Dune", "author": "Frank Herbert", "duration_hhmm": "21:30"}
        with_flag = {"title": "Dune", "authors": ["Frank Herbert"], "audio_seconds": 77400, "has_audiobook": True}
        without_flag = {"title": "Dune", "authors": ["Frank Herbert"], "audio_seconds": 77400}
        self.assertEqual(score_candidate(row, with_flag), score_candidate(row, without_flag))


if __name__ == "__main__":
    unittest.main()
