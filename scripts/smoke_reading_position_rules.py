"""Smoke the LIVE firestore.rules reading-position gates via the REST API.

⚠️ THIS IS THE ONLY INSTRUMENT THAT ANSWERS THE QUESTION. `readingPositions`
is the FIRST genuinely per-person collection in this project — everything else
is shape-only by a recorded owner decision — so "another signed-in account
cannot read or write your document" is a new claim, and no unit test in any
language can check it. Rules are also PROJECT-WIDE the moment they deploy;
there is no dev lane for enforcement. This script drives the live rules the way
a browser would and asserts each answer.

Scratch data only, in the DEV lane (readingPositions_dev): two synthetic auth
users, one document each, both deleted at the end along with the users.

Needs scripts/firebase_service_account.json (gitignored). Password and
anonymous sign-up are DISABLED on this project (Google SSO only), so the only
way to hold a live request.auth outside a browser is to sign a custom token
with the service account and exchange it — the same signup() the club-manager
smoke uses, and the reason both scripts have one.

Run:  python scripts/smoke_reading_position_rules.py   (from the repo root)
Written 2026-08-17 for viewer phase 3 ("save your spot"); re-run it after any
change to the readingPositions clauses in firestore.rules.

⚠️ WINDOWS: keep every `print()` string inside cp1252 — plain ASCII is safest.
This console encodes stdout as cp1252, and a non-cp1252 character in a PRINT
raises UnicodeEncodeError mid-run, which here means the script dies AFTER
creating scratch data and BEFORE the cleanup block. It cost exactly that once
in the sibling smoke script (2026-08-17). Emoji in comments and docstrings are
fine; only printed text is affected.
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
COL = 'readingPositions_dev'

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
    """A real ID token for a synthetic uid — see the module docstring."""
    import jwt
    sa = json.load(open('scripts/firebase_service_account.json', encoding='utf-8'))
    uid = f'zzreadpos{tag}'          # no underscore: it is the doc-id separator
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


def position_fields(uid, book='zz-smoke-book', page=42, kind='page', drop_kind=False):
    """The document site/reading-position.js writes, in REST wire format."""
    pos = {'value': {'integerValue': str(page)}}
    if not drop_kind:
        pos['kind'] = {'stringValue': kind}
    return {'fields': {
        'uid': {'stringValue': uid},
        'bookId': {'stringValue': book},
        'anchor': {'stringValue': 'b-zzsmoke00000'},
        'format': {'stringValue': 'pdf'},
        'pos': {'mapValue': {'fields': pos}},
        'updatedAt': {'integerValue': '1755400000000'},
        'device': {'stringValue': 'Smoke'},
    }}


tok_a, uid_a = signup('a')
tok_b, uid_b = signup('b')
print(f'synthetic users: A={uid_a} B={uid_b}\n')

doc_a = f'{COL}/{uid_a}_zz-smoke-book'
doc_b = f'{COL}/{uid_b}_zz-smoke-book'

# Clean slate — a crashed earlier run must not turn a refusal into a 200.
call('DELETE', f'{FS}/{doc_a}', token=tok_a)
call('DELETE', f'{FS}/{doc_b}', token=tok_b)

print('-- your own document --')
st, _ = call('PATCH', f'{FS}/{doc_a}', position_fields(uid_a), token=tok_a)
check('A saves their own spot', st, 200)

st, body = call('GET', f'{FS}/{doc_a}', token=tok_a)
check('A reads their own spot back', st, 200)
if st == 200:
    page = body['fields']['pos']['mapValue']['fields']['value']['integerValue']
    check('...and it is the page they saved', page, '42')

print('\n-- somebody else\'s document --')
# ⚠️ THE CLAIM THIS SCRIPT EXISTS FOR. Every other collection in this project
# is world-readable; a reading position must not be.
st, _ = call('GET', f'{FS}/{doc_a}', token=tok_b)
check('B cannot READ A\'s position', st, 403)

st, _ = call('PATCH', f'{FS}/{doc_a}', position_fields(uid_a, page=999), token=tok_b)
check('B cannot OVERWRITE A\'s position', st, 403)

st, _ = call('DELETE', f'{FS}/{doc_a}', token=tok_b)
check('B cannot DELETE A\'s position', st, 403)

# ⚠️ The doc id is the gate, so a forged uid FIELD must not buy a document id
# that is not yours — and neither must the reverse.
st, _ = call('PATCH', f'{FS}/{doc_b}', position_fields(uid_b), token=tok_a)
check('A cannot write a document id belonging to B, even with B\'s uid inside', st, 403)

st, _ = call('PATCH', f'{FS}/{doc_a}', position_fields(uid_b), token=tok_a)
check('A cannot claim to BE B inside their own document', st, 403)

print('\n-- signed out --')
st, _ = call('GET', f'{FS}/{doc_a}')
check('an anonymous reader cannot READ a position', st, 403)

st, _ = call('PATCH', f'{FS}/{doc_a}', position_fields(uid_a, page=1))
check('an anonymous writer cannot WRITE a position', st, 403)

st, _ = call('DELETE', f'{FS}/{doc_a}')
check('an anonymous caller cannot DELETE a position', st, 403)

print('\n-- the shape --')
# ⚠️ pos.kind travels WITH pos.value or the document is refused. A CFI read as
# a page number is a silent jump to the wrong place.
st, _ = call('PATCH', f'{FS}/{doc_a}', position_fields(uid_a, drop_kind=True), token=tok_a)
check('a locator that lost its `kind` is REFUSED', st, 403)

bad_format = position_fields(uid_a)
bad_format['fields']['format'] = {'stringValue': 'mobi'}
st, _ = call('PATCH', f'{FS}/{doc_a}', bad_format, token=tok_a)
check('a format this reader cannot open is REFUSED', st, 403)

print('\n-- the AUDIO locator (audio player phase 3, 2026-09-02) --')
# ⚠️ THE CLAUSE PHASE 3 HANGS OFF. `validReadingPosition()` carries 'audio' in
# BOTH lists — `format` and `pos.kind` — and until the rules are DEPLOYED the
# file saying so proves nothing. A position written against rules that refuse
# it fails silently and looks exactly like "the player does not save your
# spot" (design §1.4, §7.4). That is why this runs against the live project.
doc_audio = f'{COL}/{uid_a}_zz-smoke-audio'
call('DELETE', f'{FS}/{doc_audio}', token=tok_a)

audio = position_fields(uid_a, book='zz-smoke-audio')
audio['fields']['format'] = {'stringValue': 'audio'}
# 🔴 {chapter, offsetSec}, NEVER a single absolute second (design §7.4). The
# `seconds` field rides along for DISPLAY and is never navigated by.
audio['fields']['pos'] = {'mapValue': {'fields': {
    'kind': {'stringValue': 'audio'},
    'value': {'mapValue': {'fields': {
        'chapter': {'integerValue': '7'},
        'offsetSec': {'doubleValue': 812.4},
        'seconds': {'doubleValue': 5312.9},
    }}},
}}}
# Per-book speed moved onto this document in phase 3 (design §9.2 #2), so the
# rule must accept the extra field rather than being a closed shape.
audio['fields']['rate'] = {'doubleValue': 1.5}
st, _ = call('PATCH', f'{FS}/{doc_audio}', audio, token=tok_a)
check('an AUDIO position with a {chapter, offsetSec} locator is accepted', st, 200)

st, body = call('GET', f'{FS}/{doc_audio}', token=tok_a)
check('...and reads back', st, 200)
if st == 200:
    chapter = (body['fields']['pos']['mapValue']['fields']['value']
               ['mapValue']['fields']['chapter']['integerValue'])
    check('...with the chapter it named', chapter, '7')
    check('...and the remembered speed', body['fields']['rate']['doubleValue'], 1.5)

bad_kind = position_fields(uid_a, book='zz-smoke-audio', kind='timestamp')
bad_kind['fields']['format'] = {'stringValue': 'audio'}
st, _ = call('PATCH', f'{FS}/{doc_audio}', bad_kind, token=tok_a)
check('a locator kind nothing can read is REFUSED', st, 403)

st, _ = call('DELETE', f'{FS}/{doc_audio}', token=tok_a)
check('the audio scratch document is removed', st, 200)

print('\n-- listing --')
# ⚠️ `allow list: if false`. What a household reads is not a queryable set,
# and the doc-id wildcard is not reliably bound for a list operation — an
# `allow read` here would have been a hole wearing a get's clothes.
q = {'structuredQuery': {'from': [{'collectionId': COL}], 'limit': 1}}
st, _ = call('POST', f'{FS}:runQuery', q, token=tok_a)
check('A cannot LIST the collection, not even their own rows', st, 403)

print('\n-- cleanup --')
st, _ = call('DELETE', f'{FS}/{doc_a}', token=tok_a)
check('A deletes their own position (forgetting a spot is allowed)', st, 200)
st, _ = call('GET', f'{FS}/{doc_a}', token=tok_a)
check('and it is gone', st, 404)
call('DELETE', f'{FS}/{doc_b}', token=tok_b)
for tok in (tok_a, tok_b):
    call('POST', f'{IDT}:delete?key={KEY}', {'idToken': tok})
print('synthetic users deleted')

print(f'\n{sum(results)}/{len(results)} assertions passed')
sys.exit(0 if all(results) else 1)
