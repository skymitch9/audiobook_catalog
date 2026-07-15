"""Tests for the additions log (dated upload history, stable across file moves)."""

import json
import tempfile
import unittest
from pathlib import Path

from app.additions_log import book_key, load_log, update_additions_log
from app.web.html_builder import _recently_added_html, _upload_history_html


def row(title, author="Author", mtime=0):
    return {"title": title, "author": author, "file_mtime": mtime}


class AdditionsLogTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.site = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_first_run_logs_all_books_with_given_date(self):
        entries = update_additions_log([row("A"), row("B")], self.site, today="2026-07-15")
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[book_key("A", "Author")]["added"], "2026-07-15")
        self.assertEqual(entries[book_key("A", "Author")]["source"], "pipeline")

    def test_existing_dates_never_change(self):
        update_additions_log([row("A")], self.site, today="2026-07-01")
        entries = update_additions_log([row("A"), row("B")], self.site, today="2026-07-15")
        self.assertEqual(entries[book_key("A", "Author")]["added"], "2026-07-01")
        self.assertEqual(entries[book_key("B", "Author")]["added"], "2026-07-15")

    def test_moved_file_keeps_its_date(self):
        # A "move" only changes mtime; title|author is unchanged -> no new entry
        update_additions_log([row("A", mtime=100)], self.site, today="2026-07-01")
        entries = update_additions_log([row("A", mtime=999)], self.site, today="2026-07-15")
        self.assertEqual(entries[book_key("A", "Author")]["added"], "2026-07-01")

    def test_log_round_trips_through_disk(self):
        update_additions_log([row("A")], self.site, today="2026-07-01")
        self.assertEqual(load_log(self.site)[book_key("A", "Author")]["added"], "2026-07-01")

    def test_log_file_is_valid_json_with_entries_list(self):
        update_additions_log([row("A")], self.site, today="2026-07-01")
        with open(self.site / "additions_log.json", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data["entries"]), 1)


class RecentlyAddedRenderTestCase(unittest.TestCase):
    def test_log_date_outranks_mtime(self):
        # "Old" was bumped to a huge mtime (moved file); "New" has a newer log date
        rows = [row("Old", mtime=9999), row("New", mtime=1)]
        additions = {
            book_key("Old", "Author"): {"added": "2026-01-01"},
            book_key("New", "Author"): {"added": "2026-07-15"},
        }
        html = _recently_added_html(rows, additions, count=1)
        self.assertIn("New", html)
        self.assertNotIn("Old", html)

    def test_falls_back_to_mtime_without_log(self):
        rows = [row("Older", mtime=1), row("Newest", mtime=9)]
        html = _recently_added_html(rows, None, count=1)
        self.assertIn("Newest", html)

    def test_history_groups_by_date_newest_first(self):
        rows = [row("A"), row("B")]
        additions = {
            book_key("A", "Author"): {"added": "2026-01-01", "source": "git"},
            book_key("B", "Author"): {"added": "2026-07-15", "source": "pipeline"},
        }
        html = _upload_history_html(rows, additions)
        self.assertLess(html.index("2026-07-15"), html.index("2026-01-01"))

    def test_history_labels_baseline_group(self):
        rows = [row("A")]
        additions = {book_key("A", "Author"): {"added": "2025-10-08", "source": "baseline"}}
        self.assertIn("library baseline", _upload_history_html(rows, additions))
