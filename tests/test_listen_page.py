"""
The player's wiring guard — audio player phase 2 (2026-09-02).

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


def test_the_player_does_not_write_a_reading_position():
    """⚠️ A PHASE BOUNDARY, NOT AN OVERSIGHT. Positions are phase 3 and are
    gated on a firestore.rules deploy plus a live smoke test. A position
    written against rules that refuse it fails SILENTLY and looks exactly like
    "the player does not save your spot" (design §1.4, §7.4)."""
    body = strip_comments(read(LISTEN_JS))
    for forbidden in ("reading-position.js", "createPositionKeeper", "readingPositions"):
        assert forbidden not in body, (
            f"{forbidden} in listen.js — positions are phase 3, and the rules "
            "must ship and be smoke-tested before the first write"
        )


def test_the_page_says_what_it_does_not_do_yet():
    """The two phase boundaries are stated to the person, not left to be
    discovered: no saved position, no offline."""
    body = strip_comments(read(LISTEN_JS))
    assert "does not save your spot yet" in body
    assert "does not work offline" in body


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


def test_the_modal_keeps_the_shelf_link():
    """⚠️ The player link is ADDITIVE. Another session routed playback to the
    Audiobookshelf shelf on 2026-08-21; this phase does not revert that, and
    which of the two surfaces survives is the owner's call.

    ⚠️ The shelf link MOVED on 2026-09-02 — out of the `m-audio` row and into
    the modal's action row as `#m-book-shelf`, where the owner asked for it.
    That is the opposite of reverting it: the two offers now sit in different
    places instead of stacked in one row, and "Listen here" keeps `m-audio` to
    itself. This test still guards the thing it was written to guard — that the
    shelf link is not deleted in favour of the estate's own player."""
    body = read(INDEX_TEMPLATE)
    assert 'id="m-book-shelf"' in body
    assert "renderShelfButton" in body
    # …and the audio row still offers the estate player beside it.
    assert "renderAudioRow" in body
