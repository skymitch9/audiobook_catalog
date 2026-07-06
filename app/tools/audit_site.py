"""
Audit the committed site artifacts for the core catalog guarantees:

  1. Every book has an author.
  2. Every book has a narrator.
  3. Every book has a cover image that exists on disk under site/.
  4. Every author resolves to a Google Drive folder (matching the site's own
     resolution: exact or case-insensitive match on the FULL author string),
     or is explicitly excluded in scripts/audit_exclusions.json.

     The map that actually ships is the one EMBEDDED in site/index.html at
     build time (author_drive_map.json is only the source for the next
     build), so failures are judged against the embedded map. An author
     mapped in author_drive_map.json but not yet embedded is a warning
     ("pending site rebuild"), not a failure.

Unlike tests/test_catalog_completeness.py (which needs the audio library and
skips in CI), this audits only files tracked in git, so it can run as a
promotion gate in GitHub Actions.

Usage:
    python -m app.tools.audit_site            # audit repo-root site/
    python -m app.tools.audit_site --site-dir path/to/site

Exit code 0 = all checks pass, 1 = at least one failure.
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path

EXCLUSIONS_REL_PATH = Path("scripts") / "audit_exclusions.json"
AUTHOR_MAP_NAME = "author_drive_map.json"


def load_exclusions(path: Path) -> dict:
    """Load the exclusions file. Missing file = no exclusions."""
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _excluded_titles(exclusions: dict, check: str) -> set:
    return {t.strip().lower() for t in exclusions.get(check, {}).get("titles", [])}


def _excluded_authors(exclusions: dict) -> set:
    return {a.strip().lower() for a in exclusions.get("drive_links", {}).get("authors", [])}


def load_embedded_author_map(index_html: Path):
    """Extract the author map embedded in site/index.html (what actually
    ships). Returns None if the page or the embedded block is absent."""
    if not index_html.exists():
        return None
    html = index_html.read_text(encoding="utf-8")
    match = re.search(
        r'<script[^>]*id="ab-author-map-json"[^>]*>(.*?)</script>', html, re.DOTALL
    )
    if not match:
        return None
    try:
        parsed = json.loads(match.group(1).strip())
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def resolve_author_link(author: str, author_map: dict) -> bool:
    """Mirror the site's _resolveAuthorFolder: exact match, then
    case-insensitive match on the full author string."""
    if not author:
        return False
    link = author_map.get(author)
    if link and str(link).strip():
        return True
    norm = author.lower().strip()
    for key, value in author_map.items():
        if key.lower().strip() == norm and value and str(value).strip():
            return True
    return False


def audit(site_dir: Path, author_map_path: Path, exclusions_path: Path) -> int:
    catalog_path = site_dir / "catalog.csv"
    if not catalog_path.exists():
        print(f"::error::{catalog_path} not found — cannot audit")
        return 1

    with open(catalog_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print(f"::error::{catalog_path} has no book rows")
        return 1

    author_map = {}
    if author_map_path.exists():
        with open(author_map_path, encoding="utf-8") as f:
            author_map = json.load(f)
    else:
        print(f"::error::{author_map_path} not found — drive links cannot be audited")
        return 1

    exclusions = load_exclusions(exclusions_path)
    failures = []
    warnings = []

    # --- Check 1 & 2: author and narrator present on every row ---
    for check, field in (("author", "author"), ("narrator", "narrator")):
        skip_titles = _excluded_titles(exclusions, check)
        missing = [
            r["title"]
            for r in rows
            if not (r.get(field) or "").strip()
            and r["title"].strip().lower() not in skip_titles
        ]
        if missing:
            failures.append(
                f"{len(missing)} books missing {field}: "
                + "; ".join(missing[:10])
                + ("; ..." if len(missing) > 10 else "")
            )
        else:
            print(f"[OK] {field}: all {len(rows)} books have one")

    # --- Check 3: cover_href present and file exists under site/ ---
    skip_titles = _excluded_titles(exclusions, "covers")
    no_href = []
    missing_file = []
    for r in rows:
        if r["title"].strip().lower() in skip_titles:
            continue
        href = (r.get("cover_href") or "").strip()
        if not href:
            no_href.append(r["title"])
        elif not (site_dir / href).exists():
            missing_file.append(f"{r['title']} -> {href}")
    if no_href:
        failures.append(
            f"{len(no_href)} books have no cover_href: "
            + "; ".join(no_href[:10])
            + ("; ..." if len(no_href) > 10 else "")
        )
    if missing_file:
        failures.append(
            f"{len(missing_file)} cover files missing from {site_dir}: "
            + "; ".join(missing_file[:10])
            + ("; ..." if len(missing_file) > 10 else "")
        )
    if not no_href and not missing_file:
        print(f"[OK] covers: all {len(rows)} books have a cover file on disk")

    # --- Check 4: every author resolves in the drive map or is excluded ---
    # The embedded map in site/index.html is what ships; author_drive_map.json
    # only takes effect on the next site rebuild.
    embedded_map = load_embedded_author_map(site_dir / "index.html")
    shipped_map = embedded_map if embedded_map is not None else author_map
    if embedded_map is None:
        warnings.append(
            f"no embedded author map found in {site_dir / 'index.html'} — auditing {author_map_path} instead"
        )

    excluded_authors = _excluded_authors(exclusions)
    authors = sorted({(r.get("author") or "").strip() for r in rows if (r.get("author") or "").strip()})
    unmapped = [
        a
        for a in authors
        if a.lower() not in excluded_authors and not resolve_author_link(a, shipped_map)
    ]
    pending_rebuild = [a for a in unmapped if resolve_author_link(a, author_map)]
    hard_unmapped = [a for a in unmapped if a not in pending_rebuild]

    for a in pending_rebuild:
        warnings.append(
            f"author mapped in {author_map_path} but not in the shipped site yet "
            f"(regenerate the site to fix): {a}"
        )
    if hard_unmapped:
        failures.append(
            f"{len(hard_unmapped)} authors have no drive link and are not excluded: "
            + "; ".join(hard_unmapped[:10])
            + ("; ..." if len(hard_unmapped) > 10 else "")
            + " — add them to author_drive_map.json or to "
            + f"{EXCLUSIONS_REL_PATH} under drive_links.authors"
        )
    else:
        n_excluded = sum(1 for a in authors if a.lower() in excluded_authors)
        print(
            f"[OK] drive links: {len(authors) - n_excluded - len(pending_rebuild)} authors mapped, "
            f"{n_excluded} explicitly excluded, {len(pending_rebuild)} pending rebuild"
        )

    # --- Stale exclusions (warn only): entries that no longer match anything ---
    live_authors = {a.lower() for a in authors}
    for a in sorted(excluded_authors - live_authors):
        warnings.append(f"stale exclusion (author no longer in catalog): {a}")
    live_titles = {r["title"].strip().lower() for r in rows}
    for check in ("author", "narrator", "covers"):
        for t in sorted(_excluded_titles(exclusions, check) - live_titles):
            warnings.append(f"stale exclusion ({check} title no longer in catalog): {t}")

    for w in warnings:
        print(f"::warning::{w}")
    if failures:
        for msg in failures:
            print(f"::error::{msg}")
        print(f"\n[FAIL] {len(failures)} audit check(s) failed for {len(rows)} books")
        return 1

    print(f"\n[PASS] All core-feature audits passed for {len(rows)} books")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit committed site artifacts for core guarantees")
    parser.add_argument("--site-dir", default="site", type=Path)
    parser.add_argument("--author-map", default=Path(AUTHOR_MAP_NAME), type=Path)
    parser.add_argument("--exclusions", default=EXCLUSIONS_REL_PATH, type=Path)
    args = parser.parse_args()
    return audit(args.site_dir, args.author_map, args.exclusions)


if __name__ == "__main__":
    sys.exit(main())
