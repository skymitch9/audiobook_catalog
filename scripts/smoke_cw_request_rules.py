"""Smoke the LIVE firestore.rules cw_requests gates via the REST API.

WHY THIS EXISTS, and it is not the usual reason. Every other smoke script in
this folder guards PRIVACY. This one guards MONEY: a cw_requests document is a
work order, and the hourly `cw-fulfill.yml` GitHub Action picks it up and pays
Anthropic to answer it. Until 2026-08-26 the rule was `allow write: if
validCwRequest()` -- shape only, no account -- so anyone on the internet could
enqueue paid work from a public page, one book per click. (Written up as A3 in
catalog-platform/docs/info/llm-billing-control-design.md.)

THE THREE THINGS THIS FILE ASSERTS, and every one is load-bearing:

  1. create/update REFUSE a signed-out caller       (the fix)
  2. create/update ACCEPT a signed-in one           (not over-tightened)
  3. delete and read STAY OPEN to an anonymous caller  (the fulfiller)

Number 3 is the one people will be tempted to "fix" later, so read this before
touching it. app/tools/fetch_content_warnings.py LISTS this collection and
DELETES fulfilled requests over the REST API authenticated by the *public web
API key* -- not a service account -- so it is gated by these rules exactly like
a browser. Closing read or delete would strand every request with no error
anywhere: the button would work, the queue would fill, and nothing would ever
be answered or cleared. An anonymous DELETE succeeding is the fulfiller's
lifeline, not a hole somebody forgot.

Rules are PROJECT-WIDE the moment they deploy and no unit test in any language
can answer "does the live rule refuse an anonymous write". Only this can.

Scratch data only, in the DEV lane (cw_requests_dev), removed at the end along
with the synthetic account. The dev and prod blocks are identical by
construction and the file header says they must stay in step.

Needs scripts/firebase_service_account.json (gitignored). Password and
anonymous sign-up are DISABLED on this project (Google SSO only), so the only
way to hold a live request.auth outside a browser is to sign a custom token
with the service account and exchange it -- same trick as
smoke_reading_list_rules.py.

Run:  python scripts/smoke_cw_request_rules.py   (from the repo root)
Re-run it after any change to the cw_requests clauses in firestore.rules.

WINDOWS: keep every print() string inside cp1252 -- plain ASCII is safest. A
non-cp1252 character in a print raises UnicodeEncodeError mid-run, which here
means dying AFTER creating scratch data and BEFORE the cleanup block (KI-3).
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
COL = 'cw_requests_dev'
BOOK = 'zz-smoke-cw-book'

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


def signup(tag):
    """A real ID token for a synthetic uid.

    Padded to 28 alphanumeric characters to match a real Firebase uid's shape.
    Nothing in these rules reads the uid out of a document id (the id here is
    the BOOK id), so the length is convention rather than load-bearing -- but
    matching the other smoke scripts costs nothing and avoids a surprise if a
    future rule ever does look.
    """
    import jwt
    sa = json.load(open('scripts/firebase_service_account.json', encoding='utf-8'))
    uid = ('zzcw' + tag).ljust(28, '0')[:28]
    assert len(uid) == 28 and uid.isalnum(), uid
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


def cw_fields(title='A Smoke Test', by='Smoke Tester'):
    """The document the 'Request AI warning check' button writes, in REST wire
    format. Shape must satisfy validCwRequest()."""
    return {'fields': {
        'bookTitle': {'stringValue': title},
        'requestedBy': {'stringValue': by},
    }}


tok, uid = signup('A')
print(f'synthetic account: {uid}\n')

docp = f'{COL}/{BOOK}'

# Clean slate -- a crashed earlier run must not turn a refusal into a 200, nor
# an intended create into an update.
call('DELETE', f'{FS}/{docp}', token=tok)

print('-- the fix: an anonymous caller cannot queue paid work --')
# THE ASSERTION THIS SCRIPT EXISTS FOR. A 200 here is not a cosmetic failure:
# it means anyone on the internet can spend the household's LLM budget.
st, _ = call('PATCH', f'{FS}/{docp}', cw_fields(), token=None)
check('a signed-OUT create is REFUSED', st, 403)

st, _ = call('GET', f'{FS}/{docp}')
check('and nothing was written (404 = absent, not refused)', st, 404)

print('\n-- not over-tightened: a household account still works --')
st, _ = call('PATCH', f'{FS}/{docp}', cw_fields(), token=tok)
check('a signed-IN create is allowed', st, 200)

st, _ = call('PATCH', f'{FS}/{docp}', cw_fields(by='Someone Else'), token=tok)
check('a signed-IN update (repeat click) is allowed', st, 200)

print('\n-- the shape is still checked --')
bad = cw_fields()
del bad['fields']['bookTitle']
st, _ = call('PATCH', f'{FS}/{docp}', bad, token=tok)
check('a request with no bookTitle is refused even signed in', st, 403)

st, _ = call('PATCH', f'{FS}/{docp}', cw_fields(title=''), token=tok)
check('an empty bookTitle is refused', st, 403)

print('\n-- LOAD-BEARING: the fulfiller holds no account --')
# app/tools/fetch_content_warnings.py lists and clears with the PUBLIC web API
# key. If either of these two ever goes 403, the queue fills and nothing is
# ever answered -- and nothing anywhere logs an error.
st, _ = call('GET', f'{FS}/{docp}')
check('an anonymous READ still works (the fulfiller lists this)', st, 200)

st, _ = call('DELETE', f'{FS}/{docp}', token=None)
check('an anonymous DELETE still works (the fulfiller clears this)', st, 200)

st, _ = call('GET', f'{FS}/{docp}')
check('and the request is gone', st, 404)

print('\n-- cleanup --')
call('DELETE', f'{FS}/{docp}', token=tok)
call('POST', f'{IDT}:delete?key={KEY}', {'idToken': tok})
print('synthetic account deleted')

print(f'\n{sum(results)}/{len(results)} assertions passed')
sys.exit(0 if all(results) else 1)
