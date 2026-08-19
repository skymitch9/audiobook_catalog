"""One modal open renders the modal ONCE — the guard for a bug that shipped.

WHAT HAPPENED (measured on live prod, 2026-08-19, from the page itself). The
owner photographed the book modal with the "Not streamable yet — request it"
button and its explainer paragraph rendered twice, stacked. Driving the live
site reproduced it worse than the photo: opening one book rendered the audio
row **four** times, and ran four of everything else the modal loads.

TWO MULTIPLIERS, and neither is visible from reading either one alone:

  1. The modal-open MutationObserver ran its body once per MUTATION RECORD via
     `mutations.forEach(...)`. `openModal()` changes two attributes — `class`
     then `aria-hidden` — so one open delivers two records, and two renders.
  2. `openModal()` is itself called TWICE for one click on a cover inside the
     table: the `button.cover-btn` document listener fires, and the
     `#ab-table tbody td` listener explicitly does *not* bail on a cover-btn
     target, so it fires too. Two calls x two attributes = four records.

WHY IT WAS INVISIBLE FOR MONTHS. The observer's double-fire has been there
since the reviews section was first wired into the modal (`1d535c7`). It never
showed, because `renderReviewSection` and `renderReadingListButtons` both END by
ASSIGNING `innerHTML` — running them twice is wasteful but idempotent on screen.
`renderAudioRow` (`7691cc6`, audio phase 1, 2026-08-18) is the first of the
three to APPEND, and it clears its container *before* its `await`s: two
concurrent calls both clear, then both append. So the day an append-shaped
renderer joined an unchanged observer, a latent bug became a visible one.

⚠️ THAT IS WHY THESE TESTS PIN THE OBSERVER AND NOT THE BUTTON. A test that
only counted audio rows would pass again the moment somebody wrote another
innerHTML-assigning renderer, and the next append-shaped one would resurrect
this exact bug. The invariant worth keeping is "one open, one render".

THE COST, MEASURED, because it is also the answer to a second report the same
morning ("community db stuff is loading slow"): each of those four renders
called `getReviews()`, which downloaded the WHOLE `reviews` collection —
886 documents, 272,065 bytes, 410-424 ms per call. One modal open therefore
moved ~1.09 MB and spent ~1.7 s of Firestore time on a wired desktop, to show a
handful of reviews. See `test_reviews_are_fetched_filtered` below.

⚠️ Stated plainly, in the house style: **nothing here runs the page.** These are
source-shape guards over files whose contract is a convention, not an import.
The live proof is a signed-in person opening a book and seeing one button.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "app" / "web" / "templates" / "index.html"
GENERATED = REPO / "site" / "index.html"
REVIEWS_JS = REPO / "site" / "reviews.js"


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def strip_comments(text: str) -> str:
    """HTML comments and JS block/line comments removed.

    ⚠️ Load-bearing here: the block under test now carries a long comment that
    NAMES the bug, `mutations.forEach` and all. A naive substring search finds
    the explanation and calls it the code — which would fail every test below
    for the wrong reason.
    """
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)


def observer_block(src: str) -> str:
    """The modal-open observer's code, comments stripped.

    Bounded by the observer's construction and the `observe()` call that arms
    it, so an unrelated MutationObserver elsewhere on the page cannot satisfy
    or break these tests.
    """
    code = strip_comments(src)
    start = code.find("const observer = new MutationObserver(")
    assert start != -1, "the modal-open MutationObserver is gone from this page"
    end = code.find("observer.observe(modal", start)
    assert end != -1, "observer.observe(modal, ...) no longer follows the observer"
    return code[start:end]


# --------------------------------------------------------------------------- #
# One open, one render
# --------------------------------------------------------------------------- #
def test_the_observer_does_not_run_its_body_per_mutation_record():
    """🔴 THE REGRESSION ITSELF.

    `mutations.forEach(...)` is the whole bug: it turns one open into one render
    PER ATTRIBUTE CHANGED. A MutationObserver callback is delivered once per
    task with the entire batch, so reading the modal's state once per CALLBACK
    is both correct and immune to how many attributes the opener touches — and,
    because both `openModal()` calls land in the same click task, immune to how
    many callers there are too.
    """
    for name, path in (("template", TEMPLATE), ("generated", GENERATED)):
        block = observer_block(read(path))
        assert "forEach" not in block, (
            f"{name}: the modal observer iterates its mutation records again. "
            "One open delivers several records, so this renders the modal "
            "several times — that is the duplicate audio-request block."
        )
        assert "attributeName" not in block, (
            f"{name}: the observer is branching on individual records again; "
            "read modal.classList once per callback instead."
        )


def test_each_modal_renderer_is_invoked_exactly_once():
    for name, path in (("template", TEMPLATE), ("generated", GENERATED)):
        block = observer_block(read(path))
        for fn in ("renderReviewSection", "renderReadingListButtons", "renderAudioRow"):
            assert block.count(f"{fn}(") == 1, (
                f"{name}: {fn} is called {block.count(f'{fn}(')} times in the "
                "modal-open observer; it must be called exactly once per open."
            )


def test_a_reopen_of_the_same_book_still_renders():
    """The guard must be armed by CLOSING, not held forever.

    A bare "already rendered" flag would make the second visit to a book show an
    empty modal — a worse bug than the one being fixed, and one that only
    appears on the second open, which is exactly when nobody is looking.
    """
    for name, path in (("template", TEMPLATE), ("generated", GENERATED)):
        block = observer_block(read(path))
        assert "classList.contains('open')" in block, (
            f"{name}: the observer no longer reads the modal's open state."
        )
        assert "= null" in block, (
            f"{name}: nothing resets the render guard when the modal closes, so "
            "re-opening a book would render nothing."
        )


def test_opening_a_different_book_without_closing_still_renders():
    """The guard tracks WHICH title was drawn, not merely that something was.

    Clicking a second cover while the modal is open changes the title without
    ever passing through the closed state, so a boolean guard would leave the
    first book's reviews on screen under the second book's name.
    """
    for name, path in (("template", TEMPLATE), ("generated", GENERATED)):
        block = observer_block(read(path))
        assert "modal-title" in block, (
            f"{name}: the observer no longer reads the modal title."
        )
        assert re.search(r"title\s*[!=]==?\s*_modalRenderedFor", block), (
            f"{name}: the render guard no longer compares the title it drew, so "
            "opening a second book without closing the first would draw nothing."
        )


def test_the_REBUILD_actually_happened():
    """🔴 THE FAILURE THIS REPO HAS PAID FOR BEFORE.

    `site/index.html` is GENERATED from `app/web/templates/index.html`
    (docs/info/SITE_DATA.md). A fix typed into the template and never rebuilt
    reaches nobody; a fix typed into the generated file is erased by the next
    catalog run. The observer must match in both.
    """
    assert observer_block(read(TEMPLATE)) == observer_block(read(GENERATED)), (
        "app/web/templates/index.html and site/index.html disagree about the "
        "modal observer — rebuild the site (or apply the edit to both)."
    )


# --------------------------------------------------------------------------- #
# ...and it does not re-download the estate to draw one book's reviews
# --------------------------------------------------------------------------- #
def test_reviews_are_fetched_filtered():
    """886 documents and 272 KB, per modal open, to show two reviews.

    The removed comment claimed a client-side filter avoided "Firestore index
    requirements". It is not true for a SINGLE-FIELD equality filter: Firestore
    indexes every field automatically and only composite queries need a declared
    index. This repo has no `firestore.indexes.json` at all, and
    `site/user-warnings.js` has been running the identical
    `where('bookId','==',…)` shape in production the whole time. The filtered
    query was measured live at 147 ms against 410-424 ms for the full read.
    """
    code = strip_comments(read(REVIEWS_JS))
    fn = code[code.find("export async function getReviews("):]
    fn = fn[: fn.find("\nexport ")]
    assert "where('bookId', '==', bookId)" in fn, (
        "getReviews no longer filters server-side; it is downloading every "
        "review in the estate on every book-modal open."
    )
    assert not re.search(r"getDocs\(\s*collection\(", fn), (
        "getReviews is reading a whole collection again. Its cost must stay "
        "O(reviews for this book), never O(all reviews)."
    )
