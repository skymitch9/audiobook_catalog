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
    assert "./audio-request.js" in src
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
    assert "./audio-request.js" in generated
    assert "renderAudioRow" in generated


def test_the_row_renders_on_modal_open():
    """The modal's MutationObserver is the only thing that calls it."""
    src = strip_comments(read(TEMPLATE))
    assert "renderAudioRow(title)" in src, (
        "nothing calls renderAudioRow on modal open, so the row never appears"
    )


# --------------------------------------------------------------------------- #
# ⚠️ PHASE 2 IS BUILT — the player lives in audio-player.js
# --------------------------------------------------------------------------- #
def test_the_player_module_exists():
    """Phase 2: audio-player.js provides the renderAudioPlayer function."""
    player_module = REPO / "site" / "audio-player.js"
    assert player_module.exists(), "site/audio-player.js is gone; the modal's player import 404s"


def test_the_service_worker_exists():
    """Phase 2: audio-sw.js is the auth seam service worker."""
    sw = REPO / "site" / "audio-sw.js"
    assert sw.exists(), "site/audio-sw.js is gone; the player cannot inject auth tokens"


def test_the_player_is_rendered_for_streamable_books():
    """Phase 2 replaced the 'player coming' placeholder with the actual player."""
    src = strip_comments(read(TEMPLATE))
    assert "renderAudioPlayer" in src, (
        "renderAudioRow no longer calls renderAudioPlayer for streamable books"
    )


# --------------------------------------------------------------------------- #
# ⚠️ THE COPY — the wait is EIGHT HOURS, and saying "within the hour" is a lie
# --------------------------------------------------------------------------- #
def test_the_wait_is_described_honestly():
    """The design's first draft said "requested, usually ready within the hour".

    The pipeline that fulfils this queue runs every EIGHT HOURS (sync step 5.9,
    excluded from --rebuild-only on purpose). A person told "within the hour"
    who waits three concludes the button is broken — and then presses it again,
    which the duplicate clause absorbs but which nobody should have to do.
    """
    # ⚠️ strip_comments, because both files EXPLAIN this rule by quoting the
    # forbidden phrase — and a naive search finds the explanation and calls it
    # the copy. It did exactly that when this test was written.
    src = strip_comments(read(TEMPLATE)) + strip_comments(read(MODULE))
    assert "within the hour" not in src, (
        "the copy promises an hour; the pipeline runs every eight. Say a few hours."
    )
    assert "every eight hours" in src, "the copy no longer says how long the wait really is"


def test_the_first_rung_is_the_designs_words():
    src = read(TEMPLATE)
    assert "Not streamable yet — request it" in src


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
    """The streaming UI is replaced by the shelf link (K8/K12, 2026-08-21).
    The shelf button renders for everyone — ABS's own Cloudflare Access gate
    handles who can actually open it."""
    src = strip_comments(read(TEMPLATE))
    # The shelf button renders unconditionally; auth is on ABS's side
    assert "Play / Download in Shelf" in src or "Find in Shelf" in src
