"""Smoke the LIVE firestore.rules reading-list gates via the REST API.

⚠️ THIS IS THE ONLY INSTRUMENT THAT ANSWERS THE QUESTION, for the same reason
`smoke_reading_position_rules.py` exists: rules are PROJECT-WIDE the moment they
deploy, there is no dev lane for enforcement, and no unit test in any language
can check "another signed-in account cannot overwrite your list". This drives
the live rules the way a browser would and asserts each answer.

⚠️ WHAT MAKES THIS ONE DIFFERENT from the reading-position smoke: /readingLists
runs TWO models at once. The 2026-08-18 account migration ("Make tbr keyed to
account") moved 181 of 234 documents to `{uid}_{bookId}`, and could not move 53
— their owner is a retired v1 passphrase account with no Firebase uid at all.
Those keep the old `{displayNameLower}_{bookId}` id and the old shape-only
rules, or the only person entitled to them loses them.

So this script has to prove BOTH halves, and the second is as important as the
first:

    account-keyed docs  -> owner-only writes and deletes
    legacy-keyed docs   -> still writable, exactly as before

A run that proved only the first would be a green light for a change that
silently stranded somebody's reading list.

Scratch data only, in the DEV lane (readingLists_dev), all removed at the end
along with the synthetic users.

⚠️ THE SYNTHETIC UIDS MUST BE EXACTLY 28 CHARACTERS. The rules tell the two
lanes apart by the SHAPE of the document id (`tbrIdIsUidKeyed`: a 28-character
alphanumeric head is an account). The reading-position smoke uses short custom
uids like `zzreadpos_a`, and one of those here would be read as a LEGACY id and
sail through the shape-only branch — the script would pass while testing
nothing. Cost an hour to notice once; hence this paragraph.

Needs scripts/firebase_service_account.json (gitignored). Password and anonymous
sign-up are DISABLED on this project (Google SSO only), so the only way to hold
a live request.auth outside a browser is to sign a custom token with the service
account and exchange it.

Run:  python scripts/smoke_reading_list_rules.py   (from the repo root)
Re-run it after any change to the readingLists clauses in firestore.rules.

⚠️ WINDOWS: keep every print() string inside cp1252 — plain ASCII is safest.
A non-cp1252 character in a PRINT raises UnicodeEncodeError mid-run, which here
means dying AFTER creating scratch data and BEFORE the cleanup block.
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
COL = 'readingLists_dev'
BOOK = 'zz-smoke-tbr-book'

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

    ⚠️ Padded to EXACTLY 28 alphanumeric characters — see the module docstring.
    A shorter uid makes every account-keyed assertion below test the LEGACY
    branch instead, and the whole script passes vacuously.
    """
    import jwt
    sa = json.load(open('scripts/firebase_service_account.json', encoding='utf-8'))
    uid = ('zztbr' + tag).ljust(28, '0')[:28]
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


def tbr_fields(name='Smoke Tester', uid=None, book=BOOK, status='tbr'):
    """The document the TBR button writes, in REST wire format."""
    f = {
        'displayName': {'stringValue': name},
        'bookId': {'stringValue': book},
        'bookTitle': {'stringValue': 'A Smoke Test'},
        'status': {'stringValue': status},
    }
    if uid:
        f['uid'] = {'stringValue': uid}
    return {'fields': f}


tok_a, uid_a = signup('A')
tok_b, uid_b = signup('B')
print(f'synthetic accounts: A={uid_a} B={uid_b}\n')

doc_a = f'{COL}/{uid_a}_{BOOK}'
doc_b = f'{COL}/{uid_b}_{BOOK}'
# The legacy lane: a lowercased display name, not an account.
legacy = f'{COL}/zzsmokelegacy_{BOOK}'

# Clean slate — a crashed earlier run must not turn a refusal into a 200.
for d, t in ((doc_a, tok_a), (doc_b, tok_b), (legacy, tok_a)):
    call('DELETE', f'{FS}/{d}', token=t)

print('-- your own account-keyed entry --')
st, _ = call('PATCH', f'{FS}/{doc_a}', tbr_fields(uid=uid_a), token=tok_a)
check('A puts a book on their own list', st, 200)

st, _ = call('GET', f'{FS}/{doc_a}', token=tok_a)
check('A reads it back', st, 200)

print('\n-- somebody ELSE\'s account-keyed entry (the whole point) --')
# ⚠️ THE CLAIM THIS SCRIPT EXISTS FOR. Before the migration these were the
# SAME DOCUMENT whenever two people shared a display name.
st, _ = call('PATCH', f'{FS}/{doc_a}', tbr_fields(uid=uid_a), token=tok_b)
check('B cannot OVERWRITE A\'s entry', st, 403)

st, _ = call('DELETE', f'{FS}/{doc_a}', token=tok_b)
check('B cannot DELETE A\'s entry', st, 403)

st, _ = call('PATCH', f'{FS}/{doc_b}', tbr_fields(uid=uid_b), token=tok_a)
check('A cannot write a document id belonging to B', st, 403)

# ⚠️ The id and the uid FIELD must name the SAME account, both the caller's.
# Either half alone is a hole: a forged field mis-attributes every scan that
# trusts it, and a forged id is somebody else's document outright.
st, _ = call('PATCH', f'{FS}/{doc_a}', tbr_fields(uid=uid_b), token=tok_a)
check('A cannot claim to BE B inside their own document', st, 403)

st, _ = call('PATCH', f'{FS}/{doc_a}', tbr_fields(uid=None), token=tok_a)
check('an account-keyed id with NO uid field is refused', st, 403)

print('\n-- signed out --')
st, _ = call('PATCH', f'{FS}/{doc_a}', tbr_fields(uid=uid_a))
check('an anonymous writer cannot WRITE an account entry', st, 403)

st, _ = call('DELETE', f'{FS}/{doc_a}')
check('an anonymous caller cannot DELETE an account entry', st, 403)

# Reads stay world-open, and that is deliberate: three surfaces list this
# collection. Asserted so a future tightening is a deliberate, visible change.
st, _ = call('GET', f'{FS}/{doc_a}')
check('reads stay OPEN (community counts, both catalogs\' filters)', st, 200)

print('\n-- the LEGACY lane must still work (53 real documents) --')
# ⚠️ If these fail, the 53 documents whose owner has no Firebase account are
# stranded and the person who wrote them can never touch them again.
st, _ = call('PATCH', f'{FS}/{legacy}', tbr_fields(name='zzsmokelegacy'))
check('a legacy display-name entry is still writable, signed OUT', st, 200)

st, _ = call('GET', f'{FS}/{legacy}')
check('and readable', st, 200)

st, _ = call('DELETE', f'{FS}/{legacy}')
check('and deletable (shape-only, as before)', st, 200)

# ⚠️ The one thing the legacy lane must NOT allow: smuggling a uid in. That
# would put an account-attributed document under a name-shaped id, which every
# scan reads by uid and no rule protects.
st, _ = call('PATCH', f'{FS}/{legacy}', tbr_fields(name='zzsmokelegacy', uid=uid_a),
             token=tok_a)
check('a legacy id carrying a uid field is REFUSED', st, 403)

print('\n-- the shape --')
bad = tbr_fields(uid=uid_a)
del bad['fields']['status']
st, _ = call('PATCH', f'{FS}/{doc_a}', bad, token=tok_a)
check('an entry with no status is refused', st, 403)

print('\n-- cleanup --')
st, _ = call('DELETE', f'{FS}/{doc_a}', token=tok_a)
check('A takes their own book off the list', st, 200)
st, _ = call('GET', f'{FS}/{doc_a}', token=tok_a)
check('and it is gone', st, 404)
call('DELETE', f'{FS}/{doc_b}', token=tok_b)
call('DELETE', f'{FS}/{legacy}', token=tok_a)
for tok in (tok_a, tok_b):
    call('POST', f'{IDT}:delete?key={KEY}', {'idToken': tok})
print('synthetic accounts deleted')

print(f'\n{sum(results)}/{len(results)} assertions passed')
sys.exit(0 if all(results) else 1)
