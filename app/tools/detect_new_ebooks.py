#!/usr/bin/env python3
"""
Detect new EBOOKS for the Discord post — the ebook twin of detect_new_books.py,
run by the same deploy.yml job, at the same moment (a promote), with the same
snapshot discipline. Owner, 2026-10-02: "lets use github actions, i want to
match the audiobook catalog perfectly".

What is different, and why each difference exists:

* INPUT. site/ebooks.json is gitignored (the shelf is permission-gated; owner:
  "I don't want people scraping my books") so a checkout has no ebook list.
  deploy.yml downloads the PUBLISHED gated manifest (`ebooks-gated/ebooks.json`,
  what readers actually see) to $EBOOKS_MANIFEST_PATH with a read-only
  credential, and this reads that file. Absent file = step skipped, never
  "zero ebooks".
* SNAPSHOT. last_catalog_snapshot.json is tracked and holds titles — fine for
  the public audiobook catalogue, NOT for ebooks: this repo is public. So
  last_ebook_snapshot.json stores sha256("title|author") only.
* LOGS. GitHub Actions logs of a public repo are public. This module prints
  COUNTS, never a title or an author.

Identity is "title|author" exactly as detect_new_books.py builds it, so a
re-filed ebook (new path, new anchor) is not news and a duplicate row is
announced once.

    python -m app.tools.detect_new_ebooks                    # writes new_ebooks.json
    python -m app.tools.detect_new_ebooks --update-snapshot  # after Discord confirmed
"""
import hashlib
import json
import os
import sys
from pathlib import Path

SNAPSHOT_PATH = Path("last_ebook_snapshot.json")
OUTPUT_PATH = Path("new_ebooks.json")


def manifest_path() -> Path:
    return Path(os.environ.get("EBOOKS_MANIFEST_PATH", "ebooks_manifest.json"))


def ebook_key(row: dict) -> str:
    """The same "title|author" string detect_new_books.py uses, hashed so the
    tracked snapshot does not list the household's ebooks by name."""
    raw = f"{row.get('title', '')}|{row.get('author', '')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_manifest_rows() -> list[dict] | None:
    try:
        with open(manifest_path(), "r", encoding="utf-8") as f:
            rows = json.load(f).get("ebooks")
        return rows if isinstance(rows, list) else None
    except Exception:
        return None


def load_snapshot() -> set | None:
    """Announced ebook keys, or None when there is no snapshot yet."""
    if not SNAPSHOT_PATH.exists():
        return None
    with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
        return set(json.load(f).get("ebook_keys", []))


def save_snapshot(keys: set) -> None:
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump({"ebook_keys": sorted(keys)}, f, indent=0)
        f.write("\n")


def audiobook_count() -> int:
    import csv
    try:
        with open("site/catalog.csv", "r", encoding="utf-8") as f:
            return sum(1 for _ in csv.DictReader(f))
    except Exception:
        return 0


def _gh_output(**values) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as gh:
            for k, v in values.items():
                gh.write(f"{k}={v}\n")


def main() -> int:
    rows = load_manifest_rows()
    if rows is None:
        print(f"No readable ebook manifest at {manifest_path()} — ebook post skipped")
        _gh_output(has_new_ebooks="false", new_ebook_count=0)
        return 0

    current = {ebook_key(r) for r in rows}
    previous = load_snapshot()

    # First run: baseline, announce nothing (what is already on the shelf is
    # not news) — the same rule detect_new_books.py follows.
    if previous is None:
        save_snapshot(current)
        print(f"First run: saving baseline ebook snapshot ({len(rows)} ebooks)")
        _gh_output(has_new_ebooks="false", new_ebook_count=0, baselined="true")
        return 0

    seen: set[str] = set()
    new: list[dict] = []
    for row in rows:
        key = ebook_key(row)
        if key in previous or key in seen:
            continue
        seen.add(key)
        new.append({
            "title": row.get("title", ""),
            "author": row.get("author", ""),
            "cover": row.get("cover_url") or "",
            "format": row.get("format", ""),
        })

    output = {
        "new_count": len(new),
        "total_count": audiobook_count(),
        # len(rows): the same number site/ebooks_status.json carries, which
        # the audiobook post prints — one ebook count, not two.
        "ebook_count": len(rows),
        "books": new[:10],  # Limit to 10 for Discord
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    if "--update-snapshot" in sys.argv:
        # previous | current, not current alone: a book that briefly left the
        # shelf (re-filed between two publishes) must not be re-announced.
        save_snapshot(previous | current)
        print("  Ebook snapshot updated.")

    # ⚠️ Counts only — this log is public.
    print(f"Found {len(new)} new ebooks (total: {len(rows)})")
    _gh_output(has_new_ebooks="true" if new else "false", new_ebook_count=len(new))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
