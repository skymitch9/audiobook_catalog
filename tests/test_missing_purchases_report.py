"""app/tools/missing_purchases_report.py — the daily full-library look.

Why: the 15-minute audit sees only the newest 50 purchases, so a burst of 50+
adds hid two older gaps for weeks. The properties pinned:

  1. it covers OLD purchases the top-50 window cannot see;
  2. it uses the same matcher and the same licence-refused exclusions;
  3. a profile with no rows is a FAILED report, never a shorter list;
  4. the Discord lines stay under the message limit and name the overflow.

Nothing here contacts Audible: ``collect`` is not called, ``summarise`` is pure.
"""

from __future__ import annotations

from app.tools import audit_new_purchases as anp
from app.tools import missing_purchases_report as mpr


def _row(title, date, asin, profile="skylar"):
    return {"title_short": title, "title": title, "author": "A", "purchase_date": date,
            "asin": asin, "profile": profile}


def _library(n_recent=60):
    """60 recent owned purchases plus one OLD gap below the top-50 window."""
    rows = [_row(f"Owned Book {i}", f"2026-10-{1 + i % 28:02d}", f"R{i}") for i in range(n_recent)]
    rows.append(_row("The Forgotten One", "2025-01-02", "OLD1", "samantha"))
    rows.append(_row("Paige Pick", "2026-11-01", "P1", "paige"))
    return rows


CATALOG = {anp.norm(f"Owned Book {i}") for i in range(60)} | {anp.norm("Paige Pick")}


def test_the_full_check_sees_an_old_gap_the_top_50_window_cannot():
    books = _library()
    assert anp.missing_purchases(books, CATALOG, top=50) == []
    got = mpr.summarise(books, CATALOG, skip_asins={})
    assert got["ok"] is True and got["covered_all"] is True
    assert got["items"] == [{"date": "2025-01-02", "title": "The Forgotten One",
                             "asin": "OLD1", "profile": "samantha"}]
    assert got["checked"] == len(books)


def test_licence_refused_titles_stay_excluded():
    got = mpr.summarise(_library(), CATALOG, skip_asins={"OLD1": {}})
    assert got["items"] == []


def test_a_profile_with_no_rows_fails_the_report_rather_than_shrinking_it():
    books = [b for b in _library() if b["profile"] != "paige"]
    got = mpr.summarise(books, CATALOG, skip_asins={})
    assert got["ok"] is False and "paige" in got["error"]


def test_a_library_bigger_than_the_cap_says_it_was_not_covered():
    got = mpr.summarise(_library(), CATALOG, skip_asins={}, top=10)
    assert got["covered_all"] is False and got["checked"] == 10


def test_missing_purchases_keeps_its_old_date_title_shape():
    books = [_row("Gap", "2026-11-02T10:00:00", "G1")]
    assert anp.missing_purchases(books, set(), top=5) == [("2026-11-02", "Gap")]
    assert anp.missing_purchase_rows(books, set(), top=5)[0]["asin"] == "G1"


def test_discord_lines_stay_under_the_limit_and_name_the_overflow():
    items = [{"date": "2026-01-01", "title": "X" * 80, "asin": f"A{i}", "profile": "skylar"}
             for i in range(60)]
    lines = mpr.discord_lines(items, {"A0"})
    assert sum(len(ln) + 1 for ln in lines) < 2000
    assert "🆕" in lines[0] and "🆕" not in lines[1]
    assert "more in output_files/missing_purchases_report.json" in lines[-1]
