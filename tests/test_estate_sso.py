"""Estate SSO adoption in site/identity.js — the static wiring pins.

The owner's complaint, verbatim (sso-design.md §8c):

    "Ebooks makes me login every time why is it not inheriting login from
     main page?"

Firebase web auth state is per-ORIGIN, so a sign-in on heygabi.ai left
`audiobooks.heygabi.ai` — and `ebooks.heygabi.ai`, which PROXIES this repo's
page and therefore runs THIS module under that hostname — signed out. The fix
(design §4.3) is a parent-domain HttpOnly cookie traded at auth.heygabi.ai for
a Worker-minted Firebase custom token.

⚠️ WHY A STATIC SWEEP AND NOT ONLY BEHAVIOUR. The behavioural half lives in
`site/__tests__/identity.test.js` and covers the branches. What it cannot
cover is the *shape* of two mistakes that fail SILENTLY in production and pass
every test:

  1. a missing `credentials: 'include'` — the browser drops the Set-Cookie on
     the way back, or never sends the cookie, while every status code still
     reads 200 and nothing anywhere reports a thing (design §8c.2);
  2. the SSO decision drifting off the ONE chokepoint every sign-in-offering
     page passes through, so some pages inherit and others do not, with no
     failure to notice.

Both are cheap to pin by shape, and the shape is what regresses.

The signed-in hop itself is the owner's attended test — two real origins, a
real cookie, a real Google session. It is scripted in
`docs/access/ESTATE_SSO.md`; nothing here claims to have exercised it.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
IDENTITY = REPO / "site" / "identity.js"

SESSION_URL = "https://auth.heygabi.ai/api/session"
TOKEN_URL = "https://auth.heygabi.ai/api/session/token"


def _src() -> str:
    return IDENTITY.read_text(encoding="utf-8")


class TestTheEndpointsAreNamedOnce:
    def test_both_session_routes_are_module_constants(self):
        src = _src()
        assert f"const SESSION_URL = '{SESSION_URL}';" in src
        assert f"const SESSION_TOKEN_URL = '{TOKEN_URL}';" in src

    def test_no_url_is_inlined_at_a_call_site(self):
        """One name per endpoint. A pasted URL is how a lane drifts."""
        src = _src()
        # Two definitions plus the docblock mention are all that may carry the
        # literal; every fetch goes through the constant.
        assert src.count(f"'{TOKEN_URL}'") == 1
        assert src.count(f"'{SESSION_URL}'") == 1

    def test_the_custom_token_exchange_is_imported_from_the_pinned_sdk(self):
        src = _src()
        assert re.search(
            r"import \{[^}]*\bsignInWithCustomToken\b[^}]*\} from "
            r"'https://www\.gstatic\.com/firebasejs/10\.8\.0/firebase-auth\.js'",
            src,
        ), "the exchange must come from the ONE Firebase auth module this file already imports"


class TestCredentialsInclude:
    """⚠️ The nastiest failure in the whole mechanism, in both directions.

    Without `credentials: 'include'` the browser silently drops the Set-Cookie
    on the way back (publish) or never sends the cookie at all (inherit) —
    and every status code still reads 200. There is no error, no log, and no
    test that would notice; the estate simply never learns anyone signed in.
    """

    def test_every_session_route_fetch_sends_credentials(self):
        src = _src()
        # Each of the three calls (POST publish, POST mint, DELETE sign-out).
        assert src.count("credentials: 'include'") == 3, (
            "publish, inherit and the sign-out DELETE must all carry "
            "credentials:'include' — a missing one fails silently with a 200"
        )

    def test_publish_sends_a_bearer_and_inherit_sends_no_body(self):
        src = _src()
        assert "headers: { Authorization: 'Bearer ' + token }" in src
        # The mint route is authorised by the cookie alone; a body would be a
        # second, forgeable input to a decision the cookie already settles.
        assert (
            "fetch(SESSION_TOKEN_URL, { method: 'POST', credentials: 'include' })" in src
        )

    def test_sign_out_deletes_the_cookie(self):
        src = _src()
        assert "fetch(SESSION_URL, { method: 'DELETE', credentials: 'include' })" in src
        assert "await endEstateSession();" in src, (
            "signOutGoogle must clear the estate cookie, or the sign-in keeps "
            "travelling to every other surface after the person signed out"
        )


class TestTheChokepoint:
    """The decision rides ONE listener, reached from the one call every page makes.

    `handleRedirectResult()` is this module's standing every-page-on-load rule
    (a page that skips it drops redirect sign-ins on the floor). It attaches
    the auth mirror; the mirror's FIRST answer is when "is this browser signed
    in here?" first becomes answerable, so that is where the SSO decision
    sits. A new page inherits SSO by obeying a rule it already had to obey.
    """

    def test_the_decision_hangs_off_the_mirror_listener_first_answer(self):
        src = _src()
        assert re.search(
            r"if \(!_ssoDecided\) \{\s*\n\s*_ssoDecided = true;\s*\n\s*startEstateSso\(app, user\);",
            src,
        ), "the SSO decision must be taken once, on the auth mirror's first answer"

    def test_sso_does_NOT_open_a_second_auth_listener(self):
        """One subscription. Two would race on the same question."""
        src = _src()
        # attachAuthMirror + liveUser are the only two subscribers, and both
        # predate SSO. startEstateSso takes the user it is handed.
        assert src.count("onAuthStateChanged(") == 2
        assert re.search(
            r"export async function startEstateSso\(app, user\)", src
        ), "startEstateSso takes the published user rather than waiting for its own"

    def test_an_interactive_sign_in_publishes_so_it_can_travel(self):
        src = _src()
        assert "await publishEstateSession(app, user);" in src


class TestItNeverBecomesAWall:
    """This site is PUBLIC and SSO must never turn into a gate or a loop.

    Standing non-negotiable (sso-design.md §5): the bootstrap is silent, and
    failure means "stay anonymous", never a prompt, an error or a redirect.
    """

    def test_every_sso_function_answers_false_rather_than_throwing(self):
        src = _src()
        for fn in ("publishEstateSession", "inheritEstateSession", "startEstateSso"):
            body = _function_body(src, fn)
            assert "catch (e)" in body, f"{fn} must swallow its own failures"
            assert "return false" in body, f"{fn} must degrade to false, never throw"

    def test_no_sso_path_navigates_or_reloads(self):
        """A sign-in loop is the one unacceptable failure mode."""
        for fn in ("publishEstateSession", "inheritEstateSession", "startEstateSso"):
            body = _function_body(_src(), fn)
            assert "location." not in body, f"{fn} must never navigate"

    def test_no_sso_path_words_a_refusal_to_the_visitor(self):
        """401/403/503 are all the same to a page: stay signed out, silently.

        There is nothing a visitor could do about any of them, so surfacing
        one would be noise wearing an error's clothes.
        """
        body = _function_body(_src(), "inheritEstateSession")
        for shout in ("alert(", "console.error", "throw "):
            assert shout not in body


class TestTheLegacyGuard:
    """Owner decision Q5: silent sign-in YES, but never over a legacy v1 row.

    A legacy mirror is a name captured before 2026-08-14 with no live session
    behind it. It is the name that person's reviews are filed under. Inheriting
    would replace it with whatever account the estate cookie names, without a
    word — so the existing one-time "Sign in" upgrade button stays the ONE
    door out of a legacy session, because it is a door they choose.
    """

    def test_the_guard_is_present_and_checks_both_untouchable_mirrors(self):
        body = _function_body(_src(), "startEstateSso")
        assert "session.legacy" in body
        assert "marker === 'stub'" in body, (
            "the dev-lane stub is a test fixture; materialising a real session "
            "under an automated flow would replace the identity it asserts on"
        )
        assert "return false;" in body

    def test_the_guard_is_documented_as_an_owner_decision(self):
        src = _src()
        assert "Q5" in src, (
            "the guard must name the decision it implements — it looks like a "
            "missing feature otherwise, and the next reader deletes it"
        )


class TestBothOriginsAreRecordedAsServed:
    def test_the_ebooks_hostname_is_named_in_the_module(self):
        """⚠️ ebooks.heygabi.ai runs THIS file, under ITS OWN origin.

        `apps/ebooks-door` proxies this repo's /ebooks page verbatim, so the
        hostname the browser (and the auth Worker's CORS allow-list) sees is
        ebooks.heygabi.ai, not audiobooks.heygabi.ai. That is the exact
        hostname behind the owner's complaint, and a reader who does not know
        it will assume this change only reaches one site.
        """
        src = _src()
        assert "ebooks.heygabi.ai" in src


def _function_body(src: str, name: str) -> str:
    """The source of one top-level async function, brace-matched.

    Crude on purpose: these are flat functions with no string braces, and a
    real parser would be a dependency to maintain for one assertion apiece.
    """
    start = src.index(f"function {name}(")
    depth = 0
    i = src.index("{", start)
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start : j + 1]
    raise AssertionError(f"unbalanced braces reading {name}")
