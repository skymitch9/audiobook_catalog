"""An empty-looking TBR list must say WHY — the silent-void guard.

WHAT HAPPENED, 2026-08-19. The owner reported "my TBR list is empty" on
audiobooks.heygabi.ai. The first hypothesis was a failed or timed-out Firestore
read rendering as an empty list. It was NOT. Measured directly in Firestore the
same morning, the owner's account (uid tX912Otd…) holds:

    51 `readingLists` rows
    50 of them  status = 'read'
     1 of them  status = 'tbr'   -> bookTitle "The Court of the Dead"

and that one title matches NONE of the catalogue's 1,078 rows (checked against
site/catalog.csv; `difflib` found no near match either). So the filter matched
nothing — correctly — and the page then said nothing at all.

⚠️ THE DEFECT IS THE SILENCE, NOT THE COUNT, and it was worse from the dropdown
than from the deep link. `#list=tbr&user=…` puts a label in the search box, so
the generic `#ab-empty` state at least appeared — worded "try a different
search", which is already the wrong advice for a to-read list. The **dropdown**
sets no search text, so `qVal` was empty, `#ab-empty` stayed hidden, and
`renderPage()` painted a blank catalogue with no explanation whatsoever.

That is the estate's silent-failure rule broken in the worst direction: an empty
list is indistinguishable from lost data, and the person cannot tell which they
are looking at. An outage, an unconfirmed account, an genuinely empty list and a
list whose books are not in this catalogue are FOUR different situations with
four different fixes, and they all rendered as the same blank page.

⚠️ THE UNCONFIRMED-ACCOUNT CASE IS A REAL RACE, not a theoretical one.
`ownsReadingListDoc` is account-only since the 2026-08-18 uid migration and
FAILS CLOSED, so a null uid rejects every row. Firebase publishes a restored
session asynchronously — measured at ~340 ms of token refresh plus
accounts:lookup after load on the sibling app the same morning — so pressing
this early genuinely produces no uid, and produced a silent empty list.

⚠️ Stated plainly, in the house style: **nothing here runs the page.** These are
source-shape guards over a file whose contract is a convention. The live proof
is a signed-in person pressing "My TBR List" and reading a sentence.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "app" / "web" / "templates" / "index.html"
GENERATED = REPO / "site" / "index.html"

TARGETS = (("template", TEMPLATE), ("generated", GENERATED))


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def strip_comments(text: str) -> str:
    """HTML comments and JS block/line comments removed.

    ⚠️ Load-bearing: the function under test now carries a long comment that
    quotes its own copy and names every branch. A naive substring search finds
    the explanation and calls it the code.
    """
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)


def filter_fn(src: str) -> str:
    code = strip_comments(src)
    start = code.find("async function filterByReadingList(")
    assert start != -1, "filterByReadingList is gone"
    end = code.find("\n    var toggle=", start)
    assert end != -1, "could not find the end of filterByReadingList"
    return code[start:end]


# --------------------------------------------------------------------------- #
# There is somewhere to put a sentence, and renderPage shows it
# --------------------------------------------------------------------------- #
def test_the_notice_element_exists_and_is_separate_from_the_search_empty_state():
    """Separate from `#ab-empty` on purpose.

    That element is worded for a SEARCH, and sharing it would mean rewriting its
    innerHTML — destroying the `#ab-empty-q` span the search path still holds a
    reference to.
    """
    for name, path in TARGETS:
        src = read(path)
        assert 'id="ab-list-notice"' in src, f"{name}: the list-notice element is gone"
        assert 'id="ab-empty-q"' in src, (
            f"{name}: the search empty-state's span was destroyed; the search "
            "path still writes to it."
        )


def test_renderPage_shows_the_notice_even_with_an_EMPTY_search_box():
    """🔴 THE EXACT REGRESSION.

    The dropdown sets no search text. The old code gated the only visible
    explanation on `qVal.trim()`, so from the dropdown there was none at all.
    """
    for name, path in TARGETS:
        code = strip_comments(read(path))
        start = code.find("function renderPage(")
        assert start != -1, f"{name}: renderPage is gone"
        body = code[start : code.find("function drawPager(", start)]
        assert "listNoticeEl" in body, (
            f"{name}: renderPage no longer paints the list notice, so an empty "
            "TBR list from the dropdown renders as a silent blank catalogue."
        )
        assert re.search(r"_listNotice\s*\?\s*[\"']block[\"']", body), (
            f"{name}: the notice is never shown; it must not depend on the "
            "search box having text."
        )


def test_the_two_explanations_cannot_contradict_each_other():
    """One blank page, one reason. `#ab-empty` stands down when a notice speaks."""
    for name, path in TARGETS:
        code = strip_comments(read(path))
        start = code.find("function renderPage(")
        body = code[start : code.find("function drawPager(", start)]
        assert "!_listNotice" in body, (
            f"{name}: the generic 'try a different search' can appear alongside "
            "the list notice — two explanations of one blank page, and only one "
            "of them is true."
        )


# --------------------------------------------------------------------------- #
# Four situations, four sentences
# --------------------------------------------------------------------------- #
def test_a_FAILED_read_is_not_reported_as_an_empty_list():
    """🔴 The silent-failure rule, in the direction that matters.

    The catch used to `console.warn` and stop, leaving a filter that had failed
    looking exactly like a list with nothing on it.
    """
    for name, path in TARGETS:
        fn = filter_fn(read(path))
        tail = fn[fn.rfind("} catch(e) {"):]
        assert "setListNotice(" in tail, (
            f"{name}: a failed read no longer says anything; it renders as an "
            "empty list, which reads as data loss."
        )
        assert "renderPage()" in tail, (
            f"{name}: the failure path does not repaint, so the notice never "
            "reaches the screen."
        )


def test_an_unconfirmed_account_says_so_instead_of_rendering_empty():
    for name, path in TARGETS:
        fn = filter_fn(read(path))
        assert re.search(r"if\s*\(\s*!me\.uid\s*\)", fn), (
            f"{name}: a missing uid no longer short-circuits. ownsReadingListDoc "
            "fails closed, so this renders a silent empty list during the "
            "ordinary Firebase session-restore race."
        )


def test_a_genuinely_empty_list_NAMES_THE_ACCOUNT():
    """Two-account household: "whose list is this" is the first question.

    Owner's ask, 2026-08-19 — the wording should make the ambiguity
    self-explaining rather than something to come and ask about.
    """
    for name, path in TARGETS:
        fn = filter_fn(read(path))
        # ⚠️ Anchored on the CONCATENATION, not on the phrase. "signed in as"
        # also appears in the unconfirmed-account sentence ("…which account
        # you're signed in as, so your TBR list can't be shown"), and matching
        # that one would let the empty-list case lose the name silently.
        assert re.search(r"signed in as '\s*\+\s*targetUser", fn), (
            f"{name}: the empty-list sentence no longer names the signed-in "
            "account — in a two-account household that is the first question "
            "an empty list raises."
        )


def test_entries_that_are_not_in_THIS_catalogue_are_reported_as_such():
    """The owner's actual case, and the one nobody would guess from a blank page.

    One TBR entry, "The Court of the Dead", present in Firestore and absent from
    all 1,078 catalogue rows. Naming it is the difference between "my list is
    gone" and "ah, that book is not in here".
    """
    for name, path in TARGETS:
        fn = filter_fn(read(path))
        assert "in this catalogue" in fn, (
            f"{name}: a list whose books are all missing from the catalogue is "
            "reported the same as an empty list."
        )
        assert "_missing" in fn, (
            f"{name}: the unmatched titles are no longer named, so the person "
            "cannot tell which book is missing."
        )


def test_the_notice_is_cleared_when_the_filter_is_dropped():
    for name, path in TARGETS:
        code = strip_comments(read(path))
        start = code.find("function clearReadingListFilter()")
        assert start != -1, f"{name}: clearReadingListFilter is gone"
        assert "setListNotice(null)" in code[start : start + 600], (
            f"{name}: 'your list is empty' would survive into an ordinary "
            "sorted catalogue."
        )


def test_the_REBUILD_actually_happened():
    """`site/index.html` is GENERATED from the template — both must carry it."""
    assert filter_fn(read(TEMPLATE)) == filter_fn(read(GENERATED)), (
        "app/web/templates/index.html and site/index.html disagree about "
        "filterByReadingList — rebuild the site (or apply the edit to both)."
    )
