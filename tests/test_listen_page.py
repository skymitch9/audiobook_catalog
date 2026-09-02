"""
The player's wiring guard — audio player phases 2 + 3 (2026-09-02).

WHY IT EXISTS, and it is `test_reader_page.py`'s reason word for word:
everything the browser player depends on is a CONVENTION rather than an
import — a page copied into `site/` by a tuple in writers.py, a module loaded
by filename, a CSP applied by a path in `_headers`, and a wire format written
out twice because a service worker cannot import from a page. None of that
typechecks, none of it fails a build, and every one of them fails in
production and nowhere else.

⚠️ STATED PLAINLY, BECAUSE IT MATTERS MORE THAN THE GREEN TICK: **nothing here
proves a single second of audio plays.** No test in any language can. It has
to be opened by a signed-in person, on the dev lane, with ears. What these pin
is that the pieces are still connected to each other — and, specifically, the
handful of ways this feature can break SILENTLY:

  * a page whose logic moved inline, which only the production CSP blocks;
  * a CSP missing `media-src`, which is silence and no error;
  * a service worker registered at the wrong scope, which is a dev page
    changing production behaviour;
  * the IndexedDB key drifting between the page and the worker, which is a
    bearer that is written and never read — i.e. a dead play button;
  * `user.getIdToken()` coming back, which is the TypeError the ebook reader
    shipped to every signed-in person.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "app" / "web" / "templates" / "listen.html"
SITE_PAGE = REPO / "site" / "listen.html"
LISTEN_JS = REPO / "site" / "listen.js"
SEAM_JS = REPO / "site" / "audio-seam.js"
SW_JS = REPO / "site" / "audio-sw.js"
CHAPTERS_JS = REPO / "site" / "audio-chapters.js"
PREFS_JS = REPO / "site" / "audio-prefs.js"
MEDIA_JS = REPO / "site" / "media-session.js"
HEADERS = REPO / "site" / "_headers"
INDEX_TEMPLATE = REPO / "app" / "web" / "templates" / "index.html"


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def strip_comments(text: str) -> str:
    """HTML comments and JS block/line comments removed.

    ⚠️ Needed because these files EXPLAIN the rules they follow, at length,
    and a naive substring search finds the explanation and calls it a
    violation. test_reader_page.py records the same trap.
    """
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)


# ---------------------------------------------------------------------------
# The page exists, and the build copies it
# ---------------------------------------------------------------------------
def test_the_template_exists():
    assert TEMPLATE.exists(), "app/web/templates/listen.html is the source of truth for the page"


def test_writers_copies_the_page_into_site():
    """⚠️ Without this tuple entry the page is never deployed, and the only
    symptom is a 404 on a link that looks perfectly correct in the repo."""
    from app.writers import STATIC_TEMPLATE_PAGES

    assert "listen.html" in STATIC_TEMPLATE_PAGES


def test_the_deployed_copy_matches_the_template():
    """`site/listen.html` is BUILD OUTPUT. An edit made there is wiped by the
    next catalog build, so a drift here means somebody edited the wrong file
    and their change is living on borrowed time."""
    assert SITE_PAGE.exists(), "run the site build (or the writers copy step)"
    assert read(SITE_PAGE) == read(TEMPLATE)


def test_every_player_module_exists():
    for p in (LISTEN_JS, SEAM_JS, SW_JS, CHAPTERS_JS, PREFS_JS, MEDIA_JS):
        assert p.exists(), f"{p.name} is imported by the player and must ship"


# ---------------------------------------------------------------------------
# The CSP — the directive whose absence is silence
# ---------------------------------------------------------------------------
def _listen_rules() -> dict[str, str]:
    """Every `/listen` rule in _headers, path -> its CSP string."""
    out: dict[str, str] = {}
    current = None
    for line in read(HEADERS).splitlines():
        if line.startswith("/"):
            current = line.strip()
        elif current and "Content-Security-Policy:" in line:
            out[current] = line.split("Content-Security-Policy:", 1)[1].strip()
    return {k: v for k, v in out.items() if "listen" in k}


def test_all_four_listen_rules_are_present():
    """⚠️ BOTH LANES AND BOTH SLASH FORMS. `/dev/` is a PATH, not a host, so a
    `/listen` rule does NOT cover `/dev/listen`; and Cloudflare 308s the
    trailing-slash form, which a rule matching only the canonical URL leaves
    bare. Cloudflare's `*` is a path splat, not a glob."""
    rules = _listen_rules()
    for path in ("/listen", "/listen/", "/dev/listen", "/dev/listen/"):
        assert path in rules, f"{path} has no Content-Security-Policy in site/_headers"


def test_the_four_rules_are_byte_identical():
    """Four policy strings that differ by one token is a trap nobody spots in
    review — the /read block in _headers says exactly this, having nearly
    shipped it."""
    values = set(_listen_rules().values())
    assert len(values) == 1, "the four /listen CSP rules have drifted apart"


def test_media_src_names_the_audio_host():
    """🔴 THE DIRECTIVE WHOSE ABSENCE IS SILENCE AND NO ERROR.

    An `<audio src>` is governed by `media-src`, NOT by `connect-src`, and
    `default-src 'none'` blocks it. Omit this and the page renders perfectly,
    the probe passes, and no sound ever comes out.
    """
    for path, csp in _listen_rules().items():
        assert "media-src" in csp, f"{path} has no media-src"
        media = csp.split("media-src", 1)[1].split(";", 1)[0]
        assert "https://audiobook-api.heygabi.ai" in media, f"{path}: media-src does not name the audio host"


def test_worker_src_allows_the_bearer_injector():
    """⚠️ Service-worker registration is governed by `worker-src`. Blocked, it
    rejects, no controller ever arrives, every range goes out bare, and the
    401 surfaces as a bare `error` event — the silently dead play button."""
    for path, csp in _listen_rules().items():
        assert "worker-src" in csp, f"{path} has no worker-src"
        assert "'self'" in csp.split("worker-src", 1)[1].split(";", 1)[0]


def test_connect_src_keeps_self():
    """⚠️ NOT PADDING. `default-src 'none'` blocks SAME-ORIGIN fetches too, and
    this page fetches `chapters.json`. The reader ate this exact bug."""
    for path, csp in _listen_rules().items():
        connect = csp.split("connect-src", 1)[1].split(";", 1)[0]
        assert "'self'" in connect, f"{path}: connect-src is missing 'self'"
        assert "https://audiobook-api.heygabi.ai" in connect


def test_frame_ancestors_is_self_not_none():
    """⚠️ 'none' was the P1 blank-reader bug on every WebKit browser. The
    reasoning is written out in _headers; this pins the value so nobody
    "restores it for safety"."""
    for path, csp in _listen_rules().items():
        assert "frame-ancestors 'self'" in csp, f"{path}: frame-ancestors must be 'self'"


def test_script_src_has_no_unsafe_inline():
    for path, csp in _listen_rules().items():
        script = csp.split("script-src", 1)[1].split(";", 1)[0]
        assert "'unsafe-inline'" not in script, f"{path}: script-src must not allow inline"


# ---------------------------------------------------------------------------
# The page carries no logic
# ---------------------------------------------------------------------------
def test_the_page_has_no_inline_script():
    """⚠️ The CSP above forbids it, so an inline <script> here would be blocked
    in production and NOWHERE ELSE — including on the dev lane, which ships no
    CSP at all. The worst kind of bug to find.

    ⚠️ Comments are stripped FIRST — this page explains the rule in an HTML
    comment that contains the words `<script>`, and the naive version of this
    test found its own explanation and called it a violation. The reader's
    guard records the same trap.
    """
    html = strip_comments(read(TEMPLATE))
    for match in re.finditer(r"<script\b([^>]*)>(.*?)</script>", html, flags=re.DOTALL):
        attrs, body = match.group(1), match.group(2)
        assert "src=" in attrs, "listen.html must not carry an inline <script>"
        assert body.strip() == "", "listen.html must not carry an inline <script> body"


def test_the_page_loads_listen_js_as_a_module():
    html = read(TEMPLATE)
    assert 'type="module"' in html and 'src="listen.js"' in html


def test_every_asset_reference_is_relative():
    """⚠️ The dev lane is a PATH. An absolute `/listen.js` on /dev/listen loads
    the PROMOTED module — a dev page running production code, silently."""
    html = read(TEMPLATE)
    for m in re.finditer(r'\b(?:src|href)="(/[^/][^"]*)"', html):
        raise AssertionError(f"root-absolute asset reference {m.group(1)!r} breaks the /dev/ lane")


# ---------------------------------------------------------------------------
# The wire format written twice
# ---------------------------------------------------------------------------
def test_the_indexeddb_names_match_between_page_and_worker():
    """🔴 THE DUPLICATION THIS TEST EXISTS FOR.

    A service worker has its own module graph and CANNOT import from the page,
    so the IndexedDB database/store/key are written out in BOTH
    `audio-seam.js` and `audio-sw.js`. If they drift, the page writes a token
    the worker never finds, every range goes out bare, and the symptom is a
    play button that does nothing — with no error on either side.
    """
    seam, sw = read(SEAM_JS), read(SW_JS)

    def const(text: str, name: str) -> str:
        m = re.search(rf"{name}\s*=\s*'([^']+)'", text)
        assert m, f"{name} not found"
        return m.group(1)

    for name in ("DB_NAME", "STORE_NAME", "TOKEN_KEY"):
        assert const(seam, name) == const(sw, name), f"{name} has drifted between the page and the worker"

    m_seam = re.search(r"DB_VERSION\s*=\s*(\d+)", seam)
    m_sw = re.search(r"DB_VERSION\s*=\s*(\d+)", sw)
    assert m_seam and m_sw and m_seam.group(1) == m_sw.group(1), "DB_VERSION has drifted"


def test_the_worker_checks_the_ORIGIN_not_only_the_path():
    """🔴 A BEARER IS ONLY AS SAFE AS THE LIST OF HOSTS IT IS HANDED TO.

    Matching on `pathname` alone would attach the household's Firebase ID
    token to a request for `/api/audio/<x>/file` on ANY origin. The gate is
    the origin first.
    """
    sw = strip_comments(read(SW_JS))
    assert "AUDIO_API_ORIGIN" in sw
    assert "url.origin === AUDIO_API_ORIGIN" in sw


def test_the_worker_returns_the_response_verbatim():
    """⚠️ WebKit's media loader REJECTS a 200 answering a range request (bug
    184447). Rewriting the status, re-wrapping the body or stripping
    Content-Range is "the audio silently will not play in Safari"."""
    sw = strip_comments(read(SW_JS))
    assert "return fetch(authed);" in sw, "the worker must return the upstream response unchanged"
    for forbidden in ("new Response(", "res.body", "status: 200"):
        assert forbidden not in sw, f"the worker must not construct a response ({forbidden})"


def test_the_worker_re_applies_the_range_header_by_hand():
    """⚠️ Design §3.2 item 1: constructing a new Request historically DROPPED
    `Range`. A dropped Range is a 200, and a 200 here is a 601 MB download."""
    sw = strip_comments(read(SW_JS))
    assert "headers.set('Range', range)" in sw


# ---------------------------------------------------------------------------
# The bugs this estate has already paid for
# ---------------------------------------------------------------------------
def test_the_player_never_calls_getIdToken_on_a_snapshot():
    """⚠️ `getLiveUser()` answers a flat SNAPSHOT with NO `getIdToken` method.
    The ebook reader called it anyway and threw a TypeError for every
    signed-in person, reported as "the shelf did not answer" — an outage
    sentence for something that was not an outage. If `user.getIdToken`
    reappears, it is that bug coming back."""
    for js in (LISTEN_JS, SEAM_JS):
        body = strip_comments(read(js))
        assert "user.getIdToken" not in body, f"{js.name}: use identity.getIdToken(app)"


def test_the_service_worker_scope_is_derived_not_hard_coded():
    """🔴 A hard-coded '/' scope registered from /dev/listen installs the
    PROMOTED worker and gives it control of the PROMOTED site."""
    seam = strip_comments(read(SEAM_JS))
    assert "swPaths" in seam
    assert "register('/audio-sw.js'" not in seam
    assert "scope: '/' }" not in seam
    listen = strip_comments(read(LISTEN_JS))
    assert "ensureController(navigator, location.pathname)" in listen


def test_every_seek_goes_through_one_function():
    """⚠️ Design §8's cross-cutting note, traced to a real defect in the ebook
    reader: a second page-turn path bypassed the position keeper and stopped
    saving the spot, silently. Here there are seven ways to move, so a direct
    `currentTime =` assignment outside `seekTo` is that bug being re-created.
    """
    body = strip_comments(read(LISTEN_JS))
    assignments = re.findall(r"\baudio\.currentTime\s*=", body)
    assert len(assignments) == 1, (
        "audio.currentTime is assigned outside seekTo() — every seek path must "
        "funnel through one function (design §8)"
    )


def test_the_probe_runs_before_a_play_button_exists():
    """🔴 §3.2 item 5 is MANDATORY, not advisory. If the element is given a
    src before the probe answers, a refusal is a dead button instead of a
    sentence."""
    body = strip_comments(read(LISTEN_JS))
    probe_at = body.index("await probe(")
    src_at = body.index("audio.src =")
    assert probe_at < src_at, "the HEAD probe must answer before <audio> gets a src"


# ---------------------------------------------------------------------------
# PHASE 3 — save your spot
#
# ⚠️ THE WHOLE PHASE FAILS SILENTLY OR NOT AT ALL. There is no error state
# between "the spot is saved" and "the spot is not saved" — a person simply
# reopens a 30-hour book at the beginning, three weeks later, and concludes the
# player is bad. So what is pinned here is every seam whose breakage is
# invisible from inside the browser.
# ---------------------------------------------------------------------------
def test_the_player_reuses_the_one_position_store():
    """Design §7.4: *"one new `kind`, not a new store"*. A second
    implementation of "save your spot" would be a second doc-id convention, a
    second reconcile and a second set of manners — and `firestore.rules` reads
    the uid back out of the ONE id shape `reading-position.js` writes."""
    body = strip_comments(read(LISTEN_JS))
    assert "./reading-position.js" in body
    assert "createPositionKeeper" in body
    # ⚠️ The collection name must NOT be re-typed here: `col(POSITION_COLLECTION)`
    # is what lane-suffixes it, and a literal would write prod's collection
    # from the dev lane.
    assert "'readingPositions'" not in body, (
        "listen.js names the positions collection directly — that bypasses the "
        "lane suffix and writes prod data from /dev/"
    )


def test_the_stored_locator_is_a_chapter_and_an_offset():
    """🔴 Design §7.4: store `{chapter, offsetSec}`, NEVER a single absolute
    second. An absolute offset is a position in the FILE, and a re-encode or a
    boundary correction moves it silently."""
    body = strip_comments(read(LISTEN_JS))
    assert "toLocator(" in body and "resolveLocator(" in body
    assert "kind: 'audio'" in body, "the locator's kind must travel with its value"


def test_the_position_write_hangs_off_the_seek_funnel():
    """⚠️ The reason `seekTo` exists at all (design §8, reader-page.md §7.6).
    The write must be INSIDE the one function every seek path calls, so an
    eighth path added later inherits it instead of silently skipping it."""
    body = strip_comments(read(LISTEN_JS))
    fn = body.split("function seekTo(", 1)[1].split("\nfunction ", 1)[0]
    assert "recordPosition(" in fn, (
        "seekTo() no longer records the position — a new seek path would stop "
        "saving the spot, silently, which is the exact bug reader-page.md §7.6 "
        "records shipping in the ebook reader"
    )


def test_the_position_is_written_at_every_moment_the_design_names():
    """Design §8 #1, verbatim: *"write on pause, on chapter change, on
    `pagehide`/`visibilitychange`, and every ~15 s while playing
    (throttled)"*. ⚠️ `visibilitychange` is the one that is easy to leave out
    and the one that matters most on a phone: a backgrounded tab is routinely
    killed without ever firing an unload event."""
    body = strip_comments(read(LISTEN_JS))
    for moment in ("'pause'", "'timeupdate'", "'pagehide'", "'visibilitychange'"):
        assert moment in body, f"nothing saves the spot on {moment}"
    assert "RECORD_INTERVAL_MS" in body, "the ~15 s throttle is not applied"


def test_a_remote_position_is_OFFERED_and_never_applied_over_a_local_one():
    """⚠️ `reading-position.js` §4 — cross-device sync that relocates somebody
    without asking is the single most common complaint about every reader ever
    shipped, and a player is worse because it moves while you are listening."""
    body = strip_comments(read(LISTEN_JS))
    assert "offerResume(" in body
    for piece in ("newerOf(", "samePlace("):
        assert piece in body, f"{piece} missing — the reconcile is not the one in §4"
    page = strip_comments(read(TEMPLATE))
    assert 'id="ls-resume"' in page and 'id="ls-resume-jump"' in page
    assert 'id="ls-resume-stay"' in page, "'Stay' must be said out loud, not implied"


def test_the_keeper_is_armed_only_after_the_book_has_loaded():
    """⚠️ The guard that stops a failed open overwriting a good position —
    reader.js arms only once a page has genuinely rendered, for the same
    reason. A book that would not play must cost nobody their place in it."""
    body = strip_comments(read(LISTEN_JS))
    fn = body.split("function restorePosition(", 1)[1].split("\n/**", 1)[0]
    assert "arm()" in fn
    assert fn.index("seekToStored(local") < fn.index("arm()"), (
        "the keeper is armed before the restore — the restore would then record "
        "itself and make the local row look newer than the remote one"
    )


def test_an_unresolvable_saved_spot_is_refused_in_words_not_silently_zeroed():
    """🔴 `resolveLocator` answers null when the saved chapter is gone, and a
    null is a REFUSAL, not a zero. Silently restarting a 30-hour book from the
    beginning is the failure this phase exists to prevent, and the estate's
    rule is that a person never meets a silent one."""
    body = strip_comments(read(LISTEN_JS))
    fn = body.split("function seekToStored(", 1)[1].split("\n/**", 1)[0]
    assert "showError(" in fn, "an unresolvable position is swallowed"
    assert "Nothing was lost" in body, "the refusal must say the spot is still saved"


def test_the_per_book_speed_now_rides_the_position_document():
    """Design §9.2 #2 and the TODO's phase-3 item 4. ⚠️ The localStorage copy
    STAYS as the first-paint cache — deleting it would put the network on the
    critical path of "start my book at the right speed"."""
    body = strip_comments(read(LISTEN_JS))
    assert "rate: state.rate" in body, "the speed never reaches the document"
    assert "applyStoredRate(" in body
    assert "setBookRate(" in body, "the local first-paint cache was dropped"
    prefs = read(PREFS_JS)
    assert "getBookRate" in prefs and "setBookRate" in prefs


def test_the_eviction_shield_is_stamped_and_is_not_the_ping_that_was_rejected():
    """🔴 THE MID-BOOK SHIELD — `audio_positions/{anchor}`, epoch
    MILLISECONDS, read by fulfill_audio_requests as `last_position_at`.

    ⚠️ IT LOOKS LIKE THE `stream-ping` THE PHASE-2 WIP WAS REJECTED FOR, AND
    THE DIFFERENCE IS WORTH STATING RATHER THAN LEAVING TO BE RE-ARGUED. That
    one was a client-driven Worker route claiming *"somebody streamed bytes"* —
    a fact the Worker itself holds and can therefore stamp truthfully, which is
    what design §10.1 has it do. A saved POSITION is the opposite: no Worker
    ever sees it, so the browser is the only party with anything true to say.
    It is throttled far coarser than a ping (one write per anchor per ten
    minutes against a 30-DAY question), it carries no title, path or uid, and
    the rules refuse the two forgeries that are not benign — a stamp dragged
    backwards, and a stamp parked in the future."""
    body = strip_comments(read(LISTEN_JS))
    assert "stampBody(" in body and "STAMP_COLLECTION" in body
    assert "shouldStamp(" in body, "the stamp is not throttled"
    assert "stream-ping" not in body
    # ⚠️ The lane suffix, again: a dev-lane stamp landing in prod's collection
    # would shield the wrong lane's objects.
    assert "col(STAMP_COLLECTION)" in body


def test_the_page_still_says_what_it_does_not_do_yet():
    """Offline is still a phase boundary and is still stated to the person.
    ⚠️ And the SAVED-SPOT sentence has been replaced rather than deleted: the
    page should now say it saves, not go quiet about it."""
    body = strip_comments(read(LISTEN_JS))
    assert "does not work offline" in body
    assert "does not save your spot yet" not in body, (
        "the page still tells people their spot is not saved, and it now is"
    )
    assert "Your spot is saved" in body


def test_the_footer_disclosure_was_narrowed_rather_than_left_to_go_stale():
    """⚠️ It used to read "Nothing is downloaded to this device", which stopped
    being the whole truth the moment a position was cached locally. The claim
    that MATTERS — no book files — is kept, and the rest is said."""
    page = read(TEMPLATE)
    assert "no book files are kept on this device" in page
    assert "Nothing is downloaded to this device." not in page


def test_no_invented_worker_route_is_called():
    """⚠️ THE WIP THIS PHASE REPLACED called `POST /api/audio/:anchor/stream-ping`,
    a route that does not exist on the Worker and that the design does not
    specify. Design §10.1 puts the eviction stamp on the BYTE ROUTE, written
    by the Worker's own service account and throttled per isolate — never a
    client-driven ping, which is both spoofable and a request per listener.
    """
    for js in (LISTEN_JS, SEAM_JS):
        body = strip_comments(read(js))
        assert "stream-ping" not in body, f"{js.name} calls a Worker route that does not exist"


# ---------------------------------------------------------------------------
# The catalogue's way in
# ---------------------------------------------------------------------------
def test_the_modal_links_to_the_player_relatively():
    """⚠️ Relative and extensionless: `listen?b=` so the /dev/ lane opens the
    /dev/ player, and no `.html` (Cloudflare 308s it) and no trailing slash
    (which would re-base every asset on that page)."""
    body = read(INDEX_TEMPLATE)
    assert "'listen?b=' + encodeURIComponent(anchor)" in body
    assert "listen.html?b=" not in body
    assert "/listen?b=" not in body


def test_the_modal_offers_only_the_estate_player():
    """🔴 INVERTED 2026-09-02 ON THE OWNER'S ORDER, and renamed with it.

    This test used to be `test_the_modal_keeps_the_shelf_link`, and it existed
    because phase 2's player was deliberately ADDITIVE: it guarded the ABS
    "Open on the shelf" link against being deleted in favour of the estate's
    own player, while recording in its own docstring that *which of the two
    surfaces survives is the owner's call*.

    He called it, verbatim: *"for now we want to use only the listen/download
    here button in the audiobook catalog."* So the guard flips — the modal's
    one play control is ▶ Listen here, and the ABS button must stay off this
    page until he says otherwise.

    ⚠️ PRESENTATION ONLY. `site/shelf-link.js` is NOT deleted; it is the
    canonical catalog→shelf join, still exercised by
    `site/__tests__/shelf-link.test.js` and `tests/test_shelf_map.py`, and the
    reader port (docs/TODO.md) is built on it. This test is about what the
    catalogue modal PAINTS, not about whether the join exists."""
    body = strip_comments(read(INDEX_TEMPLATE))
    assert 'id="m-book-shelf"' not in body, (
        "the ABS shelf button is back in the modal — owner, 2026-09-02: only "
        "the listen/download here button"
    )
    assert "renderShelfButton" not in body
    assert "shelf-link.js" not in body, "the modal re-imported the shelf join"
    # …and the estate player is what stands in its place.
    assert "renderAudioRow" in body
    assert "'▶ Listen here'" in body


def test_removing_the_button_did_not_remove_the_join():
    """⚠️ The other half of the test above, and the reason it is a SEPARATE
    test: "presentation only" is a claim, and a claim about a deletion is worth
    exactly as much as the guard under it.

    The owner said *"for now"*. If a later session reads the button's removal
    as permission to delete the join it called, the reader port
    (docs/TODO.md — "Port the EPUB + PDF readers to the SHELF") loses the one
    canonical implementation it is designed around, and the next person writes
    a second one that emits `/audiobookshelf/item/<uuid>` links — the exact
    shape that 404'd for 1,077 books after the hardlink reshape."""
    module = REPO / "site" / "shelf-link.js"
    assert module.exists(), (
        "site/shelf-link.js is gone — the modal button was removed, the "
        "canonical catalog→shelf join was NOT meant to be"
    )
    src = read(module)
    for fn in ("shelfLinkFor", "normalizeShelfMap", "shelfSearchUrl"):
        assert f"export function {fn}" in src or f"export {{" in src, (
            f"shelf-link.js no longer exports {fn}"
        )
    # The ebooks page registers the join through its own seam and is a
    # DIFFERENT surface — the owner's order named the audiobook catalog only.
    assert "useShelfJoin" in read(REPO / "app" / "web" / "templates" / "ebooks.html")
