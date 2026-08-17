"""Smoke the LIVE firestore.rules club-manager gates via the REST API.

Rules are the audiobook site's REAL gate, and they are PROJECT-WIDE the
moment they deploy — there is no dev lane for enforcement and no unit test
that can exercise them. This script is the instrument: it drives the live
rules the way a browser would and asserts each answer.

Scratch data only, in the DEV lane (clubs_dev): it creates one club it owns,
exercises every arm of the roster/administer gates against it, deletes it,
and deletes the two synthetic auth users. Verified to leave nothing behind.

Needs scripts/firebase_service_account.json (gitignored). Password and
anonymous sign-up are DISABLED on this project (Google SSO only), so the
only way to hold a live request.auth outside a browser is to sign a custom
token with the service account and exchange it — see signup() below.

Run:  python scripts/smoke_club_manager_rules.py   (from the repo root)
Written 2026-08-17 for the CLUB MANAGER package; re-run it after any change
to the club clauses in firestore.rules.
"""
import json
import re
import sys
import urllib.error
import urllib.request

PROJECT = 'audiobook-catalog'
FS = f'https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents'
IDT = 'https://identitytoolkit.googleapis.com/v1/accounts'
CLUB = 'zz_clubmgr_smoke'
COL = 'clubs_dev'

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

    Password and anonymous sign-up are both DISABLED on this project (Google
    SSO only), so the only way to hold a live `request.auth` outside a browser
    is the admin path: sign a custom token with the service account and
    exchange it for an ID token. The uid is ours to choose; the token is
    otherwise identical to what a signed-in browser carries.
    """
    import time
    import jwt
    sa = json.load(open('scripts/firebase_service_account.json', encoding='utf-8'))
    uid = f'zz-clubmgr-smoke-{tag}'
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


def doc_url(path, mask=None):
    u = f'{FS}/{path}'
    if mask:
        u += '?' + '&'.join(f'updateMask.fieldPaths={m}' for m in mask)
    return u


tok_a, uid_a = signup('a')
tok_b, uid_b = signup('b')
print(f'synthetic users: A={uid_a} B={uid_b}\n')

club_path = f'{COL}/{CLUB}'
# Clean slate.
call('DELETE', doc_url(f'{club_path}/settings/discord'), token=tok_a)
call('DELETE', doc_url(club_path), token=tok_a)

st, _ = call('PATCH', doc_url(club_path), {'fields': {
    'name': {'stringValue': 'Club Manager Smoke'},
    'hostDisplayName': {'stringValue': 'Smoke Host'},
}}, token=tok_a)
check('setup: an unclaimed scratch club is created', st, 200)


def roster_patch(uid, token, role='host'):
    """The exact write claimManagerRole makes: managerUids.<uid> = {...}."""
    return call('PATCH', doc_url(club_path, [f'managerUids.`{uid}`']), {'fields': {
        'managerUids': {'mapValue': {'fields': {uid: {'mapValue': {'fields': {
            'role': {'stringValue': role},
            'displayName': {'stringValue': 'Smoke'},
            'claimedAt': {'integerValue': '1'},
        }}}}}}
    }}, token=token)


def mask_patch(token, value='…abcd'):
    return call('PATCH', doc_url(club_path, ['discordWebhookMask']),
                {'fields': {'discordWebhookMask': {'stringValue': value}}}, token=token)


print('\n-- the ROSTER gate --')
st, _ = roster_patch(uid_a, None)
check('anonymous claim on an unclaimed club is REFUSED (was open before today)', st, 403)

st, _ = roster_patch(uid_b, tok_a)
check("A cannot add B's uid — the claim is self-only", st, 403)

st, _ = roster_patch(uid_a, tok_a)
check('A claims the unclaimed club for THEMSELVES', st, 200)

st, _ = roster_patch(uid_b, tok_b)
check('B cannot claim the now-CLAIMED club', st, 403)

st, _ = roster_patch(uid_b, tok_a)
check('A, now the bound manager, cannot appoint B (peer-escalation)', st, 403)

st, _ = roster_patch(uid_a, tok_a, role='moderator')
check('A cannot even alter their OWN roster entry once claimed', st, 403)

# ⚠️ The one shape that IS admitted, and why it is not a hole: an identical
# re-write. Rules gate on diff().affectedKeys(), so a PATCH that changes no
# value is not a "roster change" at all and never reaches the gate. It writes
# what is already there — the site's own claim button is idempotent for the
# same reason. Pinned here so nobody reads a 200 as a broken gate.
st, _ = roster_patch(uid_a, tok_a)
check('an identical no-op re-write is admitted (it changes nothing)', st, 200)

print('\n-- the ADMINISTERED gate (the club island) --')
st, _ = mask_patch(tok_a)
check('A, a bound manager with NO site role, sets the webhook mask', st, 200)

st, _ = mask_patch(tok_b, '…zzzz')
check('B, signed in but not a manager, is refused the mask', st, 403)

st, _ = mask_patch(None, '…yyyy')
check('anonymous is refused the mask on a claimed club', st, 403)

hook = 'https://discord.com/api/webhooks/123456/abcDEF_ghi-jkl'
st, _ = call('PATCH', doc_url(f'{club_path}/settings/discord'), {'fields': {
    'webhookUrl': {'stringValue': hook},
    'updatedBy': {'stringValue': 'Smoke'},
}}, token=tok_a)
check('A writes the write-only settings/discord subdoc', st, 200)

st, _ = call('PATCH', doc_url(f'{club_path}/settings/discord'), {'fields': {
    'webhookUrl': {'stringValue': hook},
    'updatedBy': {'stringValue': 'Intruder'},
}}, token=tok_b)
check('B is refused the settings subdoc', st, 403)

st, _ = call('GET', doc_url(f'{club_path}/settings/discord'), token=tok_a)
check('nobody reads the webhook back, not even its own manager', st, 403)

print('\n-- structural fields still behave (nothing else moved) --')
st, _ = call('PATCH', doc_url(club_path, ['joinMode']),
             {'fields': {'joinMode': {'stringValue': 'application'}}}, token=tok_a)
check('A changes joinMode on their own club', st, 200)
st, _ = call('PATCH', doc_url(club_path, ['joinMode']),
             {'fields': {'joinMode': {'stringValue': 'open'}}}, token=tok_b)
check('B cannot change joinMode', st, 403)

# ────────────────────────────────────────────────────────────────────────
# The MANAGECLUB SPLIT, 2026-08-17 (owner decision, option B)
#
# Read-lifecycle actions — finishing a read, removing one, revealing its
# ratings — moved from canManageClub to canOperateClub, so THIS club's bound
# managers and any site MODERATOR hold them. The genuinely destructive rows
# (the club delete, the club's structural fields) did not move.
#
# The moderator arm is the half that actually changed, and it cannot be
# exercised with an ID token alone: site_roles is `allow write: if false` for
# every browser, so the role doc is seeded through the service account (which
# bypasses rules, exactly as scripts/seed_site_admin.py does) and deleted
# again below. ⚠️ It is seeded LATE on purpose — a site moderator also passes
# the ROSTER gate, so seeding it earlier would silently weaken every roster
# assertion above.
# ────────────────────────────────────────────────────────────────────────

read_path = f'{club_path}/reads/r1'


def seed_read():
    """A fresh active read (validClubRead: bookTitle + status). Create is an
    open member action, so any token can put it back between arms."""
    call('DELETE', doc_url(read_path), token=tok_a)
    return call('PATCH', doc_url(read_path), {'fields': {
        'bookTitle': {'stringValue': 'Smoke Read'},
        'status': {'stringValue': 'active'},
        'slot': {'integerValue': '1'},
    }}, token=tok_a)


def finish_patch(token):
    """The exact write finishRead makes: status + finishedAt."""
    return call('PATCH', doc_url(read_path, ['status', 'finishedAt']), {'fields': {
        'bookTitle': {'stringValue': 'Smoke Read'},
        'status': {'stringValue': 'finished'},
        'finishedAt': {'timestampValue': '2026-08-17T00:00:00Z'},
    }}, token=token)


def reveal_patch(token):
    """The exact write revealRatings makes: ratingsRevealed + revealedAt."""
    return call('PATCH', doc_url(read_path, ['ratingsRevealed', 'revealedAt']), {'fields': {
        'bookTitle': {'stringValue': 'Smoke Read'},
        'status': {'stringValue': 'active'},
        'ratingsRevealed': {'booleanValue': True},
        'revealedAt': {'timestampValue': '2026-08-17T00:00:00Z'},
    }}, token=token)


def slot_patch(token, slot='2'):
    """STRUCTURAL — the slot ASSIGNMENT, which did NOT move to operateClub."""
    return call('PATCH', doc_url(read_path, ['slot']), {'fields': {
        'bookTitle': {'stringValue': 'Smoke Read'},
        'status': {'stringValue': 'active'},
        'slot': {'integerValue': slot},
    }}, token=token)


print('\n-- the READ LIFECYCLE (the MANAGECLUB SPLIT) --')
st, _ = seed_read()
check('setup: a read is created on the claimed club (open, a member action)', st, 200)

st, _ = finish_patch(tok_b)
check('B, signed in but managing nothing, cannot FINISH the read', st, 403)

st, _ = finish_patch(None)
check('anonymous cannot finish the read', st, 403)

st, _ = finish_patch(tok_a)
check('A, the bound manager, FINISHES their own club’s read', st, 200)

seed_read()
st, _ = reveal_patch(tok_b)
check('B cannot REVEAL the ratings', st, 403)
st, _ = reveal_patch(tok_a)
check('A reveals the ratings on their own club’s read', st, 200)

seed_read()
st, _ = call('DELETE', doc_url(read_path), token=tok_b)
check('B cannot REMOVE the read', st, 403)
st, _ = call('DELETE', doc_url(read_path), token=tok_a)
check('A removes the read', st, 200)

print('\n-- the site MODERATOR arm (what the split actually widened) --')
seed_read()
st, _ = slot_patch(tok_b)
check('pre-check: B holds nothing on this club yet', st, 403)

import firebase_admin                                        # noqa: E402
from firebase_admin import credentials, firestore            # noqa: E402

_app = firebase_admin.initialize_app(
    credentials.Certificate('scripts/firebase_service_account.json'))
_fs = firestore.client()
_fs.collection('site_roles').document(uid_b).set({
    'role': 'moderator', 'seededBy': 'smoke_club_manager_rules.py',
})
print(f'seeded site_roles/{uid_b} = moderator (service account, bypasses rules)')

st, _ = finish_patch(tok_b)
check('a site MODERATOR finishes a read on a club they do NOT manage', st, 200)

seed_read()
st, _ = reveal_patch(tok_b)
check('a site MODERATOR reveals ratings on a club they do NOT manage', st, 200)

seed_read()
st, _ = call('DELETE', doc_url(read_path), token=tok_b)
check('a site MODERATOR removes a read on a club they do NOT manage', st, 200)

print('\n-- ⚠️ the DESTRUCTIVE half did NOT move (option B’s other line) --')
seed_read()
st, _ = slot_patch(tok_b)
check('a site moderator is STILL refused the read’s slot (structural)', st, 403)

st, _ = call('PATCH', doc_url(club_path, ['joinMode']),
             {'fields': {'joinMode': {'stringValue': 'application'}}}, token=tok_b)
check('a site moderator is STILL refused joinMode (structural)', st, 403)

st, _ = call('DELETE', doc_url(club_path), token=tok_b)
check('a site moderator is STILL refused DELETING THE CLUB', st, 403)

st, _ = call('GET', doc_url(club_path))
check('…and the club is still there, unharmed', st, 200)

print('\n-- cleanup --')
_fs.collection('site_roles').document(uid_b).delete()
gone = _fs.collection('site_roles').document(uid_b).get()
check('the seeded moderator role doc is deleted', gone.exists, False)
firebase_admin.delete_app(_app)
call('DELETE', doc_url(read_path), token=tok_a)
st, _ = call('GET', doc_url(read_path))
check('the scratch read is gone', st, 404)
call('DELETE', doc_url(f'{club_path}/settings/discord'), token=tok_a)
st, _ = call('DELETE', doc_url(club_path), token=tok_a)
check('the scratch club is deleted', st, 200)
st, _ = call('GET', doc_url(club_path))
check('and is gone', st, 404)
for tok in (tok_a, tok_b):
    call('POST', f'{IDT}:delete?key={KEY}', {'idToken': tok})
print('synthetic users deleted')

print(f'\n{sum(results)}/{len(results)} assertions passed')
sys.exit(0 if all(results) else 1)
