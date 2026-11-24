"""Daily FULL-library "missing purchases" report — report only, never downloads.

Why it exists (owner-approved 2026-11-23): the 15-minute purchase audit
(``purchase_audit`` -> ``auto_acquire``, default ``--top 50``) only looks at
the newest 50 purchases across all three Audible accounts. A burst of 50+ new
adds pushes every older un-downloaded title out of that window for good — two
titles went unfetched for weeks exactly that way, and nothing said so.

This module is the wide, slow, once-a-day look that the fast tick cannot
afford: the SAME matcher (``audit_new_purchases.missing_purchase_rows``) over
the whole library (``FULL_TOP``), the same licence-refused exclusions, and no
download path at all. ``purchase_audit`` runs ``collect`` in a bounded child
under the audible-cli interpreter once per calendar day and owns the state,
the report file and the "post only when the set changed" rule.

⚠️ A profile with NO rows is a failed report, not an empty account. If one
export fails, its missing titles would vanish from the set and come back the
next day — two false "the list changed" posts for one Audible hiccup. There is
deliberately no books.json fallback here either: a stale list is exactly what
this report exists to see past.

Usage:
    python -m app.tools.missing_purchases_report   # print the full list (contacts Audible)
"""

from __future__ import annotations

import json
import sys

from app.tools import audit_new_purchases as anp

# Larger than any household library will plausibly be (1,077 purchases across
# the three profiles, measured 2026-10-20). `collect` says when it was not.
FULL_TOP = 5000


def summarise(books: list[dict], catalog_titles, skip_asins, top: int = FULL_TOP,
              profiles=anp.AUDIBLE_PROFILES) -> dict:
    """Pure: the report for these rows. JSON-only, so it crosses the child
    boundary unchanged."""
    seen = {b.get("profile") for b in books}
    absent = [p for p in profiles if p not in seen]
    if absent:
        return {"ok": False,
                "error": f"no library rows exported for profile(s) {', '.join(absent)} — "
                         "not reporting a partial list"}
    rows = anp.missing_purchase_rows(books, catalog_titles, top, skip_asins=skip_asins)
    checked = len(anp.newest_purchases(books, top))
    purchases = len(anp.newest_purchases(books, len(books)))
    items = sorted(
        ({"date": anp.purchase_date(b), "title": b.get("title_short") or b.get("title") or "",
          "asin": b.get("asin") or "", "profile": b.get("profile") or ""} for b in rows),
        key=lambda i: (i["date"], i["title"]), reverse=True,
    )
    return {"ok": True, "checked": checked, "covered_all": purchases <= top, "items": items}


def collect(top: int = FULL_TOP) -> dict:
    """Fetch fresh exports for every profile and build the report. Contacts
    Audible (read-only library export); downloads nothing. Never raises."""
    try:
        from app.tools.license_refused import load as load_refused
        books = anp.audible_cli_books()
        return summarise(books, anp.load_catalog_titles(), load_refused(), top)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def asin_key(item: dict) -> str:
    """The identity used for "did the set change". A row with no ASIN (never
    seen in practice from audible-cli) falls back to its title."""
    return item.get("asin") or f"title:{item.get('title', '')}"


def discord_lines(items: list[dict], new_keys: set[str], limit_chars: int = 1700) -> list[str]:
    """One line per title, newest first, NEW ones marked, capped well under
    Discord's 2,000-character message limit."""
    lines: list[str] = []
    used = 0
    for i, it in enumerate(items):
        mark = "🆕 " if asin_key(it) in new_keys else ""
        line = f"- {mark}{it['date']} | {it['title']} ({it['profile']}, {it['asin']})"
        if used + len(line) > limit_chars:
            lines.append(f"- … +{len(items) - i} more in output_files/missing_purchases_report.json")
            break
        lines.append(line)
        used += len(line) + 1
    return lines


def main() -> int:
    report = collect()
    if not report.get("ok"):
        print(f"FAILED — {report.get('error')}")
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if report["items"] else 0


if __name__ == "__main__":
    sys.exit(main())
