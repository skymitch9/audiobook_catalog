"""Smoke the LIVE firestore.rules `audio_positions` gates via the REST API.

AUDIO PLAYER PHASE 3, 2026-09-02. Run this AFTER
`firebase deploy --only firestore:rules`, and re-run it after any change to
the audio_positions clauses.

⚠️ WHY A SMOKE AND NOT JUST UNIT TESTS. Rules are enforced by Google, not by
this repo, and they are PROJECT-WIDE the moment they deploy — there is no dev
lane for enforcement. A unit test can prove the text of the file; only this can
prove the behaviour. And the failure it guards against is the quiet kind: a
stamp refused by a rule is invisible to the page (the write is fire-and-forget,
because a bookkeeping write must never interrupt playback), so a mis-deployed
rule presents as "eviction never learned anything" weeks later.

WHAT THIS COLLECTION IS. One document per anchor, `{ anchor, lastPositionAt }`,
epoch MILLISECONDS, the document id IS the anchor. It is the MID-BOOK SHIELD
that `app/tools/fulfill_audio_requests.evict_candidates()` reads as
`last_position_at`, and the reason the idle threshold is 30 days and not 7.
Why it is not `readingPositions`: site/audio-position.js §4.

THE FIVE CLAIMS, in the order they are checked:

  1. ANYONE MAY READ AND **LIST** IT. Load-bearing: the evictor lists it with
     the PUBLIC web API key. A refused list is indistinguishable from "nobody
     has ever listened", which is also the correct day-one answer — so the
     failure would be silent for as long as nobody looked.
  2. A SIGNED-IN BROWSER MAY STAMP. Unlike /audio_streams, whose stamps only a
     Worker can write. Only the listener's browser holds a position.
  3. A STAMP MAY NEVER MOVE BACKWARDS. That is the one that would cause an
     EARLY eviction of a book somebody is halfway through.
  4. A STAMP MAY NOT BE PARKED IN THE FUTURE, and may carry nothing but the
     two fields it claims to.
  5. A SERVICE ACCOUNT BYPASSES ALL OF IT. That is the premise the sibling
     collection /audio_streams rests on (`allow write: if false` is safe
     BECAUSE the Worker is not a browser), and it is asserted here rather than
     assumed. It is also how this script cleans up after itself: `delete` is
     refused to every browser on purpose, so the scratch document can only be
     removed by the account the rules do not apply to.

Scratch data only, in the DEV lane (audio_positions_dev), under an anchor that
cannot collide with a real book: a real anchor is `"b-" + sha256(path)[:12]`,
and this uses all-zeroes.

Needs scripts/firebase_service_account.json (gitignored). Password and
anonymous sign-up are DISABLED on this project (Google SSO only), so the only
way to hold a live `request.auth` outside a browser is to sign a custom token
with the service account and exchange it — the same signup() the reading
-position smoke uses.

Run:  python scripts/smoke_audio_position_rules.py   (from the repo root)

⚠️ WINDOWS: keep every `print()` string inside cp1252 — plain ASCII is safest.
This console encodes stdout as cp1252, and a non-cp1252 character in a PRINT
raises UnicodeEncodeError mid-run, which here means the script dies AFTER
creating scratch data and BEFORE the cleanup block. It cost exactly that once
(2026-08-17). Emoji in comments and docstrings are fine; only printed text is.
"""
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PROJECT = 'audiobook-catalog'
FS = f'https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents'
IDT = 'https://identitytoolkit.googleapis.com/v1/accounts'
OAUTH = 'https://oauth2.googleapis.com/token'
COL = 'audio_positions_dev'

# ⚠️ Hex, 12 characters, so it satisfies the rule's anchor shape — and all
# zeroes, so it cannot be the sha256 prefix of any real library path.
ANCHOR = 'b-000000000000'

src = open('site/fb-env.js', encoding='utf-8').read()
KEY = re.search(r'apiKey:\s*"([^"]+)"', src).group(1)

results = []


def call(method, url, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('content-type', 'application/json')
    if token:
        req.add_header('authorization', 'Bearer ' + token)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b'{}')


def check(label, got, want):
    ok = got == want
    results.append(ok)
    print(f'{"PASS" if ok else "FAIL"}  {label}  (got {got}, want {want})')


def _sa():
    return json.load(open('scripts/firebase_service_account.json', encoding='utf-8'))


def signup(tag):
    """A real ID token for a synthetic uid — see the module docstring."""
    import jwt
    sa = _sa()
    uid = f'zzaudiopos{tag}'
    now = int(time.time())
    custom = jwt.encode(
        {
            'iss': sa['client_email'], 'sub': sa['client_email'],
            'aud': ('https://identitytoolkit.googleapis.com/'
                    'google.identity.identitytoolkit.v1.IdentityToolkit'),
            'iat': now, 'exp': now + 3600, 'uid': uid,
        },
        sa['private_key'], algorithm='RS256')
    st, body = call('POST', f'{IDT}:signInWithCustomToken?key={KEY}',
                    {'token': custom, 'returnSecureToken': True})
    if st != 200:
        print('signInWithCustomToken failed', st, body)
        sys.exit(2)
    return body['idToken'], uid


def service_account_token():
    """An OAuth access token for the service account itself.

    ⚠️ THIS IS THE ACCOUNT THE RULES DO NOT APPLY TO, and that is not a
    loophole — it is the premise /audio_streams is built on: `allow write: if
    false` is safe there precisely BECAUSE the audiobook Worker authenticates
    this way and a browser cannot. Claim 5 asserts it instead of assuming it.
    """
    import jwt
    sa = _sa()
    now = int(time.time())
    assertion = jwt.encode(
        {
            'iss': sa['client_email'], 'scope': 'https://www.googleapis.com/auth/datastore',
            'aud': OAUTH, 'iat': now, 'exp': now + 3600,
        },
        sa['private_key'], algorithm='RS256')
    data = urllib.parse.urlencode({
        'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
        'assertion': assertion,
    }).encode()
    req = urllib.request.Request(OAUTH, data=data, method='POST')
    req.add_header('content-type', 'application/x-www-form-urlencoded')
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())['access_token']


def stamp(at_ms, anchor=ANCHOR, extra=None):
    """The document site/listen.js writes, in REST wire format.

    ⚠️ `integerValue` because `lastPositionAt` is epoch MILLISECONDS, which is
    what `Date.now()` produces. Seconds here would decode to 1970 on the
    Python side, which is older than every cutoff and therefore reads as
    "evict this book".
    """
    fields = {
        'anchor': {'stringValue': anchor},
        'lastPositionAt': {'integerValue': str(int(at_ms))},
    }
    if extra:
        fields.update(extra)
    return {'fields': fields}


now_ms = int(time.time() * 1000)
doc = f'{COL}/{ANCHOR}'

tok, uid = signup('a')
sa_tok = service_account_token()
print(f'synthetic user: {uid}\nscratch anchor: {ANCHOR}\n')

# Clean slate — a crashed earlier run must not turn a create into an update.
# Only the service account can do this; that is claim 5, and it is why the
# cleanup at the bottom can work at all.
call('DELETE', f'{FS}/{doc}', token=sa_tok)

print('-- claim 1: anyone may read and LIST (the evictor uses the public key) --')
q = {'structuredQuery': {'from': [{'collectionId': COL}], 'limit': 1}}
st, _ = call('POST', f'{FS}:runQuery?key={KEY}', q)
check('an anonymous caller can LIST the stamps', st, 200)

print('\n-- claim 2: a signed-in browser may stamp --')
st, _ = call('PATCH', f'{FS}/{doc}', stamp(now_ms), token=tok)
check('a signed-in listener writes a stamp', st, 200)

st, body = call('GET', f'{FS}/{doc}')
check('an anonymous caller reads it back', st, 200)
if st == 200:
    check('...and it is the millisecond value written',
          body['fields']['lastPositionAt']['integerValue'], str(now_ms))

st, _ = call('PATCH', f'{FS}/{COL}/b-111111111111', stamp(now_ms, anchor='b-111111111111'))
check('a signed-OUT caller cannot stamp', st, 403)

print('\n-- claim 3: a stamp may never move BACKWARDS --')
# 🔴 THE ONE THAT PROTECTS A HALF-FINISHED BOOK. A stamp dragged back is what
# makes a book look idle enough to evict.
st, _ = call('PATCH', f'{FS}/{doc}', stamp(now_ms - 60 * 86400 * 1000), token=tok)
check('a backwards stamp is REFUSED', st, 403)

st, _ = call('PATCH', f'{FS}/{doc}', stamp(now_ms + 5000), token=tok)
check('a forwards stamp is accepted', st, 200)

print('\n-- claim 4: the shape --')
st, _ = call('PATCH', f'{FS}/{doc}', stamp(now_ms + 400 * 86400 * 1000), token=tok)
check('a stamp parked a year in the future is REFUSED', st, 403)

st, _ = call('PATCH', f'{FS}/{doc}',
             stamp(now_ms + 6000, extra={'uid': {'stringValue': uid}}), token=tok)
check('an extra field is REFUSED (this doc is one fact, not a claim board)', st, 403)

st, _ = call('PATCH', f'{FS}/{doc}', stamp(now_ms + 6000, anchor='b-999999999999'), token=tok)
check('a body whose anchor is not the document id is REFUSED', st, 403)

bad_type = {'fields': {'anchor': {'stringValue': ANCHOR},
                       'lastPositionAt': {'stringValue': str(now_ms)}}}
st, _ = call('PATCH', f'{FS}/{doc}', bad_type, token=tok)
check('a stamp that is not a number is REFUSED', st, 403)

st, _ = call('PATCH', f'{FS}/{COL}/not-an-anchor',
             stamp(now_ms, anchor='not-an-anchor'), token=tok)
check('a document id that is not an anchor is REFUSED', st, 403)

st, _ = call('DELETE', f'{FS}/{doc}', token=tok)
check('a browser cannot DELETE a stamp (that would remove the shield)', st, 403)

print('\n-- claim 5 + cleanup: the service account is not a browser --')
st, _ = call('DELETE', f'{FS}/{doc}', token=sa_tok)
check('the service account deletes what no browser could', st, 200)
st, _ = call('GET', f'{FS}/{doc}')
check('and the scratch document is gone', st, 404)

call('POST', f'{IDT}:delete?key={KEY}', {'idToken': tok})
print('synthetic user deleted')

print(f'\n{sum(results)}/{len(results)} assertions passed')
sys.exit(0 if all(results) else 1)
