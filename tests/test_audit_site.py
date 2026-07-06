"""
Tests for app/tools/audit_site.py — the promote-to-prod audit gate.

These run against synthetic fixtures (no audio library needed), so unlike
test_catalog_completeness.py they exercise the real logic in CI.
"""

import csv
import json
import tempfile
import unittest
from pathlib import Path

from app.tools.audit_site import audit, load_embedded_author_map, resolve_author_link


def write_catalog(site_dir: Path, rows: list) -> None:
    fields = ["title", "author", "narrator", "cover_href"]
    with open(site_dir / "catalog.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


class AuditSiteTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.site = self.root / "site"
        (self.site / "covers").mkdir(parents=True)
        self.map_path = self.root / "author_drive_map.json"
        self.excl_path = self.root / "audit_exclusions.json"

    def tearDown(self):
        self._tmp.cleanup()

    def make_cover(self, name: str) -> str:
        href = f"covers/{name}"
        (self.site / href).write_bytes(b"jpg")
        return href

    def good_row(self, title="Book One", author="Jane Doe", narrator="Sam Reader"):
        return {
            "title": title,
            "author": author,
            "narrator": narrator,
            "cover_href": self.make_cover(f"{title}.jpg"),
        }

    def write_map(self, mapping):
        self.map_path.write_text(json.dumps(mapping), encoding="utf-8")

    def write_exclusions(self, exclusions):
        self.excl_path.write_text(json.dumps(exclusions), encoding="utf-8")

    def run_audit(self):
        return audit(self.site, self.map_path, self.excl_path)

    # ---- passing baseline ----

    def test_clean_catalog_passes(self):
        write_catalog(self.site, [self.good_row()])
        self.write_map({"Jane Doe": "1" * 33})
        self.assertEqual(self.run_audit(), 0)

    # ---- author / narrator ----

    def test_missing_narrator_fails(self):
        write_catalog(self.site, [self.good_row(narrator="")])
        self.write_map({"Jane Doe": "1" * 33})
        self.assertEqual(self.run_audit(), 1)

    def test_missing_author_fails(self):
        write_catalog(self.site, [self.good_row(author="")])
        self.write_map({"Jane Doe": "1" * 33})
        self.assertEqual(self.run_audit(), 1)

    def test_excluded_title_skips_narrator_check(self):
        write_catalog(self.site, [self.good_row(narrator="")])
        self.write_map({"Jane Doe": "1" * 33})
        self.write_exclusions({"narrator": {"titles": ["Book One"]}})
        self.assertEqual(self.run_audit(), 0)

    # ---- covers ----

    def test_missing_cover_file_fails(self):
        row = self.good_row()
        (self.site / row["cover_href"]).unlink()
        write_catalog(self.site, [row])
        self.write_map({"Jane Doe": "1" * 33})
        self.assertEqual(self.run_audit(), 1)

    def test_empty_cover_href_fails(self):
        row = self.good_row()
        row["cover_href"] = ""
        write_catalog(self.site, [row])
        self.write_map({"Jane Doe": "1" * 33})
        self.assertEqual(self.run_audit(), 1)

    # ---- drive links ----

    def test_unmapped_author_fails(self):
        write_catalog(self.site, [self.good_row(author="Nobody Mapped")])
        self.write_map({"Jane Doe": "1" * 33})
        self.assertEqual(self.run_audit(), 1)

    def test_unmapped_author_passes_when_excluded(self):
        write_catalog(self.site, [self.good_row(author="Nobody Mapped")])
        self.write_map({"Jane Doe": "1" * 33})
        self.write_exclusions({"drive_links": {"authors": ["Nobody Mapped"]}})
        self.assertEqual(self.run_audit(), 0)

    def test_author_exclusion_is_case_insensitive(self):
        write_catalog(self.site, [self.good_row(author="Nobody Mapped")])
        self.write_map({"Jane Doe": "1" * 33})
        self.write_exclusions({"drive_links": {"authors": ["nobody mapped"]}})
        self.assertEqual(self.run_audit(), 0)

    def test_empty_map_link_counts_as_unmapped(self):
        write_catalog(self.site, [self.good_row()])
        self.write_map({"Jane Doe": ""})
        self.assertEqual(self.run_audit(), 1)

    # ---- resolution mirrors the site's app.js ----

    def test_resolve_exact_match(self):
        self.assertTrue(resolve_author_link("Jane Doe", {"Jane Doe": "abc"}))

    def test_resolve_case_insensitive_full_string(self):
        self.assertTrue(resolve_author_link("jane doe", {"Jane Doe ": "abc"}))

    def test_resolve_does_not_split_coauthors(self):
        # The site matches on the FULL author string only — "Jane Doe, Bob Co"
        # is NOT resolved by a "Jane Doe" entry.
        self.assertFalse(resolve_author_link("Jane Doe, Bob Co", {"Jane Doe": "abc"}))

    def test_resolve_empty_link_is_unresolved(self):
        self.assertFalse(resolve_author_link("Jane Doe", {"Jane Doe": "  "}))

    # ---- embedded map (site/index.html) is the shipped source of truth ----

    def write_index_with_map(self, mapping):
        (self.site / "index.html").write_text(
            '<html><body><script type="application/json" id="ab-author-map-json">'
            + json.dumps(mapping)
            + "</script></body></html>",
            encoding="utf-8",
        )

    def test_embedded_map_is_preferred_over_repo_map(self):
        # Author only in the embedded map -> passes even with empty repo map
        write_catalog(self.site, [self.good_row()])
        self.write_map({})
        self.write_index_with_map({"Jane Doe": "1" * 33})
        self.assertEqual(self.run_audit(), 0)

    def test_repo_mapped_but_not_embedded_warns_not_fails(self):
        # Mapped in author_drive_map.json but site not rebuilt yet -> pass
        write_catalog(self.site, [self.good_row()])
        self.write_map({"Jane Doe": "1" * 33})
        self.write_index_with_map({"Someone Else": "2" * 33})
        self.assertEqual(self.run_audit(), 0)

    def test_unmapped_in_both_fails_even_with_embedded_map(self):
        write_catalog(self.site, [self.good_row()])
        self.write_map({"Someone Else": "2" * 33})
        self.write_index_with_map({"Someone Else": "2" * 33})
        self.assertEqual(self.run_audit(), 1)

    def test_load_embedded_map_missing_file_returns_none(self):
        self.assertIsNone(load_embedded_author_map(self.site / "index.html"))

    def test_load_embedded_map_no_block_returns_none(self):
        (self.site / "index.html").write_text("<html>no map here</html>", encoding="utf-8")
        self.assertIsNone(load_embedded_author_map(self.site / "index.html"))

    # ---- stale exclusions warn but do not fail ----

    def test_stale_exclusion_does_not_fail(self):
        write_catalog(self.site, [self.good_row()])
        self.write_map({"Jane Doe": "1" * 33})
        self.write_exclusions({"drive_links": {"authors": ["Gone Author"]}})
        self.assertEqual(self.run_audit(), 0)

    # ---- structural failures ----

    def test_empty_catalog_fails(self):
        write_catalog(self.site, [])
        self.write_map({"Jane Doe": "1" * 33})
        self.assertEqual(self.run_audit(), 1)

    def test_missing_author_map_fails(self):
        write_catalog(self.site, [self.good_row()])
        self.assertEqual(self.run_audit(), 1)


if __name__ == "__main__":
    unittest.main()
