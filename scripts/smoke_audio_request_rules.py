"""Smoke the LIVE firestore.rules audio_requests gates via the REST API.

⚠️ THIS IS THE ONLY INSTRUMENT THAT ANSWERS THE QUESTION. Rules are
PROJECT-WIDE the moment they deploy and there is no dev lane for enforcement,
so a unit test can pin the fulfiller's parsing but nothing in Python can tell
you whether the deployed rules refuse what they are supposed to refuse. This
script drives the live rules the way a browser would and asserts each answer.

Two claims are genuinely new here, and both are structural rather than
cosmetic (audio-player-design.md 12 decision 3):

  1. ⚠️ THE DUPLICATE CLAUSE. One document per BOOK. A second requester
     JOINS the pile with an array-union; they must not be able to remove the
     first requester, and they must not be able to repoint the pile at a
     different book -- the fulfiller uploads whatever bookTitle says, so an
     editable title on a shared document would let one person redirect
     everyone else's request at a different 600 MB file.
  2. ⚠️ TWO OPEN CLAUSES ARE LOAD-BEARING, and this script pins them OPEN on
     purpose rather than by accident. app/tools/fulfill_audio_requests.py
     LISTS the collection and DELETES fulfilled rows using the *public web
     API key*, not a service account -- so it is gated by these rules exactly
     like a browser. Closing either one strands every request with no error
     anywhere, and the book re-uploads (or never uploads) forever.

Scratch data only, in the DEV lane (audio_requests_dev): one document, two
synthetic auth users, everything deleted at the end.

Needs scripts/firebase_service_account.json (gitignored). Password and
anonymous sign-up are DISABLED on this project (Google SSO only), so the only
way to hold a live request.auth outside a browser is to sign a custom token
with the service account and exchange it -- the same signup() the reading-
position and club-manager smokes use, and the reason all three have one.

Run:  python scripts/smoke_audio_request_rules.py   (from the repo root)
Written 2026-08-17 for audio-player phase 0b; re-run it after any change to
the audio_requests clauses in firestore.rules.

⚠️ WINDOWS: keep every `print()` string inside cp1252 -- plain ASCII is
safest. This console encodes stdout as cp1252, and a non-cp1252 character in
a PRINT raises UnicodeEncodeError mid-run, which here means the script dies
AFTER creating scratch data and BEFORE the cleanup block. It cost exactly
that once in a sibling smoke script (2026-08-17). Emoji in comments and
docstrings are fine; only printed text is affected.
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
COL = 'audio_requests_dev'
BOOK_ID = 'zz-smoke-audio-book'

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
    """A real ID token for a synthetic uid -- see the module docstring."""
    import jwt
    sa = json.load(open('scripts/firebase_service_account.json', encoding='utf-8'))
    uid = f'zzaudioreq{tag}'
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


def request_fields(uid, requesters, title='ZZ Smoke Audio Book', book_id=BOOK_ID):
    """The document the site's future request button writes, wire format."""
    return {'fields': {
        'bookId': {'stringValue': book_id},
        'bookTitle': {'stringValue': title},
        'requestedBy': {'stringValue': uid},
        'requesters': {'arrayValue': {'values': [
            {'stringValue': u} for u in requesters]}},
        'status': {'stringValue': 'pending'},
        'createdAt': {'integerValue': '1755400000000'},
        'updatedAt': {'integerValue': '1755400000000'},
    }}


tok_a, uid_a = signup('a')
tok_b, uid_b = signup('b')
print(f'synthetic users: A={uid_a} B={uid_b}\n')

doc = f'{COL}/{BOOK_ID}'

# Clean slate -- a crashed earlier run must not turn a refusal into a 200.
call('DELETE', f'{FS}/{doc}', token=tok_a)

print('-- making a request --')
st, _ = call('PATCH', f'{FS}/{doc}', request_fields(uid_a, [uid_a]), token=tok_a)
check('A requests a book', st, 200)

# ⚠️ Signed out is the interesting refusal: the request records WHO asked, so
# an anonymous caller must not be able to manufacture one in someone's name.
st, _ = call('PATCH', f'{FS}/{doc}', request_fields(uid_a, [uid_a]))
check('an anonymous caller cannot make a request', st, 403)

# ⚠️ THE DEFECT THE FIRST DEPLOY SHIPPED, 2026-08-17. `hasAll([uid])` reads
# like "you must be in the list" and permits "...and so may anyone you name".
# It is worse than it sounds: the stranger poisons the pile, so the REAL
# second requester's legitimate join is then refused because their list does
# not contain the stranger. Found by this script within a minute of the
# deploy; fixed to exact list equality on create.
st, _ = call('PATCH', f'{FS}/{doc}', request_fields(uid_a, [uid_a, 'somebody-else']),
             token=tok_a)
check('A cannot enrol somebody who did not ask', st, 403)

forged = request_fields(uid_a, [uid_a])
forged['fields']['requestedBy'] = {'stringValue': uid_b}
st, _ = call('PATCH', f'{FS}/{doc}', forged, token=tok_a)
check('A cannot open a pile in B\'s name', st, 403)

print('\n-- the duplicate clause --')
# ⚠️ THE WHOLE POINT. Three people wanting one book is ONE document and ONE
# upload, not three -- the book club case the owner named.
st, _ = call('PATCH', f'{FS}/{doc}', request_fields(uid_a, [uid_a, uid_b]), token=tok_b)
check('B JOINS the existing pile rather than opening a second one', st, 200)

st, body = call('GET', f'{FS}/{doc}', token=tok_b)
check('the pile is one document', st, 200)
if st == 200:
    uids = [v['stringValue'] for v in body['fields']['requesters']['arrayValue']['values']]
    check('...holding both requesters', sorted(uids), sorted([uid_a, uid_b]))

# Pressing request again when you are already in the pile must be a no-op,
# not a refusal — the site will show "requested" and someone will press it
# twice anyway.
st, _ = call('PATCH', f'{FS}/{doc}', request_fields(uid_a, [uid_a, uid_b]), token=tok_b)
check('re-pressing request is idempotent, not an error', st, 200)

st, _ = call('PATCH', f'{FS}/{doc}', request_fields(uid_b, [uid_b]), token=tok_b)
check('B cannot drop A from the pile', st, 403)

# ⚠️ The fulfiller uploads whatever bookTitle says. A mutable title on a
# shared document is a redirect at a different 600 MB file.
st, _ = call('PATCH', f'{FS}/{doc}',
             request_fields(uid_b, [uid_a, uid_b], title='Something Else Entirely'),
             token=tok_b)
check('B cannot repoint the pile at a different book', st, 403)

st, _ = call('PATCH', f'{FS}/{doc}',
             request_fields(uid_b, [uid_a, uid_b], book_id='zz-other-book'),
             token=tok_b)
check('B cannot rewrite the bookId either', st, 403)

print('\n-- the shape --')
empty_title = request_fields(uid_a, [uid_a])
empty_title['fields']['bookTitle'] = {'stringValue': ''}
st, _ = call('PATCH', f'{FS}/{doc}', empty_title, token=tok_a)
check('a request naming no book is REFUSED', st, 403)

no_book_id = request_fields(uid_a, [uid_a])
del no_book_id['fields']['bookId']
st, _ = call('PATCH', f'{FS}/{doc}', no_book_id, token=tok_a)
check('a request with no bookId is REFUSED', st, 403)

print('\n-- the two LOAD-BEARING open clauses --')
# ⚠️ These assert OPEN, deliberately. The local fulfiller is not privileged:
# it reads and clears this collection with the public web API key, so if a
# future tightening closes either of these, the queue silently stops working
# and this line is the tripwire that says so.
q = {'structuredQuery': {'from': [{'collectionId': COL}], 'limit': 1}}
st, _ = call('POST', f'{FS}:runQuery', q)
check('the fulfiller can LIST the queue unauthenticated (load-bearing)', st, 200)

st, _ = call('GET', f'{FS}/{doc}')
check('the fulfiller can GET a request unauthenticated (load-bearing)', st, 200)

print('\n-- cleanup --')
st, _ = call('DELETE', f'{FS}/{doc}')
check('the fulfiller can CLEAR a fulfilled request unauthenticated (load-bearing)', st, 200)
st, _ = call('GET', f'{FS}/{doc}', token=tok_a)
check('and it is gone', st, 404)
for tok in (tok_a, tok_b):
    call('POST', f'{IDT}:delete?key={KEY}', {'idToken': tok})
print('synthetic users deleted')

print(f'\n{sum(results)}/{len(results)} assertions passed')
sys.exit(0 if all(results) else 1)
