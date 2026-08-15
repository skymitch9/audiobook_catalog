"""Tests for the universe-stamping / series-gap-stamping WIRING added on top
of the pure lookups (app/core/universes.py::universe_for,
app/core/series_gaps.py::compute_series_gaps).

app/main.py's step 0b calls a single hook, app.core.reference_stamps.
stamp_reference_data_safe(rows) — split out precisely so main() stays under
the repo's flake8 complexity ceiling (see that module's own docstring, which
mirrors app/library_link.py's stamp_after_build/_safe split). These tests
exercise THAT module directly: it actually lands the right value on a row,
survives a real write_csv() round-trip, and reaches the HTML build's data-*
attribute set. See tests/test_universes.py for the lookup's own contract
tests and tests/test_series_gaps.py for compute_series_gaps()'s.

Reuses tests/test_universes.py's skip mechanism: universe_for() needs the
catalog-platform checkout, found via app.core.universes.find_platform_dir(),
and these tests skip (not fail) when it's absent so a checkout without the
sibling repo can still run the suite.
"""
from __future__ import annotations

import csv
import tempfile
from pathlib import Path

import pytest

from app.core import universes as uv
from app.core.reference_stamps import stamp_reference_data_safe, stamp_series_gaps, stamp_universe
from app.core.series_gaps import compute_series_gaps
from app.web.html_builder import _book_data_attrs, _table_rows_html, _universe_filter_options
from app.writers import CSV_FIELDNAMES, write_csv

PLATFORM_DIR, _TRIED = uv.find_platform_dir()

requires_platform = pytest.mark.skipif(
    PLATFORM_DIR is None,
    reason=f"catalog-platform not found (tried: {'; '.join(_TRIED)}). Set {uv.ENV_VAR}.",
)


@pytest.fixture(autouse=True)
def _restore_default_list():
    yield
    uv.reload_universes()


# --------------------------------------------------------------------------- #
# stamp_universe() / stamp_reference_data_safe() actually land results on rows
# --------------------------------------------------------------------------- #


@requires_platform
def test_a_known_universe_book_gets_stamped():
    rows = [{"title": "Warbreaker", "series": ""}]
    stamp_universe(rows)
    assert rows[0]["universe"] == "The Cosmere"


@requires_platform
def test_an_ordinary_book_gets_stamped_with_an_empty_string_not_none():
    rows = [{"title": "Some Book Nobody Wrote", "series": "Some Series Nobody Wrote"}]
    stamp_universe(rows)
    assert rows[0]["universe"] == ""
    assert rows[0]["universe"] is not None


def test_stamping_never_raises_when_the_list_is_absent(tmp_path):
    uv.reload_universes(tmp_path / "nope.json")
    rows = [{"title": "Warbreaker", "series": ""}]
    stamp_universe(rows)
    assert rows[0]["universe"] == ""


@requires_platform
def test_stamp_reference_data_safe_stamps_both_fields_on_every_row():
    rows = [
        {"title": "Warbreaker", "series": "", "series_index_display": ""},
        {"title": "Ordinary Book 1", "series": "Ordinary Series", "series_index_display": "1"},
        {"title": "Ordinary Book 2", "series": "Ordinary Series", "series_index_display": "3"},
    ]
    stamp_reference_data_safe(rows)
    assert rows[0]["universe"] == "The Cosmere"
    assert rows[0]["series_gap"] == ""  # only one book, own series, blank/no summary
    assert rows[1]["universe"] == ""
    assert rows[1]["series_gap"] == "Volumes 1, 3 owned — gap: 2"
    assert rows[2]["series_gap"] == "Volumes 1, 3 owned — gap: 2"


def test_stamp_reference_data_safe_survives_a_broken_universes_list(tmp_path, capsys):
    """A malformed universes.json must not stop series_gap from stamping."""
    uv.reload_universes(tmp_path / "nope.json")
    rows = [
        {"title": "Book 1", "series": "Some Series", "series_index_display": "1"},
        {"title": "Book 2", "series": "Some Series", "series_index_display": "3"},
    ]
    stamp_reference_data_safe(rows)
    assert rows[0]["universe"] == ""
    assert rows[0]["series_gap"] == "Volumes 1, 3 owned — gap: 2"


def test_stamp_series_gaps_alone_has_no_universes_dependency():
    """Series gaps must compute correctly even when universes.py has never
    been touched — proving the two stamps are genuinely independent."""
    rows = [
        {"title": "Book 1", "series": "Solo Series", "series_index_display": "1"},
        {"title": "Book 2", "series": "Solo Series", "series_index_display": "2"},
    ]
    stamp_series_gaps(rows)
    assert rows[0]["series_gap"] == "Volumes 1-2 owned"
    assert "universe" not in rows[0]


# --------------------------------------------------------------------------- #
# The stamped value survives a real write_csv() round-trip
# --------------------------------------------------------------------------- #


def test_universe_and_series_gap_are_csv_columns():
    assert "universe" in CSV_FIELDNAMES
    assert "series_gap" in CSV_FIELDNAMES
    # Existing columns must not have been reordered or dropped.
    assert CSV_FIELDNAMES[:12] == [
        "title", "series", "series_index_display", "series_index_sort",
        "author", "narrator", "year", "genre", "duration_hhmm",
        "cover_href", "companion_files", "desc",
    ]
    assert CSV_FIELDNAMES.index("library_work_id") < CSV_FIELDNAMES.index("universe")


@requires_platform
def test_universe_reaches_the_written_csv_file():
    rows = [{"title": "Warbreaker", "series": "", "series_gap": ""}]
    stamp_universe(rows)
    with tempfile.TemporaryDirectory() as d:
        out_path = Path(d) / "out.csv"
        write_csv(rows, out_path)
        with open(out_path, encoding="utf-8") as f:
            written = list(csv.DictReader(f))
    assert written[0]["universe"] == "The Cosmere"


def test_series_gap_reaches_the_written_csv_file():
    rows = [
        {"title": "Book 1", "series": "Gapped Series", "series_index_display": "1"},
        {"title": "Book 2", "series": "Gapped Series", "series_index_display": "3"},
    ]
    gaps = compute_series_gaps(rows)
    for row in rows:
        row["series_gap"] = gaps.get(row["series"], "")
    with tempfile.TemporaryDirectory() as d:
        out_path = Path(d) / "out.csv"
        write_csv(rows, out_path)
        with open(out_path, encoding="utf-8") as f:
            written = list(csv.DictReader(f))
    assert written[0]["series_gap"] == "Volumes 1, 3 owned — gap: 2"
    assert written[1]["series_gap"] == "Volumes 1, 3 owned — gap: 2"


# --------------------------------------------------------------------------- #
# The stamped value reaches the HTML build (modal contract + filter idiom)
# --------------------------------------------------------------------------- #


def test_book_data_attrs_emits_universe_and_series_gap():
    row = {"title": "Warbreaker", "universe": "The Cosmere", "series_gap": "Volumes 1-6, 8 owned — gap: 7"}
    attrs = _book_data_attrs(row)
    assert 'data-universe="The Cosmere"' in attrs
    assert 'data-series-gap="Volumes 1-6, 8 owned' in attrs


def test_table_row_carries_data_universe_for_the_filter():
    rows = [{"title": "Warbreaker", "universe": "The Cosmere"}, {"title": "No Universe Book", "universe": ""}]
    html = _table_rows_html(rows)
    assert '<tr data-universe="The Cosmere">' in html
    assert '<tr data-universe="">' in html


def test_universe_filter_options_lists_only_universes_present_in_these_rows():
    rows = [
        {"title": "A", "universe": "The Cosmere"},
        {"title": "B", "universe": "Runnerverse"},
        {"title": "C", "universe": ""},
    ]
    options = _universe_filter_options(rows)
    assert '_universe:The Cosmere|filter' in options
    assert '_universe:Runnerverse|filter' in options
    assert '_universe_clear|filter' in options
    # Only universes actually present — never the full platform list.
    assert "Willverse" not in options


def test_universe_filter_options_is_empty_when_no_row_has_a_universe():
    rows = [{"title": "A", "universe": ""}, {"title": "B"}]
    assert _universe_filter_options(rows) == ""


if __name__ == "__main__":
    import unittest

    unittest.main()
