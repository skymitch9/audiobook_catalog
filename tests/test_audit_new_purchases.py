"""Tests for the recent-purchase audit (top-N diff vs catalog)."""

import unittest

from app.tools.audit_new_purchases import missing_purchases, newest_purchases, norm


def book(title, purchase, author="Some Author"):
    return {"title_short": title, "purchase_date": purchase, "author": author}


class PurchaseAuditTestCase(unittest.TestCase):
    def test_norm_strips_punctuation_and_case(self):
        self.assertEqual(norm("Didn't I Say: To Make It?!"), "didn t i say to make it")

    def test_newest_sorts_by_purchase_date_and_drops_sample(self):
        books = [
            book("Old", "2024-01-01"),
            book("New", "2026-07-01"),
            {"title_short": "Welcome", "author": "OpenAudible"},
        ]
        top = newest_purchases(books, 2)
        self.assertEqual([b["title_short"] for b in top], ["New", "Old"])

    def test_missing_flags_unmatched_titles_only(self):
        books = [book("Rhythm of War", "2026-07-01"), book("Brand New Book", "2026-07-02")]
        catalog = {norm("Rhythm of War - Book Four of the Stormlight Archive")}
        missing = missing_purchases(books, catalog, top=50)
        self.assertEqual([t for _, t in missing], ["Brand New Book"])

    def test_substring_matching_works_both_directions(self):
        books = [book("Lessons in Chemistry - A Novel", "2026-07-01")]
        catalog = {norm("Lessons in Chemistry")}
        self.assertEqual(missing_purchases(books, catalog), [])

    def test_top_limits_the_window(self):
        books = [book("Missing Old", "2020-01-01"), book("Have New", "2026-07-01")]
        catalog = {norm("Have New")}
        self.assertEqual(missing_purchases(books, catalog, top=1), [])


if __name__ == "__main__":
    unittest.main()
