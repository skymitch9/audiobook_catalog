"""
Audit the committed site artifacts for the core catalog guarantees:

  1. Every book has an author.
  2. Every book has a narrator.
  3. Every book has a cover image — recorded in site/covers_manifest.json
     (i.e. uploaded to Cloudflare R2, which is where covers are served from)
     or, failing that, present on disk under site/.
  4. Every author resolves to a Google Drive folder (matching the site's own
     resolution: exact or case-insensitive match on the FULL author string),
     or is explicitly excluded in scripts/audit_exclusions.json.
  5. Every EPUB in site/ebooks.json has a cover_url. Owner rule, 2026-08-17:
     "all epubs must resolve a cover". And every PDF either resolves one or is
     NAMED in the manifest's `needs_human_cover` list — a PDF whose page 1 is
     a wall of text correctly gets no auto-cover, but a coverless PDF nobody
     named is a silent gap and fails.
     Emergency escape hatch: ALLOW_COVERLESS_EPUBS=1.

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
COVER_MANIFEST_NAME = "covers_manifest.json"
EBOOKS_MANIFEST_NAME = "ebooks.json"

# The manifest key naming books no automatic cover source could settle. ⚠️ Kept
# in step with scripts.build_ebook_manifest.NEEDS_HUMAN_COVER_KEY, but NOT
# imported from it: this module audits only files tracked in git so it can run
# in CI without the audio library, and importing the builder would drag in
# app.config (which needs ROOT_DIR) through its import chain.
EBOOK_NEEDS_HUMAN_COVER_KEY = "needs_human_cover"

# Emergency escape hatch for check 5. Documented as emergency-only in
# docs/access/GIT_CI_DEPLOY.md; it lets a known-broken shelf reach prod.
ALLOW_COVERLESS_EPUBS_ENV = "ALLOW_COVERLESS_EPUBS"


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


def _author_candidates(author: str) -> list:
    """Individual author names embedded in a (possibly multi-author) string.

    The catalog author field can be a full multi-author string
    (e.g. "Broccoli Lion, Matthew Jackson - Translator") while the Drive
    folder / map key is only the primary author ("Broccoli Lion"). Splitting
    on the same separators book_sort uses to pick a primary — and dropping
    trailing role suffixes like " - Translator" — lets any one of the string's
    authors resolve the folder. Mirrors _resolveAuthorFolder in index.html.
    """
    if not author:
        return []
    out = []
    for part in re.split(r"[;,/&]| and ", author, flags=re.IGNORECASE):
        name = part.split(" - ")[0].strip()
        if name:
            out.append(name)
    return out


def _map_has(name: str, author_map: dict) -> bool:
    """Exact then case-insensitive lookup of a single author name in the map."""
    if not name:
        return False
    link = author_map.get(name)
    if link and str(link).strip():
        return True
    norm = name.lower().strip()
    for key, value in author_map.items():
        if key.lower().strip() == norm and value and str(value).strip():
            return True
    return False


def resolve_author_link(author: str, author_map: dict) -> bool:
    """Mirror the site's _resolveAuthorFolder: exact/case-insensitive match on
    the full author string, then on any individual author parsed from a
    multi-author string (so co-author/translator books resolve via the
    primary author's folder)."""
    if not author:
        return False
    if _map_has(author, author_map):
        return True
    return any(_map_has(cand, author_map) for cand in _author_candidates(author))


def _summarize(items: list, limit: int = 10) -> str:
    return "; ".join(items[:limit]) + ("; ..." if len(items) > limit else "")


def _check_required_fields(rows: list, exclusions: dict) -> list:
    """Every row must have an author and a narrator (title-level exclusions apply)."""
    failures = []
    for check, field in (("author", "author"), ("narrator", "narrator")):
        skip_titles = _excluded_titles(exclusions, check)
        missing = [
            r["title"]
            for r in rows
            if not (r.get(field) or "").strip()
            and r["title"].strip().lower() not in skip_titles
        ]
        if missing:
            failures.append(f"{len(missing)} books missing {field}: " + _summarize(missing))
        else:
            print(f"[OK] {field}: all {len(rows)} books have one")
    return failures


def load_cover_manifest(site_dir: Path) -> set:
    """Object keys recorded in site/covers_manifest.json.

    Covers live in Cloudflare R2, not in git, so in CI there is no
    site/covers/ to stat. The manifest — written by
    scripts/upload_covers_r2.py and committed — is the record of what is
    actually in the bucket, and is what this audit checks against.

    Returns an empty set when the manifest is absent or unreadable; the
    caller then falls back to the on-disk check.
    """
    path = site_dir / COVER_MANIFEST_NAME
    if not path.exists():
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            return set((json.load(f).get("files") or {}).keys())
    except (json.JSONDecodeError, OSError):
        return set()


def _cover_key(href: str) -> str:
    """catalog.csv's `cover_href` -> the R2 object key (path under site/covers)."""
    href = href.replace("\\", "/").lstrip("/")
    return href[len("covers/"):] if href.startswith("covers/") else href


def _check_covers(rows: list, site_dir: Path, exclusions: dict) -> list:
    """Every row must have a cover_href that resolves to a real cover.

    "Real" means present in site/covers_manifest.json (i.e. uploaded to R2)
    OR present on disk under site/. Either satisfies the guarantee; a local
    working tree has both, a CI checkout has only the manifest, and an old
    rollback target predating R2 has only the files.
    """
    skip_titles = _excluded_titles(exclusions, "covers")
    manifest_keys = load_cover_manifest(site_dir)
    no_href = []
    missing_file = []
    for r in rows:
        if r["title"].strip().lower() in skip_titles:
            continue
        href = (r.get("cover_href") or "").strip()
        if not href:
            no_href.append(r["title"])
            continue
        if _cover_key(href) in manifest_keys:
            continue
        if not (site_dir / href).exists():
            missing_file.append(f"{r['title']} -> {href}")

    failures = []
    if no_href:
        failures.append(f"{len(no_href)} books have no cover_href: " + _summarize(no_href))
    if missing_file:
        where = (
            f"{site_dir / COVER_MANIFEST_NAME} and not on disk under {site_dir}"
            if manifest_keys else f"{site_dir}"
        )
        failures.append(
            f"{len(missing_file)} covers missing from {where}: " + _summarize(missing_file)
            + (" — run `python -m scripts.upload_covers_r2`" if manifest_keys else "")
        )
    if not failures:
        source = f"{len(manifest_keys)} in the R2 manifest" if manifest_keys else "on disk"
        print(f"[OK] covers: all {len(rows)} books have a cover ({source})")
    return failures


def _check_drive_links(
    authors: list, site_dir: Path, author_map: dict, author_map_path: Path, exclusions: dict
) -> tuple:
    """Every author must resolve in the SHIPPED map (embedded in
    site/index.html) or be excluded. Authors mapped only in
    author_drive_map.json warn as pending rebuild instead of failing."""
    failures = []
    warnings = []

    embedded_map = load_embedded_author_map(site_dir / "index.html")
    shipped_map = embedded_map if embedded_map is not None else author_map
    if embedded_map is None:
        warnings.append(
            f"no embedded author map found in {site_dir / 'index.html'} — auditing {author_map_path} instead"
        )

    excluded_authors = _excluded_authors(exclusions)
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
            + _summarize(hard_unmapped)
            + " — add them to author_drive_map.json or to "
            + f"{EXCLUSIONS_REL_PATH} under drive_links.authors"
        )
    else:
        n_excluded = sum(1 for a in authors if a.lower() in excluded_authors)
        print(
            f"[OK] drive links: {len(authors) - n_excluded - len(pending_rebuild)} authors mapped, "
            f"{n_excluded} explicitly excluded, {len(pending_rebuild)} pending rebuild"
        )
    return failures, warnings


def _check_ebook_covers(site_dir: Path) -> tuple:
    """Every EPUB in site/ebooks.json must carry a cover_url.

    ⚠️ Owner rule, 2026-08-17, verbatim: "all epubs must resolve a cover or
    that breaks the test suite. this is so so important to me." Enforced in
    TWO places on purpose — tests/test_ebook_covers.py gates the merge (and
    therefore auto-promote), this gates the promotion itself, because a ref
    can reach `promote.yml` without having gone through today's tests.

    PDFs get the HONEST version of the same rule (2026-08-17, when page-1
    auto-covers landed): **every PDF resolves a cover OR is named in the
    manifest's `needs_human_cover` list.** A PDF whose first page is genuinely
    a wall of text cannot have one — refusing it is correct, and shipping that
    page as a cover is the thing the owner's likeness check exists to stop — so
    a text-first PDF must not break promote. But a coverless PDF that nobody
    named is a SILENT gap, and that fails. Naming is the whole contract.

    A missing manifest is a WARNING, not a failure: an old `prod-*` rollback
    tag predates site/ebooks.json entirely, and this gate must not make such
    a ref unpromotable. The `needs_human_cover` key gets the same treatment
    for the same reason — a ref that predates it is warned about, not blocked.
    Every manifest `scripts/build_ebook_manifest.py` writes carries the key
    (empty when nothing is waiting), so the failure path is live for any
    current ref.

    Escape hatch: ALLOW_COVERLESS_EPUBS=1, emergency use only — it lets a
    known-broken shelf reach prod, so use it to unblock an unrelated urgent
    promotion and never as a way to live with missing covers. It covers the
    unnamed-PDF failure too, for the same emergency and with the same warning.
    """
    import os

    path = site_dir / EBOOKS_MANIFEST_NAME
    if not path.exists():
        return [], [f"{path} not found — ebook cover coverage not audited (pre-ebooks ref?)"]
    try:
        with open(path, encoding="utf-8") as f:
            manifest = json.load(f)
        entries = manifest["ebooks"]
        if not isinstance(entries, list):
            raise TypeError("'ebooks' is not a list")
    except (json.JSONDecodeError, OSError, KeyError, TypeError) as e:
        return [f"{path} is unreadable or malformed ({e}) — the ebook shelf would not render"], []

    rows = [e for e in entries if isinstance(e, dict)]
    by_format = lambda fmt: [e for e in rows if str(e.get("format") or "").lower() == fmt]  # noqa: E731
    coverless = lambda es: [e for e in es if not str(e.get("cover_url") or "").strip()]  # noqa: E731
    named_of = lambda es: _summarize(  # noqa: E731
        [f"{e.get('title')} ({e.get('path')})" for e in es], limit=25
    )
    escape_hatch = os.environ.get(ALLOW_COVERLESS_EPUBS_ENV) == "1"
    failures, warnings = [], []

    epubs = by_format("epub")
    if not epubs:
        warnings.append(f"{path} lists no EPUBs — nothing to audit")
    else:
        naked = coverless(epubs)
        if not naked:
            print(f"[OK] ebook covers: all {len(epubs)} EPUBs have a cover")
        elif escape_hatch:
            warnings.append(
                f"{ALLOW_COVERLESS_EPUBS_ENV}=1 — EMERGENCY OVERRIDE: promoting with "
                f"{len(naked)} coverless EPUB(s): {named_of(naked)}"
            )
        else:
            failures.append(
                f"{len(naked)} of {len(epubs)} EPUBs have no cover: {named_of(naked)}"
                " — rebuild with `python -m scripts.build_ebook_manifest` (oversized covers are"
                " downscaled, not rejected) or add one to scripts/ebook_cover_overrides.json;"
                f" emergency only: {ALLOW_COVERLESS_EPUBS_ENV}=1"
            )

    pdfs = by_format("pdf")
    naked_pdfs = coverless(pdfs)
    if not pdfs:
        pass  # a library with no PDFs is fine; nothing to say
    elif EBOOK_NEEDS_HUMAN_COVER_KEY not in manifest:
        if naked_pdfs:
            warnings.append(
                f"{path} has no '{EBOOK_NEEDS_HUMAN_COVER_KEY}' list — {len(naked_pdfs)} coverless "
                "PDF(s) not audited (pre-auto-cover ref?)"
            )
    else:
        listed = {
            str(e.get("path"))
            for e in (manifest.get(EBOOK_NEEDS_HUMAN_COVER_KEY) or [])
            if isinstance(e, dict)
        }
        unnamed = [e for e in naked_pdfs if str(e.get("path")) not in listed]
        if not unnamed:
            print(
                f"[OK] PDF covers: {len(pdfs) - len(naked_pdfs)} of {len(pdfs)} PDFs have a cover, "
                f"{len(naked_pdfs)} named as needing a human"
            )
        elif escape_hatch:
            warnings.append(
                f"{ALLOW_COVERLESS_EPUBS_ENV}=1 — EMERGENCY OVERRIDE: promoting with "
                f"{len(unnamed)} unnamed coverless PDF(s): {named_of(unnamed)}"
            )
        else:
            failures.append(
                f"{len(unnamed)} of {len(pdfs)} PDFs have no cover and are not named in "
                f"'{EBOOK_NEEDS_HUMAN_COVER_KEY}': {named_of(unnamed)}"
                " — rebuild with `python -m scripts.build_ebook_manifest`, which auto-covers a"
                " PDF whose page 1 passes the cover-likeness gate and names the rest;"
                f" emergency only: {ALLOW_COVERLESS_EPUBS_ENV}=1"
            )

    return failures, warnings


def _stale_exclusion_warnings(rows: list, authors: list, exclusions: dict) -> list:
    """Warn about exclusion entries that no longer match anything in the catalog."""
    warnings = []
    live_authors = {a.lower() for a in authors}
    for a in sorted(_excluded_authors(exclusions) - live_authors):
        warnings.append(f"stale exclusion (author no longer in catalog): {a}")
    live_titles = {r["title"].strip().lower() for r in rows}
    for check in ("author", "narrator", "covers"):
        for t in sorted(_excluded_titles(exclusions, check) - live_titles):
            warnings.append(f"stale exclusion ({check} title no longer in catalog): {t}")
    return warnings


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

    if not author_map_path.exists():
        print(f"::error::{author_map_path} not found — drive links cannot be audited")
        return 1
    with open(author_map_path, encoding="utf-8") as f:
        author_map = json.load(f)

    exclusions = load_exclusions(exclusions_path)
    authors = sorted({(r.get("author") or "").strip() for r in rows if (r.get("author") or "").strip()})

    failures = _check_required_fields(rows, exclusions)
    failures += _check_covers(rows, site_dir, exclusions)
    link_failures, warnings = _check_drive_links(
        authors, site_dir, author_map, author_map_path, exclusions
    )
    failures += link_failures
    ebook_failures, ebook_warnings = _check_ebook_covers(site_dir)
    failures += ebook_failures
    warnings += ebook_warnings
    warnings += _stale_exclusion_warnings(rows, authors, exclusions)

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
