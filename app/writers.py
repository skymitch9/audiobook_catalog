# app/writers.py
from __future__ import annotations

import csv
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from app.config import COVERS_BASE_URL, OUTPUT_DIR
from app.web.html_builder import TEMPLATE_DIR  # not strictly needed, but useful when debugging
from app.web.html_builder import STATIC_DIR, render_index_html


# --------------------------
# CSV
# --------------------------
#: The catalog CSV's columns, in order. Shared with app/library_link.py's
#: manual runner so a re-stamp of the existing site/catalog.csv writes the
#: SAME column set a fresh pipeline build would — one list, not two that can
#: drift apart.
CSV_FIELDNAMES = [
    "title",
    "series",
    "series_index_display",
    "series_index_sort",
    "author",
    "narrator",
    "year",
    "genre",
    "duration_hhmm",
    "cover_href",
    "companion_files",
    "desc",
    # "Other versions available" — library_catalog's work id + format list for
    # this book, stamped by app/library_link.py before this function ever
    # runs. Blank means unmatched (or the pipeline step was skipped/unset),
    # never a guess.
    "library_work_id",
    "library_formats",
    # Shared-universe name (app/core/universes.py::universe_for) and the
    # per-series owned-volumes/gap summary (app/core/series_gaps.py), both
    # stamped in app/main.py right after rows are built. Blank means "no
    # universe" / "nothing to report" — the ordinary answer, not an error.
    "universe",
    "series_gap",
]


def write_csv(rows: List[Dict[str, str]], out_path: Path) -> None:
    """
    Writes the catalog CSV including cover references.
    """
    fieldnames = CSV_FIELDNAMES
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})
    print(f"Wrote CSV: {out_path}")


# --------------------------
# HTML (timestamped in output_files)
# --------------------------
def render_output_html(
    rows: List[Dict[str, str]],
    out_path: Path,
    generated_at: str,
    csv_link: str,
    drive_link: Optional[str],
    additions: Optional[Dict[str, dict]] = None,
) -> None:
    """
    Renders the timestamped HTML into output_files/ using the inline-CSS/JS template.
    csv_link should be a name relative to the HTML file (usually the timestamped CSV filename).
    """
    render_index_html(
        rows=rows,
        out_path=out_path,
        generated_at=generated_at,
        csv_link=csv_link,
        drive_link=drive_link,
        additions=additions,
    )
    print(f"Wrote HTML: {out_path}")


# --------------------------
# Site staging
# --------------------------
def _copy_covers_to_site(site_dir: Path) -> None:
    """
    Copy OUTPUT_DIR/covers into site/covers (dirs_exist_ok).
    If no covers yet, silently skip.
    """
    covers_src = OUTPUT_DIR / "covers"
    if covers_src.exists():
        covers_dst = site_dir / "covers"
        shutil.copytree(covers_src, covers_dst, dirs_exist_ok=True)


_COVERS_BASE_JS_TEMPLATE = '''\
// covers-base.js - where cover images live. GENERATED, do not edit.
//
// Written by app/writers.py from app/config.py COVERS_BASE_URL, which is the
// single source of truth. Covers are served from Cloudflare R2, not from this
// site, so anything that reads catalog.csv has to resolve the relative
// `cover_href` through coverUrl() before putting it in an <img src>.
//
// site/index.html does NOT import this - its cover URLs are already absolute,
// baked in at build time by app/web/html_builder.py cover_src().

export const COVERS_BASE_URL = {base!r};

/**
 * Resolve a catalog.csv `cover_href` to a fetchable URL.
 * "covers/A. American/Home.jpg" -> "<base>/A.%20American/Home.jpg"
 * Absolute hrefs (already-resolved, or historic values stored in Firestore)
 * pass through untouched.
 * @param {{string}} href
 * @returns {{string}} '' when there is no cover
 */
export function coverUrl(href) {{
  const cover = (href || '').trim();
  if (!cover) return '';
  if (/^(https?:)?\\/\\//.test(cover) || cover.startsWith('data:')) return cover;
  let rel = cover.startsWith('covers/') ? cover.slice('covers/'.length) : cover;
  // Historic Firestore hrefs arrive in BOTH forms: raw ('J.R. Mathews/…') and
  // already percent-encoded ('J.R.%20Mathews/…'), written by different eras of
  // the profile code. Encoding an encoded value double-encodes it (%20 -> %2520)
  // and the CDN answers 503 — measured live 2026-08-13 as exactly half the
  // community covers failing. Canonicalise: if it LOOKS encoded, decode first;
  // a raw value that merely contains '%' fails the decode and stays as-is.
  if (/%[0-9A-Fa-f]{{2}}/.test(rel)) {{ try {{ rel = decodeURIComponent(rel); }} catch {{ /* literal %, leave raw */ }} }}
  if (!COVERS_BASE_URL) return cover;
  // Match Python's urllib.parse.quote(safe='/') exactly, so a cover has ONE
  // canonical URL whichever side emitted it. encodeURIComponent leaves
  // !'()* alone; quote() percent-encodes them.
  const enc = (s) => encodeURIComponent(s).replace(
    /[!'()*]/g, (c) => '%' + c.charCodeAt(0).toString(16).toUpperCase());
  const encoded = rel.replace(/^\\/+/, '').split('/').map(enc).join('/');
  return COVERS_BASE_URL.replace(/\\/+$/, '') + '/' + encoded;
}}
'''


def _write_covers_base_js(site_dir: Path) -> None:
    """Emit site/covers-base.js so browser-side code shares the one knob.

    Rewritten on every build; committed, because the static pages import it
    and CI has no library to rebuild from.
    """
    js = _COVERS_BASE_JS_TEMPLATE.format(base=COVERS_BASE_URL)
    # repr() of a str gives single quotes — fine for JS, and keeps escaping honest.
    (site_dir / "covers-base.js").write_text(js, encoding="utf-8", newline="\n")


def _copy_static_to_site(site_dir: Path) -> None:
    """
    Copy app/web/static into site/static if it exists.
    Inline JS template works without it, but if you keep extra assets,
    this ensures they’re available.
    """
    static_dst = site_dir / "static"
    if STATIC_DIR.exists():
        shutil.copytree(STATIC_DIR, static_dst, dirs_exist_ok=True)


#: Template pages copied VERBATIM into site/ on every build — no substitution,
#: unlike index.html. Edit the copy in app/web/templates/; a rebuild wipes any
#: edit made to site/<name> directly.
#:   - guess-game.html: the duration-guessing game page
#:   - ebooks.html: display-only ebook shelf; renders site/ebooks.json
#:     client-side (ebook-split design phase 1), so the pipeline's manifest
#:     refresh updates it with no HTML rebuild
#:   - read.html: the in-browser reader (viewer phase 1b). ⚠️ Its LOGIC is in
#:     site/reader.js, not in the page — /read's CSP is `script-src 'self'`
#:     with no 'unsafe-inline', so an inline <script> would be blocked in
#:     production and nowhere else. reader.js is a hand-written committed file
#:     in site/, like identity.js and ebook-notes.js; only the PAGE is a
#:     template. tests/test_reader_page.py pins that this tuple lists it.
#:   - listen.html: the in-browser audiobook player (audio player phase 2).
#:     ⚠️ Same shape and the same reason as read.html — its logic is in
#:     site/listen.js because /listen's CSP forbids inline script. It also
#:     needs `media-src` and `worker-src` in that CSP, which no other page on
#:     this site does; site/_headers carries all four rules (two lanes × two
#:     slash forms). tests/test_listen_page.py pins both.
STATIC_TEMPLATE_PAGES = ("guess-game.html", "ebooks.html", "read.html", "listen.html")


def _copy_template_pages_to_site(site_dir: Path) -> None:
    """
    Copy the verbatim template pages (game, ebooks) into site/.
    """
    for name in STATIC_TEMPLATE_PAGES:
        src = TEMPLATE_DIR / name
        if src.exists():
            shutil.copy2(src, site_dir / name)


def stage_site_files(
    out_html: Path,
    out_csv: Path,
    site_dir: Path,
    site_index_name: str,
    site_csv_name: str,
    rows: List[Dict[str, str]],
    generated_at: str,
    drive_link: Optional[str],
    additions: Optional[Dict[str, dict]] = None,
) -> None:
    """
    Prepare the deployable site directory:
      - site/
        - index.html               (rendered fresh with csv_link = site_csv_name)
        - catalog.csv              (copied from the timestamped CSV)
        - covers/                  (copied from OUTPUT_DIR/covers/)
        - static/                  (copied from app/web/static/ if present)
    """
    site_dir.mkdir(parents=True, exist_ok=True)

    # 1) Copy CSV into site/ as the canonical name (e.g., catalog.csv)
    csv_dst = site_dir / site_csv_name
    shutil.copy2(out_csv, csv_dst)

    # 2) Copy covers/ and static/ assets
    #    site/covers/ is a LOCAL build product and is gitignored — it is the
    #    upload source for scripts/upload_covers_r2.py, not a deploy artifact.
    _copy_covers_to_site(site_dir)
    _copy_static_to_site(site_dir)
    _copy_template_pages_to_site(site_dir)
    _write_covers_base_js(site_dir)

    # 3) Render site/index.html with csv_link pointing at the site CSV file name
    site_index_path = site_dir / site_index_name
    render_index_html(
        rows=rows,
        out_path=site_index_path,
        generated_at=generated_at,
        csv_link=site_csv_name,  # RELATIVE link for GitHub Pages
        drive_link=drive_link,
        additions=additions,
    )

    print(f"Staged site: {site_index_path} (CSV -> {csv_dst})")
