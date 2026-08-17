"""
The reader's wiring guard — viewer phase 1b (2026-08-17).

WHY IT EXISTS. Everything the browser reader depends on is a CONVENTION rather
than an import: a page copied into `site/` by a tuple in writers.py, a module
loaded by filename, a CSP applied by a path in `_headers`, and a renderer
vendored by hand. None of that typechecks, none of it fails a build, and every
one of them fails in production and nowhere else. These tests are the "or
breaks loudly" half.

⚠️ Stated plainly, because it matters more than the green tick: **nothing here
proves a PDF renders.** No test in any language can tell you that. It has to be
opened by a signed-in person on the dev lane, once, with eyes. What these pin
is that the pieces are still connected to each other.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "app" / "web" / "templates" / "read.html"
SHELF = REPO / "app" / "web" / "templates" / "ebooks.html"
READER_JS = REPO / "site" / "reader.js"
HEADERS = REPO / "site" / "_headers"
PDFJS = REPO / "site" / "static" / "pdfjs"

#: The version this build was vendored and verified against. Bumping pdf.js
#: means bumping this line AND re-reading VENDORED.md's update procedure.
PDFJS_VERSION = "5.4.149"


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def strip_comments(text: str) -> str:
    """HTML comments and JS block/line comments removed.

    ⚠️ Needed because these files EXPLAIN the rules they follow, at length, and
    a naive substring search finds the explanation and calls it a violation.
    (It found exactly that twice while this file was being written.)
    """
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)


# --------------------------------------------------------------------------
# The page reaches site/ at all
# --------------------------------------------------------------------------
def test_read_html_is_staged_by_the_build() -> None:
    """The template is copied VERBATIM into site/ — a tuple, not an import.

    Drop `read.html` from STATIC_TEMPLATE_PAGES and the reader silently stops
    being deployed: the template stays in the repo, the page 404s, and nothing
    else changes. That is the failure this test exists for.
    """
    from app.writers import STATIC_TEMPLATE_PAGES

    assert "read.html" in STATIC_TEMPLATE_PAGES
    assert TEMPLATE.exists(), "the template writers.py promises to copy must exist"


def test_the_page_loads_its_logic_from_an_external_module() -> None:
    """⚠️ The CSP is `script-src 'self'` with NO 'unsafe-inline'.

    An inline <script> in this page is blocked in production and nowhere else,
    which is the worst kind of bug to find. So: exactly one module script tag,
    pointing at reader.js, and no inline script block other than theme.js's
    src-only tag.
    """
    html = read(TEMPLATE)
    assert '<script type="module" src="reader.js"></script>' in html
    assert READER_JS.exists()
    # Every <script> in the page must carry a `src`. An inline one would run
    # locally and be refused live. (Comments stripped first — the page's own
    # header explains this rule and names the tag it forbids.)
    for tag in re.findall(r"<script\b[^>]*>", strip_comments(html)):
        assert " src=" in tag, f"inline script in read.html would be CSP-blocked: {tag}"


# --------------------------------------------------------------------------
# The URLs, and the two shapes that break
# --------------------------------------------------------------------------
def test_every_asset_reference_is_relative_so_the_dev_lane_loads_dev_copies() -> None:
    """⚠️ The dev lane is a PATH (`/dev/read`), not a host.

    A root-absolute `/static/...` or `/reader.js` would load the PROD copy
    while somebody reviews the dev lane — a fix that looks like it did not
    deploy. Every same-origin reference here must be relative.
    """
    html = read(TEMPLATE)
    for bad in re.findall(r'(?:src|href)="(/[^/"][^"]*)"', html):
        pytest.fail(f"root-absolute same-origin reference in read.html: {bad}")


def test_the_shelfs_read_button_links_to_the_canonical_extensionless_path() -> None:
    """`read?b=<anchor>` — no `.html`, no trailing slash, and relative.

    ⚠️ `.html` is 308'd by Cloudflare Pages and ebooks-door passes responses
    through verbatim, so the redirect escapes onto the audiobook host (measured
    on `/ebooks.html`, 2026-08-17). A trailing slash re-bases every relative URL
    on the page onto `/read/`, where nothing exists.
    """
    shelf = read(SHELF)
    assert "href=\"read?b=' + encodeURIComponent(b.anchor)" in shelf
    assert "read.html?b=" not in shelf
    assert "read/?b=" not in shelf


def test_the_read_button_is_pdf_only_and_ignores_can_download() -> None:
    """⚠️ Two separate rules, both load-bearing, both easy to "helpfully" break.

    1. PDFs only. The reader answers an honest "not yet" for EPUB, so a button
       on an EPUB card would be a dead affordance (ROLES.md §1e).
    2. `can_download` must NOT gate it. Reading is the estate's `vis_ebooks`
       grant — already held by anyone seeing this shelf — while `download`
       floors at `admin`. Gating Read on `can_download` would show every
       member a shelf none of them could read, which is the exact inversion
       viewer design §6.x was written to stop.
    """
    shelf = read(SHELF)
    # Find the block that builds the Read link.
    block = shelf[shelf.index("var read ="): shelf.index("cardInfoEl.innerHTML =")]
    assert "'pdf'" in block, "the Read link must be conditional on the PDF format"
    assert "can_download" not in block, (
        "reading is vis_ebooks, never the `download` capability (viewer design 6.x)"
    )


# --------------------------------------------------------------------------
# The CSP
# --------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/read", "/read/", "/dev/read", "/dev/read/"])
def test_all_four_reader_paths_carry_the_csp(path: str) -> None:
    """⚠️ Cloudflare's `*` is a path splat, not a glob, so these cannot be one
    rule. Both lanes and both slash forms are listed or one of them ships bare.
    """
    headers = read(HEADERS)
    block = re.search(
        rf"^{re.escape(path)}\n(?:  .+\n)+", headers, flags=re.MULTILINE
    )
    assert block, f"no _headers rule for {path}"
    assert "Content-Security-Policy:" in block.group(0)


def test_the_csp_names_the_worker_and_forbids_inline_and_eval() -> None:
    """The three directives whose absence produces a mystery rather than an error.

    ⚠️ A missing `connect-src` for the Worker fails as an OPAQUE network error,
    indistinguishable from the Worker being down — the misdiagnosis this estate
    already ate once. `'unsafe-inline'` in script-src would quietly re-permit
    the inline script the page must not have. `'unsafe-eval'` is unnecessary
    because pdf.js is given `isEvalSupported: false`; turning the flag off
    beats widening the header.
    """
    headers = read(HEADERS)
    policy = re.search(r"^/read\n  Content-Security-Policy: (.+)$", headers, flags=re.MULTILINE)
    assert policy
    csp = policy.group(1)
    assert "connect-src" in csp and "https://audiobook-api.heygabi.ai" in csp
    assert "https://securetoken.googleapis.com" in csp, "token refresh must not be blocked"
    script_src = re.search(r"script-src ([^;]+)", csp).group(1)
    assert "'unsafe-inline'" not in script_src
    assert "'unsafe-eval'" not in csp
    # ⚠️ blob: in worker-src and img-src: pdf.js may fall back to a blob worker,
    # and viewer phase 2's EPUB renderer materialises images as blob URLs.
    # Omitting the latter gives a reader that paginates and shows no pictures.
    assert "worker-src 'self' blob:" in csp
    assert "img-src 'self' data: blob:" in csp


# --------------------------------------------------------------------------
# The vendored renderer
# --------------------------------------------------------------------------
def test_pdfjs_is_vendored_whole_with_its_licence() -> None:
    """⚠️ No CDN at runtime — the CSP names none, so a CDN fetch is blocked.

    cmaps and standard_fonts are NOT optional extras: without cmaps a
    non-embedded CJK page (the shelf has Japanese light-novel PDFs) renders as
    boxes, and without standard_fonts a PDF referencing Helvetica without
    embedding it renders with no text at all. Both look like a corrupt file.
    """
    assert (PDFJS / "build" / "pdf.min.js").is_file()
    assert (PDFJS / "build" / "pdf.worker.min.js").is_file()
    assert (PDFJS / "LICENSE").is_file(), "Apache-2.0 requires the licence to travel"
    assert len(list((PDFJS / "cmaps").glob("*.bcmap"))) > 100
    assert len(list((PDFJS / "standard_fonts").glob("*"))) >= 14


def test_the_vendored_renderer_is_TRACKED_IN_GIT_not_merely_on_disk() -> None:
    """⚠️ THE ONE THAT ACTUALLY BIT, 2026-08-17.

    `.gitignore` carries the standard Python packaging rule `build/`. pdf.js's
    upstream layout puts its two renderer files in a directory called `build/`,
    so `git add site/static/pdfjs` matched that rule and **silently dropped
    both of them**. Every other check passed: the files were on disk, the page
    referenced them, the CSP allowed them, the version agreed. The deploy would
    have shipped a reader whose renderer 404s — a book that never opens, with
    nothing anywhere saying why.

    The lesson generalises past pdf.js, which is why this test is phrased about
    GIT rather than about a path: for a vendored dependency, "present in my
    working tree" and "will reach the deployment" are different facts, and only
    the second one matters. A `.gitignore` negation now exists; this is what
    fails if someone removes it.
    """
    required = [
        "site/static/pdfjs/build/pdf.min.js",
        "site/static/pdfjs/build/pdf.worker.min.js",
        "site/static/pdfjs/LICENSE",
        "site/reader.js",
        "site/read.html",
    ]
    out = subprocess.run(
        ["git", "ls-files", "--", *required],
        cwd=REPO, capture_output=True, text=True, check=False,
    )
    tracked = set(out.stdout.split())
    for path in required:
        assert path in tracked, (
            f"{path} is NOT tracked by git — it exists on disk but would never "
            f"reach the deployment. Check .gitignore (the Python `build/` rule "
            f"has done this once already)."
        )
    # Non-empty, too: a tracked zero-byte renderer is the same outage.
    for path in required:
        assert (REPO / path).stat().st_size > 0, f"{path} is empty"


def test_the_cmaps_and_fonts_are_tracked_too() -> None:
    """Same failure mode, quieter symptom.

    Missing cmaps do not break the reader — they break ONE Japanese PDF, months
    later, as boxes instead of text, which nobody connects to a deploy.
    """
    out = subprocess.run(
        ["git", "ls-files", "--", "site/static/pdfjs/cmaps", "site/static/pdfjs/standard_fonts"],
        cwd=REPO, capture_output=True, text=True, check=False,
    )
    tracked = out.stdout.split()
    assert len([t for t in tracked if t.endswith(".bcmap")]) > 100, "cmaps are not tracked"
    assert len([t for t in tracked if "standard_fonts" in t]) >= 14, "standard_fonts are not tracked"


def test_the_vendored_version_is_stated_in_exactly_one_place_and_agrees() -> None:
    """A pinned version nobody can read is not pinned.

    VENDORED.md is the record; this test is what makes it true. Bump one
    without the other and this goes red rather than leaving a doc that lies.
    """
    doc = read(PDFJS / "VENDORED.md")
    assert f"**{PDFJS_VERSION}**" in doc
    assert "Apache License 2.0" in doc
    assert "httpHeaders" in doc, "the one API guarantee the design leans on must be recorded"


def test_httpHeaders_still_exists_in_the_vendored_bytes() -> None:
    """⚠️ THE LOAD-BEARING API. The whole "no credential in a URL" decision
    (viewer design §3.3) rests on `getDocument({ httpHeaders })` reaching the
    request. It was an unverified assumption until this build; it is verified
    here, against the bytes actually shipped, so a future version bump that
    drops it fails at test time instead of at reading time.

    If this ever goes red: STOP and read viewer design §3.3, which names the
    fallback (a short-lived, book-scoped read lease).
    """
    minified = (PDFJS / "build" / "pdf.min.js").read_text(encoding="utf-8", errors="ignore")
    assert "httpHeaders" in minified


def test_the_reader_configures_pdfjs_the_way_the_design_requires() -> None:
    """The four options whose defaults are wrong for this corpus.

    ⚠️ `disableAutoFetch` is the one that costs real money and real phone data:
    left at its default, pdf.js background-fetches the rest of the document
    after page 1, turning "open the handbook to check one table" into a 181 MB
    transfer.
    """
    js = read(READER_JS)
    assert "httpHeaders: { Authorization: `Bearer ${token}` }" in js
    assert "disableAutoFetch: true" in js
    assert "disableRange: false" in js
    assert "isEvalSupported: false" in js
    # ⚠️ BOTH flags, and this one CORRECTS the design. Viewer design §5.1
    # specifies `disableStream: false`. MEASURED 2026-08-17 against the real
    # 181 MiB Stormlight handbook with the vendored pdf.js, counting bytes
    # actually delivered:
    #     disableStream: false -> the full-file GET RAN TO COMPLETION,
    #                             189,930,310 B — 100% of the file
    #     disableStream: true  -> the same GET was ABORTED at 655,360 B — 0.3%
    # `disableAutoFetch` alone does NOT prevent it: the flags govern different
    # things (speculative fetching of the rest vs the whole-file read opened at
    # the start). Flipping this back re-introduces a 181 MB transfer to open one
    # page, which is the exact cost §5.3 exists to avoid.
    assert "disableStream: true" in js, (
        "disableStream MUST be true — false transfers the whole file (measured)"
    )
    # ⚠️ The bearer is a HEADER. A token in the query string would survive in
    # history, referrers and any log that records request lines.
    assert "token=" not in js and "?auth=" not in js


def test_the_reader_never_recomputes_an_anchor() -> None:
    """⚠️ ONE implementation of the anchor fold, in Python
    (`build_ebook_manifest.ebook_anchor`). A second copy in JavaScript would
    break every deep link silently — the page simply fails to open a book.
    """
    js = strip_comments(read(READER_JS))
    for forbidden in ("sha256", "subtle.digest", "SHA-256", "crypto.subtle"):
        assert forbidden not in js, f"reader.js must READ anchors, never fold one ({forbidden})"


def test_the_epub_seam_is_documented_for_the_next_agent() -> None:
    """Phase 2 is a different build. The seam is one branch and it says so.

    This is a doc test on purpose: the next agent's first act is reading this
    file, and a seam that exists in someone's head is not a seam.
    """
    js = read(READER_JS)
    assert "EPUB SEAM" in js
    assert "foliate" in js, "the measured renderer recommendation must survive to phase 2"
    assert "epub.js" in js, "and so must the reason it is NOT epub.js"
