# Test Coverage Analysis

## Current State

**57 of 58 tests pass** (1 import error in CI due to missing `mutagen` dependency).

| Metric | Value |
|---|---|
| Total source files | 22 |
| Total source LOC | ~2,357 |
| Test files | 7 (proper unittest) + 1 (script) |
| Test LOC | ~763 |
| Test-to-source ratio | 0.32 |

## Module-by-Module Coverage

### Well-Tested (3 modules, ~242 source LOC)

| Module | Source LOC | Test File | Tests | Notes |
|---|---|---|---|---|
| `app/core/index_utils.py` | 95 | `test_index_utils.py` | 16 | Good coverage of normalization and sort keys |
| `app/parsers/title.py` | 105 | `test_title_parsing.py` | 14 | Covers main patterns and exclusions |
| `app/core/people.py` | 42 | `test_people_normalization.py` | 13 | Good coverage of name normalization |

### Partially Tested (2 modules, ~334 source LOC)

| Module | Source LOC | Test File | Tests | What's Missing |
|---|---|---|---|---|
| `app/web/html_builder.py` | 174 | `test_html_builder.py` | 10 | Only tests `_esc()` and `_cover_button()`. Missing: `_row_cells`, `_card_html`, `_table_rows_html`, `_cards_html`, `_load_author_map`, `render_index_html` |
| `app/tools/send_discord_notification.py` | 160 | `test_discord_embeds.py` | 0 (script) | `test_discord_embeds.py` is a standalone script, not a `unittest.TestCase`. It runs at import time during test discovery and doesn't use assertions. |

### Completely Untested (17 modules, ~1,781 source LOC)

| Module | Source LOC | Complexity | Priority |
|---|---|---|---|
| **`app/metadata.py`** | **297** | **High** | **Critical** |
| **`app/tools/generate_stats.py`** | **542** | **High** | **High** |
| `app/tools/inspect_tags.py` | 162 | Medium | Low |
| `app/writers.py` | 141 | Medium | High |
| `app/tools/book_sort.py` | 127 | Medium | Medium |
| `app/parsers/title_patterns.py` | 123 | Low | Low (tested indirectly) |
| `app/tools/detect_new_books.py` | 95 | Medium | Medium |
| `app/main.py` | 88 | Medium | Medium |
| `app/tools/generate_author_map.py` | 80 | Medium | Low |
| `app/extractors/covers.py` | 46 | Medium | Medium |
| `app/extractors/tags.py` | 36 | Low | High |
| `app/config.py` | 26 | Low | Low |
| `app/core/keys.py` | 18 | Low | Low |

---

## Priority 1: `app/metadata.py` (Critical)

This is the core module of the entire application. Every audiobook flows through `extract_metadata()`, and data quality depends on its correctness. None of its 12 functions are tested.

### Functions needing tests

**`bytes_to_str(b)`** — Decodes bytes with encoding fallbacks (utf-8 → utf-16 → latin-1).
- Test with valid UTF-8, UTF-16, Latin-1 encoded bytes
- Test with malformed bytes that fall through to error-ignoring decode
- Test that leading/trailing whitespace is stripped

**`first_str(val)`** — Extracts a string from tag values that may be lists, bytes, or tuples.
- Test with `list` of strings, `list` of bytes, empty list
- Test with `tuple` input
- Test with `None`, plain string, integer

**`get_tag_any(tags, keys)`** — Retrieves the first non-empty tag value from a list of keys.
- Test with first key matching, second key matching, no keys matching
- Test with empty tag values (should skip to next key)

**`get_freeform_by_suffix(tags, suffixes)`** — Searches freeform `----:` keys by suffix.
- Test with matching freeform key containing `MP4FreeForm` data
- Test with matching key containing plain bytes
- Test with no matching keys
- Test that non-`----` prefixed keys are skipped

**`_cleanup_series(name)`** — Strips "series" suffix and normalizes whitespace.
- Test removing trailing "series"/"Series"
- Test stripping punctuation characters (dashes, colons, commas)
- Test that `None`/empty returns `None`

**`normalize_people_field(s)`** — Splits and normalizes author/narrator names.
- Already tested via `app/core/people.py` (duplicate code). The duplicate itself is a risk — the two copies could drift apart. Consider testing that both produce identical results, or removing the duplicate.

**`sec_to_hhmm(s)`** — Converts seconds to `H:MM` format.
- Test standard conversions: 3600→"1:00", 5400→"1:30", 90→"0:01"
- Test `None` and non-numeric input → returns `""`
- Test 0 → "0:00"

**`_html_to_plain_text(s)`** — Converts HTML descriptions to plain text.
- Test `<br>` → newline, `</p>` → double newline
- Test stripping `<script>`, `<style>`, HTML comments
- Test HTML entity unescaping (`&amp;` → `&`)
- Test collapsing 3+ consecutive newlines to 2
- Test `<i>`, `<b>`, `<ul>` tag removal
- Test empty/None input

**`_extract_description(tags)`** — Tries multiple tag sources for description.
- Test priority order: `ldes` → `desc` → `©cmt` → freeform
- Test that HTML is cleaned in all code paths
- Test `None` when no description tag is found

**`extract_metadata(path)`** — Main extraction function orchestrating all the above.
- Requires mocking `MP4()` (mutagen dependency)
- Test with vendor series tags (SRNM/SRSQ)
- Test fallback to freeform tags
- Test fallback to title parsing
- Test that all output dict keys are present and correctly typed

**`walk_library(root, exts)`** — Discovers audio files by extension.
- Test with a temp directory containing matching and non-matching files
- Test case-insensitive extension matching
- Test recursive discovery in subdirectories
- Test empty directory returns empty list

### Suggested test file: `tests/test_metadata.py`

```python
"""Tests for core metadata extraction functions."""
import unittest
from unittest.mock import MagicMock, patch
from app.metadata import (
    bytes_to_str, first_str, get_tag_any, get_freeform_by_suffix,
    _cleanup_series, sec_to_hhmm, _html_to_plain_text,
    _extract_description, walk_library,
)

class TestBytesToStr(unittest.TestCase):
    def test_utf8(self):
        self.assertEqual(bytes_to_str("hello".encode("utf-8")), "hello")

    def test_latin1_fallback(self):
        self.assertEqual(bytes_to_str("café".encode("latin-1")), "café")

    def test_strips_whitespace(self):
        self.assertEqual(bytes_to_str(b"  hello  "), "hello")

class TestSecToHhmm(unittest.TestCase):
    def test_one_hour(self):
        self.assertEqual(sec_to_hhmm(3600), "1:00")

    def test_none(self):
        self.assertEqual(sec_to_hhmm(None), "")

    def test_non_numeric(self):
        self.assertEqual(sec_to_hhmm("abc"), "")

class TestHtmlToPlainText(unittest.TestCase):
    def test_br_to_newline(self):
        self.assertIn("\n", _html_to_plain_text("line1<br>line2"))

    def test_script_removal(self):
        result = _html_to_plain_text("<script>alert('x')</script>text")
        self.assertEqual(result, "text")

    def test_entity_unescape(self):
        self.assertIn("&", _html_to_plain_text("Tom &amp; Jerry"))

    def test_empty(self):
        self.assertEqual(_html_to_plain_text(""), "")

# ... etc.
```

---

## Priority 2: `app/tools/generate_stats.py` (High)

At 542 LOC, this is the largest module in the project and has zero tests. It computes all catalog statistics and generates a full HTML page.

### Functions needing tests

**`parse_duration_to_minutes(duration_str)`** — Parses "HH:MM" to minutes.
- Test "10:30" → 630, "0:45" → 45, "100:00" → 6000
- Test empty string, `None`, missing colon → 0
- Test invalid values like "abc:def" → 0

**`calculate_stats(csv_path)`** — Computes all stats from a CSV file.
- Create a temp CSV with known data and verify all stat calculations
- Test empty CSV returns `{}`
- Test missing CSV returns `{}`
- Test correct counting of unique authors, narrators, series, genres
- Test duration categorization boundaries (< 5h, 5-10h, 11-15h, 16-24h, 25h+)
- Test top-N lists (top authors, narrators, etc.)
- Test insights calculations (books_per_author, series_percentage, etc.)

**`generate_stats_html(stats, generated_at)`** — Generates the HTML page.
- Test that output contains expected stat values
- Test that format_listening_time produces correct thresholds (years, months, weeks, days)

---

## Priority 3: `app/extractors/tags.py` and `app/extractors/covers.py` (High)

These are the refactored versions of functions also found in `metadata.py`. They should be tested to ensure correctness and to allow eventual removal of the duplicated code.

### `app/extractors/tags.py` (36 LOC)

**`get_tag_any(tags, keys)`** and **`get_freeform_by_suffix(tags, suffixes)`** — Same logic as in `metadata.py` but importing from `core.people` for helpers.

- Test identical behavior to the `metadata.py` versions
- Can share the same test cases

### `app/extractors/covers.py` (46 LOC)

**`save_cover_for_file(path)`** — Requires mocking `MP4()`.
- Test JPEG cover extraction (FORMAT_JPEG → `.jpg`)
- Test PNG cover extraction (FORMAT_PNG → `.png`)
- Test no cover returns `None`
- Test exception handling returns `None`
- Test correct output path construction

---

## Priority 4: `app/writers.py` (High)

This module handles all output generation. Bugs here silently produce incorrect catalog files.

### Functions needing tests

**`write_csv(rows, out_path)`** — Writes catalog CSV.
- Test with known rows, read back and verify header and data
- Test that all 11 fieldnames are written
- Test that the parent directory is created if missing
- Test with empty rows list

**`render_output_html(rows, out_path, ...)`** — Thin wrapper for `render_index_html`.
- Test that output file is created
- Test that CSV link and drive link are correctly injected

**`stage_site_files(...)`** — Multi-step site staging.
- Test that CSV is copied to site directory
- Test that index.html is generated in site directory
- Test behavior when covers directory doesn't exist (should not fail)

---

## Priority 5: `app/tools/detect_new_books.py` (Medium)

### Functions needing tests

**`parse_csv_content(content)`** — Parses CSV string into dicts.
- Test with valid CSV content
- Test with empty content → `[]`
- Test with header-only content → `[]`

**`main()` logic** — New book detection by comparing with git history.
- Mock `subprocess.run` for `git show` and test diff logic
- Test with no previous catalog (all books are "new")
- Test with identical catalogs (no new books)
- Test the 10-book limit for Discord output

---

## Priority 6: `app/tools/book_sort.py` (Medium)

### Functions needing tests

**`get_author_name(file_path)`** — Requires mocking `MP4()`.
- Test with a valid artist tag
- Test with missing artist tag → `None`
- Test title-case normalization
- Test that first author before comma is used

**`organize_by_author(root_dir, exts, ...)`** — File organization.
- Test with temp directory and mock files
- Test `dry_run=True` doesn't move files
- Test files already in correct folder are skipped
- Test that destination conflicts are skipped

---

## Priority 7: `test_discord_embeds.py` Refactor (Medium)

The current file is a standalone script that runs at import time during test discovery. It should be converted to a proper `unittest.TestCase`.

### What to test

**`create_embed(new_books_data, site_url)`**:
- Test with 0 new books → single "catalog updated" embed
- Test with 1 new book → 2 embeds (summary + book)
- Test with 10+ new books → capped at 10 embeds (1 summary + 9 books)
- Test that all embeds respect Discord limits (title ≤ 256, description ≤ 4096)
- Test cover URL construction from relative path
- Test embed with and without optional fields (narrator, series, year, genre, duration)

**`send_notification(webhook_url, embeds)`**:
- Mock `requests.post` and verify correct payload structure
- Test error handling on HTTP failure

---

## Structural Issues

### 1. Duplicated code between modules

`metadata.py` contains its own copies of `bytes_to_str`, `first_str`, `get_tag_any`, `get_freeform_by_suffix`, `_cleanup_series`, and `normalize_people_field`. These are also defined in `app/core/people.py`, `app/extractors/tags.py`, and `app/parsers/title.py`. If any tests are written for one copy, the other copies remain untested and at risk of diverging behavior.

### 2. `test_discord_embeds.py` runs code at import time

The file executes `create_embed()` at module level. During `unittest` test discovery, this code runs unconditionally, printing output. It should be wrapped in a `TestCase` class.

### 3. No mocking infrastructure for `mutagen`

Many modules import `mutagen.mp4.MP4` at module level. This makes `test_catalog_completeness.py` fail in environments without `mutagen` installed. A lightweight mock or fixture pattern would make these modules testable in CI.

### 4. `test_catalog_completeness.py` fails in CI

It imports `app.metadata` which requires `mutagen`. All its tests skip in CI anyway (no library present). This test should either:
- Be moved to a separate integration test suite
- Use conditional imports with proper skip decorators

---

## Recommended Test Priorities (by impact)

| Priority | New Test File | Covers Module | Est. Tests | Rationale |
|---|---|---|---|---|
| 1 | `tests/test_metadata.py` | `app/metadata.py` | 25-30 | Core data pipeline; most functions are pure and testable without mutagen by mocking MP4 |
| 2 | `tests/test_generate_stats.py` | `app/tools/generate_stats.py` | 15-20 | Largest untested module; stat calculations are pure functions easy to test |
| 3 | `tests/test_writers.py` | `app/writers.py` | 10-12 | Output correctness; CSV writing and site staging use filesystem (temp dirs) |
| 4 | `tests/test_discord_embeds.py` (rewrite) | `app/tools/send_discord_notification.py` | 8-10 | Convert script to proper TestCase with assertions |
| 5 | `tests/test_detect_new_books.py` | `app/tools/detect_new_books.py` | 6-8 | CSV parsing is pure; git interaction can be mocked |
| 6 | `tests/test_html_builder_extended.py` | `app/web/html_builder.py` | 8-10 | Extend existing tests to cover row/card/full render |
| 7 | `tests/test_book_sort.py` | `app/tools/book_sort.py` | 6-8 | File organization logic with mocked MP4 |

Implementing priorities 1-4 would bring the tested source LOC from ~242 to ~1,173, roughly a **4.8x increase** in coverage.
