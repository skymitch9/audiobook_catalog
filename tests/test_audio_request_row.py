"""
The request-an-audiobook row's wiring guard — audio player PHASE 1 (2026-08-18).

WHY IT EXISTS. Same reasoning as `test_reader_page.py`: everything the browser
half depends on is a CONVENTION rather than an import. A module loaded by
filename, a template that must be REBUILT before it reaches `site/`, a Firestore
write whose shape is enforced by deployed rules nothing here can see. None of it
typechecks, none of it fails a build, and every one of them fails in production
and nowhere else.

⚠️ Stated plainly: **nothing here proves a request reaches Firestore.** That is
what `scripts/smoke_audio_request_rules.py` is for (17/17 live, 2026-08-17), and
ultimately a signed-in person pressing the button once, with eyes. What these
pin is that the pieces are still connected and that the copy still tells the
truth about how long the wait is.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "app" / "web" / "templates" / "index.html"
GENERATED = REPO / "site" / "index.html"
MODULE = REPO / "site" / "audio-request.js"


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def strip_comments(text: str) -> str:
    """HTML comments and JS block/line comments removed.

    ⚠️ Needed because these files EXPLAIN the rules they follow at length, and a
    naive substring search finds the explanation and calls it the code.
    """
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)


# --------------------------------------------------------------------------- #
# The pieces exist and reach the deployed site
# --------------------------------------------------------------------------- #
def test_the_module_exists():
    assert MODULE.exists(), "site/audio-request.js is gone; the modal's import 404s silently"


def test_the_template_imports_it_and_carries_the_slot():
    src = read(TEMPLATE)
    # audio-request.js import removed (shelf handles playback); slot remains
    assert 'id="m-audio"' in src


def test_the_REBUILD_actually_happened():
    """🔴 THE FAILURE THIS REPO HAS PAID FOR BEFORE.

    `site/index.html` is GENERATED from `app/web/templates/index.html`. Editing
    the template and forgetting `python -m app.main` leaves a change that is
    committed, reviewed, and not on the site — and the next catalog rebuild
    would have shipped it, which makes the gap look like a delay rather than a
    mistake. Both files move together or this goes red.
    """
    generated = read(GENERATED)
    assert 'id="m-audio"' in generated, (
        "site/index.html has no audio slot — the template was edited but not rebuilt. "
        "Run: python -m app.main   (PowerShell: prefix $env:PYTHONIOENCODING='utf-8')"
    )
    assert "renderAudioRow" in generated


def test_the_row_renders_on_modal_open():
    """The modal's MutationObserver is the only thing that calls it."""
    src = strip_comments(read(TEMPLATE))
    assert "renderAudioRow(title)" in src, (
        "nothing calls renderAudioRow on modal open, so the row never appears"
    )


# --------------------------------------------------------------------------- #
# ⚠️ PHASE 2 IS BUILT — and the player is a PAGE, not a module in this modal
# --------------------------------------------------------------------------- #
def test_the_player_page_exists():
    """The player lives at /listen, per design §7.1 and §10's phase table.

    ⚠️ THIS TEST REPLACED ONE THAT PINNED `site/audio-player.js`, and the
    replacement is the point. That file was WIP committed on the owner's
    "just commit everything" (`c02ce30`, whose own message says "NOT VERIFIED
    — nothing here has been run"). It rendered a player INSIDE the catalogue
    modal, drew a BOOK-relative scrub bar, and called
    `POST /api/audio/:anchor/stream-ping` — a Worker route that does not
    exist. The design of record specifies a `/listen?b=<anchor>` page with a
    CHAPTER-relative bar (requirement 7, the reason the whole feature ships no
    player library), so phase 2 built that and deleted the WIP rather than
    leave two players to disagree about which one is real. It is recoverable
    from git if the in-modal shape is ever preferred.
    """
    assert (REPO / "site" / "listen.html").exists(), "the player page is not deployed"
    assert (REPO / "site" / "listen.js").exists(), "the player's logic module is missing"
    assert (REPO / "site" / "audio-chapters.js").exists(), "the chapter model is missing"
    assert not (REPO / "site" / "audio-player.js").exists(), (
        "site/audio-player.js is back — two players is the split-brain this "
        "estate forbids; the player is /listen"
    )


def test_the_service_worker_exists():
    """Phase 2: audio-sw.js is the auth seam service worker."""
    sw = REPO / "site" / "audio-sw.js"
    assert sw.exists(), "site/audio-sw.js is gone; the player cannot inject auth tokens"


def test_the_player_is_rendered_for_streamable_books():
    """
    🔴 THE PLAY CONTROL IS THE ESTATE'S OWN PLAYER, AND ONLY IT (2026-09-02).

    History, because this one test has now pinned three different answers to
    the same question and the last one is the owner's:

    * 2026-08-18 — the modal offered a "request this audiobook" flow.
    * 2026-08-21 — an ABS "Open on the shelf" link replaced it.
    * 2026-09-02 (morning) — that link MOVED into the action row as the fourth
      button, `#m-book-shelf`, and the estate's own ▶ Listen here joined it in
      `#m-audio`. Two surfaces then answered "play this book", which this
      repo's own comments flagged by name as the owner's call to make.
    * 2026-09-02 (afternoon) — HE MADE IT, verbatim: *"for now we want to use
      only the listen/download here button in the audiobook catalog."*

    So the ABS button is off this page and `renderAudioRow` is the whole answer.
    ⚠️ PRESENTATION ONLY: `site/shelf-link.js` is untouched and still tested
    (`site/__tests__/shelf-link.test.js`, `tests/test_shelf_map.py`), and the
    ebooks page's Shelf link is a different surface, also untouched.
    """
    src = strip_comments(read(TEMPLATE))
    assert 'id="m-book-shelf"' not in src, (
        "the ABS shelf button is back in the modal — the owner asked for only "
        "the listen/download here button (2026-09-02)"
    )
    assert "renderShelfButton" not in src
    # …and the one surviving play control is still wired.
    assert 'id="m-audio"' in src
    assert "renderAudioRow" in src


# --------------------------------------------------------------------------- #
# ⚠️ THE COPY — the request queue is gone, and so is the shelf button
# --------------------------------------------------------------------------- #
def test_the_wait_is_described_honestly():
    """No request queue, so no wait-time copy — and, since 2026-09-02, no ABS
    button either. What remains is ▶ Listen here, which renders only when the
    book is genuinely streamable right now."""
    src = strip_comments(read(TEMPLATE))
    assert "renderAudioRow" in src
    assert "Not streamable yet" not in src
    assert 'id="m-book-shelf"' not in src


def test_the_first_rung_is_the_designs_words():
    """The estate player is the first and only rung — no request flow, and no
    shelf hand-off (owner, 2026-09-02)."""
    src = strip_comments(read(TEMPLATE))
    assert "'▶ Listen here'" in src
    assert 'id="m-book-shelf"' not in src


# --------------------------------------------------------------------------- #
# The write shape the DEPLOYED rules enforce
# --------------------------------------------------------------------------- #
def test_create_uses_exact_list_equality():
    """⚠️ `audioRequestIsNewPile` asserts `requesters == [uid]`, not `hasAll`.

    The weaker form let one person open a pile that already named a stranger,
    which POISONED it so the real second requester's join was then refused. The
    live smoke test caught that within ten minutes of the first rules deploy. A
    client that opens a pile with anything but exactly itself is denied.
    """
    src = strip_comments(read(MODULE))
    assert "requesters: [uid]" in src, "the create path no longer writes exactly [uid]"
    assert "requestedBy: uid" in src


def test_join_uses_arrayUnion_and_freezes_the_book():
    """⚠️ Two clauses of the deployed rule, both load-bearing.

    `hasAll(old)` forbids removing an existing requester and `hasOnly(old + me)`
    forbids adding anyone else — so a read-modify-write of the array loses races
    and gets denied. And `bookId`/`bookTitle`/`requestedBy` are frozen, because
    the fulfiller uploads whatever `bookTitle` says: an editable title on a
    shared pile would redirect everyone else's request at a different 600 MB
    file.
    """
    src = strip_comments(read(MODULE))
    assert "arrayUnion(uid)" in src, "the join path must merge server-side, not splice locally"
    join = src[src.index("const join = async"):]
    join = join[: join.index("};")]
    for frozen in ("bookTitle", "bookId", "requestedBy"):
        assert frozen not in join, (
            f"the join write sends {frozen}, which the deployed rules freeze — every join "
            "would be denied"
        )


def test_the_lost_create_race_is_retried_as_a_join():
    """Two club-mates pressing in the same second: the loser's create is
    evaluated as an update and refused. That is a lost race, not a permission
    problem, and without the retry the second person sees a dead button."""
    src = strip_comments(read(MODULE))
    assert src.count("await join()") >= 2, (
        "the create failure path no longer retries as a join — a simultaneous second "
        "requester gets a refusal instead of joining the pile"
    )


def test_the_uid_comes_from_the_enforced_identity():
    """The rules compare against `request.auth.uid`; the localStorage mirror has
    no uid and never will. A session that cannot prove one is told so, rather
    than discovering it as a PERMISSION_DENIED."""
    src = strip_comments(read(MODULE))
    assert "getLiveUser(app)" in src


def test_tokens_come_from_identity_getIdToken():
    """⚠️ `user.getIdToken()` is the phase-1b ebook bug, verbatim.

    identity.js's session snapshot is a plain object with no such method, and
    calling it threw a TypeError for every signed-in person. reader.js's header
    §5 records it; the same rule binds here.
    """
    src = strip_comments(read(MODULE))
    assert "getIdToken(app)" in src
    assert "user.getIdToken" not in src


# --------------------------------------------------------------------------- #
# The status read path
# --------------------------------------------------------------------------- #
def test_the_status_url_is_the_projection_route():
    src = strip_comments(read(MODULE))
    assert "https://audiobook-api.heygabi.ai/api/audio/status" in src
    # ⚠️ Not /api/audio/manifest. The Worker serves a five-field projection on
    # purpose and `path` is not one of them; a route called "manifest" invites
    # somebody to "fix" it into serving the whole file-by-file map of 630 GB.
    assert "/api/audio/manifest" not in src


def test_an_outage_is_never_reported_as_a_refusal():
    """⚠️ Mislabelling an outage as a permission failure sends people asking for
    access they already hold — a named estate rule, not a nicety."""
    src = strip_comments(read(MODULE))
    assert "reason: 'unavailable'" in src
    assert "reason: 'no_grant'" in src
    assert "reason: 'signed_out'" in src


def test_nothing_renders_without_the_grant():
    """⚠️ REPOINTED 2026-09-02 — the ABS button this used to describe is gone
    (owner: *"for now we want to use only the listen/download here button in
    the audiobook catalog"*), so the thing worth guarding is the estate
    player's own refusal posture, which is stricter than ABS's was.

    ▶ Listen here draws NOTHING unless the book is streamable right now. The
    three outcomes are deliberately kept apart and only one of them is a link:
    streamable → a link; signed out or no `vis_ebooks` grant → nothing,
    quietly; an OUTAGE → nothing, quietly. ⚠️ A refusing button is worse than
    an absent one, and mislabelling an outage as a permission fact sends people
    asking for access they already hold."""
    src = strip_comments(read(TEMPLATE))
    assert 'id="m-book-shelf"' not in src
    assert "renderShelfButton" not in src
    # The link is created only AFTER the gated status call resolves an anchor.
    assert "var anchor = getAnchorForBook(status, bookTitle);" in src
    assert "if (!anchor) return;" in src
