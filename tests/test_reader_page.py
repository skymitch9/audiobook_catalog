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
    # ⚠️ 'self' IS NOT PADDING. `default-src 'none'` blocks SAME-ORIGIN fetches
    # too, and pdf.js fetches its cMaps and standard fonts with fetch(). Without
    # this, a CJK page renders as BOXES and a page using a non-embedded base-14
    # font renders with NO TEXT — both of which look like a corrupt book rather
    # than a blocked request.
    #
    # It was genuinely MISSING, and was caught by serving this exact policy
    # string in a browser and watching `securitypolicyviolation`:
    #     connect-src <- .../__probe.pdf
    # No test and no local render could have found it, because the /dev/ lane
    # ships NO CSP at all: deploy.yml copies prod-src/site/. to the _site root
    # and Cloudflare ignores nested _headers, so the root policy comes from the
    # PROD branch. Production would have been this policy's first exercise.
    connect_src = re.search(r"connect-src ([^;]+)", csp).group(1)
    assert "'self'" in connect_src, (
        "connect-src needs 'self' or pdf.js cannot fetch its cMaps/standard fonts"
    )
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


# ==========================================================================
# VIEWER PHASE 2 — the EPUB half (2026-08-17)
#
# Same reasoning as everything above: every join here is a CONVENTION, not an
# import. A vendored library reached by path, a loader injected by hand into a
# library whose normal entry point does the opposite, a CSP applied by a path
# in _headers, and a git-tracking question that has already bitten this repo
# once. ⚠️ And the thing being defended is a NUMBER — 18 requests and 664 KB to
# open a 393 MiB book instead of one request and 412 MB. Nothing LOOKS
# different when that regresses.
#
# ⚠️ Stated plainly again: nothing here proves an EPUB renders. That was done
# by opening real books in a browser against a counting server (figures in
# catalog-platform/docs/info/ebook-viewer-phase1.md §9), and it has still never
# been done through the live gate by a signed-in person.
# ==========================================================================

FOLIATE = REPO / "site" / "static" / "foliate"
ZIPJS = REPO / "site" / "static" / "zipjs"
EPUB_LOADER = REPO / "site" / "epub-loader.js"
EPUB_RANGE = REPO / "site" / "epub-range.js"

#: The pinned foliate-js commit. Bumping it means bumping this line AND
#: re-running the range measurement — see site/static/foliate/VENDORED.md.
FOLIATE_COMMIT = "78914aef4466eb960965702401634c2cb348e9b1"
#: The pinned @zip.js/zip.js version — the one the 2026-08-17 probe measured.
ZIPJS_VERSION = "2.7.45"


def test_foliate_and_zipjs_are_vendored_whole_with_their_licences() -> None:
    """⚠️ No CDN at runtime — the CSP names none, so a CDN import is blocked."""
    for name in (
        "view.js", "epub.js", "epubcfi.js", "paginator.js", "fixed-layout.js",
        "progress.js", "overlayer.js", "text-walker.js", "search.js",
    ):
        assert (FOLIATE / name).is_file(), f"foliate-js {name} is missing"
    assert (FOLIATE / "LICENSE").is_file(), "MIT requires the licence to travel"
    assert (ZIPJS / "zip-no-worker-inflate.js").is_file()
    assert (ZIPJS / "core" / "io.js").is_file()
    assert (ZIPJS / "LICENSE").is_file(), "BSD-3-Clause requires the licence to travel"


def test_foliates_own_whole_file_loader_is_NOT_vendored() -> None:
    """⚠️ THE MECHANICAL GUARD, and the sharpest edge in viewer phase 2.

    foliate's `view.js` `makeBook()` builds `new ZipReader(new BlobReader(file))`
    over a whole in-memory Blob — 412,436,591 bytes for the White Sand Omnibus,
    pulled through a gated Worker before a word renders. The reader must never
    call it, and a comment saying so is only advice.

    Omitting `vendor/zip.js` makes it MECHANICAL: `makeZipLoader`'s
    `await import('./vendor/zip.js')` cannot resolve, so the whole-file path
    physically cannot run. Vendoring the file back is then a deliberate act with
    a reason attached, which is the bar it should have to clear.
    """
    assert not (FOLIATE / "vendor").exists(), (
        "foliate's vendor/ must NOT be vendored — it is what makes the "
        "whole-file BlobReader path work. See site/static/foliate/VENDORED.md."
    )


def test_the_epub_stack_is_TRACKED_IN_GIT_not_merely_on_disk() -> None:
    """⚠️ THE FAILURE THAT HAS NOW HAPPENED TWICE IN THIS REPO.

    At phase 1b the Python `build/` rule in .gitignore silently dropped both
    pdf.js renderer files from `git add site/static/pdfjs`. At phase 2 the
    Python `lib/` rule matched ALL 44 files of zip.js's `lib/` tree, which is
    why the vendored copy has that directory level removed (VENDORED.md records
    it).

    The lesson is phrased about GIT rather than about a path because it is not
    about `build/` or `lib/`: for a vendored dependency, "present in my working
    tree" and "will reach the deployment" are different facts, and only the
    second one matters.
    """
    required = [
        "site/epub-loader.js",
        "site/epub-range.js",
        "site/static/foliate/view.js",
        "site/static/foliate/epub.js",
        "site/static/foliate/paginator.js",
        "site/static/foliate/fixed-layout.js",
        "site/static/foliate/LICENSE",
        "site/static/zipjs/zip-no-worker-inflate.js",
        "site/static/zipjs/core/io.js",
        "site/static/zipjs/core/zip-reader.js",
        "site/static/zipjs/core/streams/codecs/inflate.js",
        "site/static/zipjs/LICENSE",
    ]
    out = subprocess.run(
        ["git", "ls-files", "--", *required],
        cwd=REPO, capture_output=True, text=True, check=False,
    )
    tracked = set(out.stdout.split())
    for path in required:
        assert path in tracked, (
            f"{path} is NOT tracked by git - it exists on disk but would never "
            f"reach the deployment. Check .gitignore (the Python `build/` and "
            f"`lib/` rules have each done this once already)."
        )
        assert (REPO / path).stat().st_size > 0, f"{path} is empty"

    # And the whole zip.js core tree, not just the files named above: a
    # partially-tracked ES module graph fails at the first missing import.
    out = subprocess.run(
        ["git", "ls-files", "--", "site/static/zipjs"],
        cwd=REPO, capture_output=True, text=True, check=False,
    )
    assert len(out.stdout.split()) >= 40, "zip.js's module tree is not fully tracked"


def test_the_pinned_versions_are_stated_in_exactly_one_place_each_and_agree() -> None:
    """A pin nobody can read is not a pin.

    ⚠️ foliate-js is pinned to a COMMIT, not to `@main`. The 2026-08-17 probe
    measured `@main` and said so in its own not-measured list; this is that
    tech-debt item closed.
    """
    doc = read(FOLIATE / "VENDORED.md")
    assert FOLIATE_COMMIT in doc
    assert "MIT" in doc
    assert "@main" in doc, "the reason it is a commit and not @main must survive"

    zdoc = read(ZIPJS / "VENDORED.md")
    assert f"**{ZIPJS_VERSION}**" in zdoc
    assert "BSD-3-Clause" in zdoc
    # ⚠️ The `lib/` deviation must stay written down, or the next update quietly
    # re-creates the directory and loses the whole library from git again.
    assert "lib/" in zdoc


def test_the_reader_never_routes_through_foliates_whole_file_path() -> None:
    """⚠️ THE ONE-FUNCTION-CALL REGRESSION.

    `makeBook`, `makeZipLoader` and `BlobReader` are the three names of the
    whole-file path. None may appear in executable code in any of these files —
    the comments explain them at length, which is why comments are stripped
    first.
    """
    for path in (READER_JS, EPUB_LOADER, EPUB_RANGE):
        src = strip_comments(read(path))
        for forbidden in ("makeBook", "makeZipLoader", "BlobReader"):
            assert forbidden not in src, (
                f"{path.name} reaches for foliate's whole-file loader "
                f"({forbidden}); that undoes viewer phase 2 entirely"
            )


def test_the_loader_is_range_only_and_treats_a_200_as_a_failure() -> None:
    """⚠️ The endpoint IGNORES a Range it cannot parse and answers 200 with the
    WHOLE FILE (phase 1a contract). So the failure mode of a bad range is not an
    error - it is a 393 MiB download. The transport must refuse a 200 rather
    than read it; the counting tests in site/__tests__/epub-range.test.js prove
    it does.
    """
    src = strip_comments(read(EPUB_RANGE))
    assert "RangeUnsupportedError" in src
    assert "res.status === 200" in src, "the whole-file answer must be detected"
    assert "res.body?.cancel()" in src, "and the body cancelled, never read"
    assert "rangeHeaderFor(" in src
    assert "method: 'GET'" in src


def test_the_epub_path_attaches_the_bearer_per_request() -> None:
    """⚠️ NOT a captured token, and NOT a URL parameter.

    pdf.js can only take `httpHeaders` once, at getDocument, which is why phase
    1b lists mid-session token expiry as unhandled. The EPUB transport calls a
    getter per range, so the Firebase SDK's own refresh keeps a long read alive.
    "Harmonising" the two by capturing the EPUB token would re-introduce the
    expiry, not remove a difference.
    """
    js = read(READER_JS)
    assert "getAuthHeader: async () =>" in js
    # ⚠️ UNFORCED, and from identity.getIdToken() rather than the user snapshot
    # (see the snapshot test below). Unforced is what makes per-request cheap:
    # the SDK returns its cached token and refreshes near expiry by itself.
    assert "const t = await getIdToken(app);" in js
    assert "await getIdToken(app, true)" in js, "and a fresh one at open"
    src = strip_comments(read(EPUB_RANGE))
    assert "await authOf()" in src, "the token getter must be awaited per request"
    assert "token=" not in src and "?auth=" not in src


def test_there_is_no_size_gate_on_epubs() -> None:
    """⚠️ Deliberately NOT built (viewer design's 32 MiB gate).

    All three oversized books open over ranges — the 393 MiB omnibus in 18
    requests totalling 664 KB. A refusal card for them would refuse books that
    work, and it is worth NOT building rather than building and removing.
    """
    shelf = strip_comments(read(SHELF))
    block = shelf[shelf.index("var read ="): shelf.index("cardInfoEl.innerHTML =")]
    assert "size_bytes" not in block, "the Read button must not gate on file size"
    assert "'pdf'" in block and "'epub'" in block, "both formats get the button"
    assert "can_download" not in block, (
        "reading is vis_ebooks, never the `download` capability (viewer design 6.x)"
    )


def test_the_reader_page_has_a_stage_for_each_renderer() -> None:
    """Two renderers, one shell. A PDF paints a canvas; an EPUB is paginated
    into #rd-book by <foliate-view>, which needs a definite height.
    """
    html = read(TEMPLATE)
    assert 'id="rd-book"' in html
    assert 'id="rd-pager-epub"' in html and 'id="rd-pager-pdf"' in html
    # ⚠️ `display:flex` beats the `hidden` attribute's UA `display:none`, so the
    # PDF stage needs an explicit rule or it stays on screen under an open EPUB.
    assert "#rd-stage[hidden]{display:none}" in html
    # Still no inline script: the CSP has not loosened.
    for tag in re.findall(r"<script\b[^>]*>", strip_comments(html)):
        assert " src=" in tag, f"inline script in read.html would be CSP-blocked: {tag}"


def test_the_reader_handles_both_of_foliates_renderers() -> None:
    """⚠️ CAUGHT BY THE ACCEPTANCE TEST, NOT BY READING THE SOURCE.

    `View.open()` picks `<foliate-paginator>` for a reflowable book and
    `<foliate-fxl>` for a `pre-paginated` one, and **foliate-fxl has no
    `setStyles` and none of the paginator's layout attributes**. Calling them
    anyway throws, and the reader answers "this book would not open" for a book
    that opens perfectly. The White Sand Omnibus — the acceptance-test book — is
    fixed-layout, which is how this was found.
    """
    js = strip_comments(read(READER_JS))
    assert "isFixedLayout" in js, (
        "reader.js must branch on view.isFixedLayout before calling setStyles"
    )


@pytest.mark.parametrize("path", ["/read", "/read/", "/dev/read", "/dev/read/"])
def test_the_csp_allows_blob_stylesheets_and_fonts(path: str) -> None:
    """⚠️ CAUGHT BY MEASUREMENT, AND IT WOULD HAVE SHIPPED SILENTLY.

    foliate rewrites an EPUB's own stylesheets and embedded fonts to `blob:`
    URLs, and `'self'` DOES NOT COVER `blob:`. Phase 1 put blob: in `img-src`
    and `frame-src` "so the EPUB build does not have to touch this file"; that
    was wrong by exactly these two directives.

    Measured both ways on a real book, 2026-08-17:
        style-src 'self'        -> the linked sheet yields ZERO rules,
                                   body font falls back to Times New Roman
        style-src 'self' blob:  -> 84 rules, body font Palatino
    A `font-src <- blob` violation was caught the same way.

    ⚠️ The failure looks like a badly-made book, not like a blocked request,
    and the page's own securitypolicyviolation listener NEVER HEARS IT: the
    section is a blob: iframe inside a CLOSED shadow root.
    """
    headers = read(HEADERS)
    block = re.search(rf"^{re.escape(path)}\n(?:  .+\n)+", headers, flags=re.MULTILINE)
    assert block
    csp = block.group(0)
    style_src = re.search(r"style-src ([^;]+)", csp).group(1)
    font_src = re.search(r"font-src ([^;]+)", csp).group(1)
    assert "blob:" in style_src, "an EPUB's own CSS is a blob: URL, and 'self' does not cover it"
    assert "blob:" in font_src, "an EPUB's embedded fonts are blob: URLs"
    # The phase-1 pair must survive too.
    assert "img-src 'self' data: blob:" in csp
    assert "frame-src 'self' blob:" in csp


def test_the_vendored_epub_stack_is_long_cached_in_both_lanes() -> None:
    """~50 small ES modules whose bytes never change in place. Without a rule
    the /* no-cache default costs a conditional request for each, every load.
    ⚠️ Both lanes: `/dev/` is a path, not a host.
    """
    headers = read(HEADERS)
    for rule in ("/static/foliate/*", "/dev/static/foliate/*",
                 "/static/zipjs/*", "/dev/static/zipjs/*"):
        block = re.search(rf"^{re.escape(rule)}\n(?:  .+\n)+", headers, flags=re.MULTILINE)
        assert block, f"no _headers cache rule for {rule}"
        assert "max-age=604800" in block.group(0)
        # ⚠️ NOT immutable: the path does not change when the pin does.
        assert "immutable" not in block.group(0)


def test_the_reader_takes_tokens_from_identity_not_from_the_user_snapshot() -> None:
    """⚠️ THE BUG THAT MADE THE READER UNUSABLE FOR EVERY SIGNED-IN PERSON.

    `getLiveUser()` answers a flat SNAPSHOT — `{uid, email, displayName}` — and
    has NO `getIdToken` method, deliberately: handing the live Firebase `User`
    to every caller is how a page ends up minting credentials in places nobody
    audits.

    Phase 1b called `user.getIdToken()` on that snapshot. It threw
    `TypeError: user.getIdToken is not a function` for every signed-in reader,
    and the surrounding catch reported it as "The shelf did not answer" — an
    OUTAGE sentence for something that was not an outage. ⚠️ Nothing caught it,
    because every test and every agent check was the SIGNED-OUT half, where the
    line never runs. It was found by opening the live dev lane in a signed-in
    browser on 2026-08-17.

    The fix is the token getter the viewer design named in advance. This test is
    what stops it coming back.
    """
    js = strip_comments(read(READER_JS))
    assert "user.getIdToken" not in js, (
        "getLiveUser()'s snapshot has no getIdToken — use identity.getIdToken(app)"
    )
    assert "getIdToken" in read(REPO / "site" / "identity.js"), "identity.js must export the getter"
    assert re.search(r"import \{[^}]*\bgetIdToken\b[^}]*\} from './identity.js'", js), (
        "reader.js must import the token getter from identity.js"
    )
    # ⚠️ And a missing token must be WORDED, not thrown: `getIdToken` answers
    # null when signed out, so every call site has to check.
    assert js.count("await getIdToken(app") >= 3


def test_identity_exports_a_token_getter_that_answers_null_when_signed_out() -> None:
    """The contract the reader depends on, pinned in the module that owns it.

    ⚠️ `null`, not a throw: "not signed in" is a state the caller words. A
    version that threw would be caught by the reader's outage branch and
    mislabelled all over again — which is the whole failure this replaced.
    """
    src = strip_comments(read(REPO / "site" / "identity.js"))
    assert "export async function getIdToken(app, force = false)" in src
    assert "typeof user.getIdToken !== 'function'" in src, (
        "the getter must survive a snapshot-shaped user rather than throw"
    )
    assert "return null" in src
    # ⚠️ getLiveUser must KEEP returning a snapshot. Widening it to the live
    # Firebase User would 'fix' the reader by undoing the reason this exists.
    assert "{ uid: user.uid, email: user.email || null, displayName: user.displayName || null }" in src


def test_NOTHING_asks_getLiveUsers_snapshot_for_a_token() -> None:
    """⚠️ A REPO-WIDE SWEEP, because this bug shipped in TWO places.

    `getLiveUser()` answers a flat snapshot — `{uid, email, displayName}` — with
    no token getter on it, deliberately. Both `site/reader.js` AND the shelf
    (`app/web/templates/ebooks.html`) asked that snapshot for a token, threw
    `TypeError: … is not a function` on the first gated request, and reported it
    through their outage branch. **The gated shelf never once rendered a book
    for anybody, and the reader never opened one.**

    ⚠️ Nothing caught it because every test and every agent check exercised the
    SIGNED-OUT half, where the line does not run — which is exactly why this is
    a static sweep rather than another behavioural test. It is cheap, it covers
    files nobody thought to test, and it fails on the shape of the mistake
    rather than on one instance of it.

    The rule: a file that gets a user from `getLiveUser()` may use `uid`,
    `email` and `displayName` from it, and must take tokens from
    `identity.getIdToken(app)`.
    """
    roots = [REPO / "site", REPO / "app" / "web" / "templates"]
    offenders = []
    for root in roots:
        for path in sorted(list(root.glob("*.js")) + list(root.glob("*.html"))):
            if "__tests__" in str(path) or path.name == "identity.js":
                continue
            src = strip_comments(read(path))
            if "getLiveUser" not in src:
                continue
            for m in re.finditer(r"(\w+)\.getIdToken\s*\(", src):
                offenders.append(f"{path.name}: {m.group(0)}")
    assert not offenders, (
        "these ask a getLiveUser() snapshot for a token, which throws for every "
        "signed-in visitor and surfaces as a mislabelled outage — use "
        f"identity.getIdToken(app) instead: {offenders}"
    )


# ==========================================================================
# VIEWER PHASE 3 — SAVE YOUR SPOT (2026-08-17)
#
# Owner: "for reading ebooks we also need to have it save your spot. this will
# be so important for pwa."
#
# ⚠️ EVERY FAILURE THIS FEATURE CAN HAVE IS SILENT. A position filed under the
# wrong id is not an error, it is "you were never here". A first paint that
# waits on Firestore is not an error, it is a reader that feels slow. A book
# that failed to open recording page 1 is not an error, it is somebody's place
# quietly destroyed. Nothing throws, nothing logs, and none of it is visible
# until a person notices their book opened at the beginning.
#
# The DECISIONS are pinned in site/__tests__/reading-position.test.js (the doc
# id's shape, last-write-wins, the arming guard). What is pinned HERE is the
# set of joins that are conventions rather than code: the rules block that
# makes ownership real, the imports that keep one implementation of the book
# key, and the two shapes in reader.js that would each undo the design while
# still passing every unit test.
# ==========================================================================

RULES = REPO / "firestore.rules"
POSITION_JS = REPO / "site" / "reading-position.js"


def test_the_position_store_is_TRACKED_IN_GIT_not_merely_on_disk() -> None:
    """Third time this file asks the question; see the pdf.js and zip.js notes.

    An untracked store module is a reader that loses everybody's place the
    moment it deploys, with the feature working perfectly on the machine that
    built it.
    """
    required = ["site/reading-position.js", "site/__tests__/reading-position.test.js"]
    out = subprocess.run(
        ["git", "ls-files", "--", *required],
        cwd=REPO, capture_output=True, text=True, check=False,
    )
    tracked = set(out.stdout.split())
    for path in required:
        assert path in tracked, f"{path} is NOT tracked by git"
        assert (REPO / path).stat().st_size > 0, f"{path} is empty"


def test_the_position_is_NOT_keyed_on_the_anchor() -> None:
    """⚠️ THE SILENT-ORPHAN TRAP, flagged by the viewer design in advance (§7.1).

    The anchor is `sha256(RELATIVE PATH)[:12]`, so re-filing or renaming a book
    changes it. A deep link that dies is a page that does not scroll; a POSITION
    that dies is a person's place in a book, gone, with no error anywhere.

    The key is the estate's own book identity instead — and it is IMPORTED, not
    re-derived: `ebook-notes.warningTitleFor()` already answers "which title
    identifies this ebook", and its header explains why keying on the epub's own
    spelling fails silently in both directions.
    """
    js = strip_comments(read(READER_JS))
    assert re.search(r"import \{[^}]*\bwarningTitleFor\b[^}]*\} from './ebook-notes.js'", js), (
        "reader.js must take the book key from ebook-notes.warningTitleFor()"
    )
    assert "warningTitleFor(cfg.book)" in js
    # The anchor still travels — as a FIELD. What must not exist is a doc id
    # built out of it.
    store = strip_comments(read(POSITION_JS))
    assert "positionDocId(uid, bookId)" in store
    assert "anchor" not in store[store.index("export function positionDocId"):
                                 store.index("export function localKey")], (
        "the anchor must never appear in the document id — it is a hint field"
    )


def test_the_first_paint_never_waits_for_the_network() -> None:
    """⚠️ The whole reason there are two stores.

    The per-device localStorage row is read SYNCHRONOUSLY and used for the very
    first draw; Firestore's answer arrives afterwards and is reconciled. An
    `await` on the lookup would put a household's uplink between a reader and
    their book — and the PWA case this was asked for is exactly the one where
    that network is worst.
    """
    js = strip_comments(read(READER_JS))
    assert "await loadLocal" not in js, "the local row must be read synchronously"
    assert "await loadRemote" not in js, (
        "the Firestore lookup must not be awaited on the open path — it "
        "reconciles in the background"
    )
    assert "loadRemote(db, uid, bookId).then(" in js


def test_a_newer_remote_position_is_OFFERED_and_never_applied_silently() -> None:
    """⚠️ Cross-device sync that relocates a reader mid-sentence without asking
    is the single most common complaint about every syncing reader ever shipped.

    The offer ships HIDDEN in the markup and is only ever revealed — the
    resolveAdmin() idiom this repo already uses for anything a page may not be
    entitled to show.
    """
    js = strip_comments(read(READER_JS))
    assert "offerJump(remote," in js
    html = read(TEMPLATE)
    assert '<div id="rd-resume" hidden>' in html
    assert 'id="rd-resume-jump"' in html and 'id="rd-resume-stay"' in html
    # ⚠️ Same trap as #rd-stage: `display:flex` beats the hidden attribute's UA
    # `display:none`, so the bar needs an explicit rule or it is always on.
    assert "#rd-resume[hidden]{display:none}" in html
    # And still no inline script — the CSP has not loosened for this either.
    for tag in re.findall(r"<script\b[^>]*>", strip_comments(html)):
        assert " src=" in tag, f"inline script in read.html would be CSP-blocked: {tag}"


def test_a_book_that_failed_to_open_records_nothing() -> None:
    """⚠️ THE GUARD THAT COSTS THE MOST WHEN IT IS MISSING.

    A broken file, a lapsed token or a refused range all end in a closed state
    — and if the keeper were live by then, "page 1" would already have been
    written over a real position. Arming is therefore explicit and happens only
    after a page has genuinely rendered.
    """
    store = strip_comments(read(POSITION_JS))
    assert "arm() { armed = true; }" in store
    assert "if (!armed" in store, "record() must refuse until armed"
    js = strip_comments(read(READER_JS))
    assert js.count("state.keeper?.arm()") == 2, (
        "both renderers must arm the keeper, and only after their first render"
    )
    # ⚠️ AND EACH MUST RECORD ONCE IMMEDIATELY AFTER ARMING — a race FOUND BY
    # EXERCISING THIS on the dev lane, not by reading the code. A reader who
    # turns a page WHILE the first page is still rendering turns it through an
    # unarmed keeper: that turn records nothing, and if they then stop, the
    # whole session saves nothing at all. Recording the settled position right
    # after arming catches it, at the price of one write per book opened.
    assert "recordPdfPosition();" in js and "recordEpubPosition(view.lastLocation);" in js, (
        "each renderer must record its settled position immediately after arming"
    )


def test_a_stale_locator_falls_back_to_the_start_never_to_a_broken_render() -> None:
    """⚠️ foliate's `init({ lastLocation })` awaits `renderer.goTo()` OUTSIDE any
    try, so a CFI that no longer resolves REJECTS init — and the reader answers
    "this book would not open" for a book that opens perfectly.

    So the bookmark is a SECOND navigation, after `init({ showTextStart: true })`
    has already put a readable page on screen. `view.goTo()` catches its own
    failures, so a dead bookmark costs a bookmark and never a book.
    """
    js = strip_comments(read(READER_JS))
    # ⚠️ Phrased about the CALL, not about the word: `view.lastLocation` is
    # READ elsewhere (to record where a book settled), which is fine. What
    # must never happen is HANDING a stored locator to init().
    assert not re.search(r"\.init\(\s*\{[^}]*lastLocation", js), (
        "the stored CFI must NOT be passed to foliate's init() — see "
        "goToStoredLocation in reader.js for why"
    )
    assert "await view.init({ showTextStart: true })" in js
    assert "goToStoredLocation" in js
    # The PDF half's equivalent: a page number out of range resolves to a real
    # page, never to an empty canvas.
    assert "function pageFrom(row, numPages)" in js


def test_the_final_save_rides_pagehide_and_visibilitychange_not_beforeunload() -> None:
    """⚠️ A mobile browser routinely kills a backgrounded tab without ever
    firing an unload event — which is precisely the life a PWA reader lives,
    and precisely the case this feature was asked for.
    """
    js = strip_comments(read(READER_JS))
    assert "addEventListener('pagehide'" in js
    assert "addEventListener('visibilitychange'" in js
    assert "beforeunload" not in js, (
        "beforeunload does not fire when a mobile browser discards a tab"
    )


def test_the_rules_make_a_reading_position_owner_only() -> None:
    """⚠️ RULES ARE PROJECT-WIDE THE MOMENT THEY DEPLOY — there is no dev lane
    for enforcement — so both lanes' blocks must be identical in posture, and
    `list` must be refused outright.

    The ownership check reads the uid back out of the document id, which is what
    makes it enforceable rather than advisory: neither half of `${uid}_${bookId}`
    can contain the separator (a Firebase uid is alphanumeric, a bookIdFromTitle
    slug is [a-z0-9-]). ⚠️ Changing the doc id shape is a rules change AND a
    migration, never an edit.
    """
    rules = read(RULES)
    assert "docId.split('_')[0] == request.auth.uid" in rules
    assert "request.auth != null" in rules
    for name in ("readingPositions", "readingPositions_dev"):
        block = re.search(rf"match /{name}/\{{docId\}} \{{\n(?:.+\n)+?    \}}", rules)
        assert block, f"no rules block for {name}"
        body = block.group(0)
        assert "allow get: if ownsPositionDoc(docId);" in body
        # ⚠️ `list` is refused, not merely unused. A collection-wide query would
        # enumerate what a household reads, and the doc-id wildcard is not
        # reliably bound for a list operation — so an `allow read` here would be
        # a hole wearing a get's clothes.
        assert "allow list: if false;" in body
        assert "allow create, update: if ownsPositionDoc(docId) && validReadingPosition();" in body
        assert "allow delete: if ownsPositionDoc(docId);" in body


def test_the_rules_refuse_a_locator_that_lost_its_kind() -> None:
    """⚠️ `pos.kind` travels WITH `pos.value`, atomically, or not at all.

    A CFI interpreted as a page number is a silent jump to the wrong place. The
    validator requires the pair, so a document whose locator has lost its type
    is refused at the store rather than stored and misread later.
    """
    rules = read(RULES)
    validator = rules[rules.index("function validReadingPosition()"):]
    validator = validator[: validator.index("}")]
    assert "request.resource.data.pos.kind in ['page', 'cfi']" in validator
    assert "request.resource.data.uid == request.auth.uid" in validator
    assert "request.resource.data.format in ['pdf', 'epub']" in validator
    assert "request.resource.data.updatedAt is number" in validator


def test_the_shelf_takes_its_manifest_token_from_identity_too() -> None:
    """The other half of the same fix, pinned where the shelf lives.

    ⚠️ And it must WORD a missing token rather than fall into the outage
    branch: "sign in again" and "the shelf's server did not respond" send a
    person to two different places, and only one of them is right.
    """
    shelf = read(SHELF)
    assert re.search(r"import \{[^}]*\bgetIdToken\b[^}]*\} from './identity.js'", shelf)
    assert "const token = await getIdToken(app);" in shelf
    assert "Your sign-in has lapsed" in shelf
