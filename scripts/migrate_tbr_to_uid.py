"""Move the TBR store from display-name keys to ACCOUNT (uid) keys.

Owner's order, 2026-08-18, verbatim:

    "Make tbr keyed to account"

given in answer to the measured finding that `readingLists/{displayNameLower}
_{bookId}` files everyone's to-read list under a string anybody can choose: two
members who pick the same display name share one document per book, see each
other's intentions, and can delete each other's. No Firestore rule could close
it — `firestore.rules` says so in its own header ("no rule can bind a display
name to a person") — so the fix is the key, and changing a persisted key is a
MIGRATION, never an edit.

    old  readingLists/{displayNameLower}_{bookId}
    new  readingLists/{uid}_{bookId}              + a `uid` field

The new id matches `positionDocId` in `site/reading-position.js` exactly. That
is this estate's uid-keyed precedent and there was no reason to invent a second
idiom.

⚠️ THIS SCRIPT REFUSES TO GUESS AN OWNER. A display name that resolves to
exactly ONE account is moved. Anything ambiguous (two accounts, same name) or
unmappable (no account at all) is LEFT EXACTLY WHERE IT IS and printed by name.
Somebody's reading list is not worth a coin flip, and a wrong guess is invisible
afterwards: the document would simply be on the wrong person's list forever.

⚠️ WHAT MAPS A NAME TO AN ACCOUNT — MEASURED, not assumed. The obvious
candidates do not work, and it is worth writing down which so nobody re-tries
them:

    reviews                884 docs, ZERO carry an authorUid   -> useless
    user_content_warnings  0 docs in both lanes                -> useless
    profiles               doc id is the display NAME, not a uid -> useless
    site_roles             doc id IS the uid, and it carries a displayName -> YES
    Firebase Auth          the authoritative account list        -> YES

So the mapping is built from Firebase Auth (every account) reinforced by
site_roles, and both are read through the service account.

Usage (from the repo root):

    python scripts/migrate_tbr_to_uid.py            # report only, writes NOTHING
    python scripts/migrate_tbr_to_uid.py --apply    # perform the move

`--report` is a synonym for the default and is what the REMOVAL CONDITION for
the legacy read-fallback is checked with: when it prints zero uid-less
documents, `legacyReadingListDocId` and its callers can go.

Needs scripts/firebase_service_account.json (gitignored).

⚠️ WINDOWS: keep every print() inside cp1252 — plain ASCII is safest. This
console encodes stdout as cp1252 and a stray character raises UnicodeEncodeError
mid-run, which here would mean dying between a write and its paired delete.
Every name printed below therefore goes through `ascii_safe()`.
"""
import argparse
import json
import sys
from collections import Counter, defaultdict

import google.auth.transport.requests
from google.oauth2 import service_account

PROJECT = 'audiobook-catalog'
BASE = f'https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents'
IDT = f'https://identitytoolkit.googleapis.com/v1/projects/{PROJECT}'
SA = 'scripts/firebase_service_account.json'

# Both lanes. The dev twin measured EMPTY on migration day, but it is enumerated
# anyway: "it was empty last time" is exactly the assumption that makes a
# migration miss half its population the second time it is run.
LANES = ('readingLists', 'readingLists_dev')

UID_LEN = 28  # a Firebase uid. Mirrored in firestore.rules + site/reviews.js.


def ascii_safe(s):
    """Printable on a cp1252 console, whatever the display name contains."""
    return str(s).encode('ascii', 'replace').decode('ascii')


def session():
    creds = service_account.Credentials.from_service_account_file(
        SA,
        scopes=['https://www.googleapis.com/auth/datastore',
                'https://www.googleapis.com/auth/identitytoolkit',
                'https://www.googleapis.com/auth/cloud-platform'])
    return google.auth.transport.requests.AuthorizedSession(creds)


def unwrap(v):
    """One Firestore REST value -> a plain Python one."""
    for k in ('stringValue', 'booleanValue', 'timestampValue'):
        if k in v:
            return v[k]
    if 'integerValue' in v:
        return int(v['integerValue'])
    if 'doubleValue' in v:
        return float(v['doubleValue'])
    if 'nullValue' in v:
        return None
    if 'mapValue' in v:
        return {k: unwrap(x) for k, x in (v['mapValue'].get('fields') or {}).items()}
    if 'arrayValue' in v:
        return [unwrap(x) for x in (v['arrayValue'].get('values') or [])]
    return v


def fetch_all(sess, col):
    """Every document in a collection, as (id, raw_fields) pairs.

    ⚠️ RAW fields are kept, not unwrapped ones. The move rewrites the document
    verbatim, and round-tripping a serverTimestamp or a float through Python
    types is how an `addedAt` silently becomes a different value — the date
    somebody's list is ordered by.
    """
    out, token = [], None
    while True:
        url = f'{BASE}/{col}?pageSize=300' + (f'&pageToken={token}' if token else '')
        r = sess.get(url)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        body = r.json()
        for d in body.get('documents', []):
            out.append((d['name'].rsplit('/', 1)[1], d.get('fields') or {}))
        token = body.get('nextPageToken')
        if not token:
            return out


def name_to_uid(sess):
    """displayName (folded) -> {uid}. Built from the two stores that MEASURED
    as carrying both halves. Returns a set per name so ambiguity is visible
    rather than silently resolved by whichever store answered last."""
    mapping = defaultdict(set)

    r = sess.get(f'{IDT}/accounts:batchGet?maxResults=1000')
    r.raise_for_status()
    for u in r.json().get('users', []):
        nm = (u.get('displayName') or '').strip().lower()
        if nm and u.get('localId'):
            mapping[nm].add(u['localId'])

    for doc_id, fields in fetch_all(sess, 'site_roles'):
        nm = (unwrap(fields.get('displayName', {})) or '').strip().lower()
        if nm:
            mapping[nm].add(doc_id)

    return mapping


def plan_lane(sess, col, mapping):
    """What would happen to every document in one lane. Writes nothing."""
    moves, ambiguous, unmappable, already = [], [], [], []

    for doc_id, fields in fetch_all(sess, col):
        # Already account-keyed? Then a previous run did it, and re-moving it
        # would be a no-op at best and a duplicate at worst.
        if 'uid' in fields:
            already.append(doc_id)
            continue

        name = (unwrap(fields.get('displayName', {})) or '').strip()
        book = unwrap(fields.get('bookId', {})) or ''
        uids = mapping.get(name.lower(), set())

        if not book:
            # No bookId means no new id can be built. Not a person problem.
            unmappable.append((doc_id, name, 'document carries no bookId'))
        elif len(uids) == 1:
            moves.append((doc_id, f'{next(iter(uids))}_{book}', next(iter(uids)), name, fields))
        elif len(uids) > 1:
            ambiguous.append((doc_id, name, f'{len(uids)} accounts share this display name'))
        else:
            unmappable.append((doc_id, name, 'no Firebase account has this display name'))

    return moves, ambiguous, unmappable, already


def apply_move(sess, col, old_id, new_id, uid, fields):
    """Write the account-keyed document, THEN delete the legacy one.

    ⚠️ THE ORDER IS THE SAFETY PROPERTY. Firestore has no cross-document
    transaction over REST here, so the run can be interrupted between the two
    calls. Writing first means the worst interruption leaves a DUPLICATE — both
    ids present, the entry visible, nothing lost — which a re-run cleans up
    because the new id already exists and the old one still resolves. Deleting
    first would make the worst case a LOST reading list.
    """
    new_fields = dict(fields)
    new_fields['uid'] = {'stringValue': uid}

    r = sess.patch(f'{BASE}/{col}/{new_id}', json={'fields': new_fields})
    if r.status_code != 200:
        return False, f'write failed {r.status_code}: {r.text[:160]}'

    r = sess.delete(f'{BASE}/{col}/{old_id}')
    if r.status_code != 200:
        return False, f'WROTE the new doc but DELETE failed {r.status_code} — duplicate left behind'
    return True, None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--apply', action='store_true',
                    help='perform the move. Without it nothing is written.')
    ap.add_argument('--report', action='store_true',
                    help='explicit no-op default; also the removal-condition check')
    args = ap.parse_args()

    sess = session()
    mapping = name_to_uid(sess)

    print(f'name -> account mapping: {len(mapping)} names')
    for nm in sorted(mapping):
        print(f'   {ascii_safe(nm):34} -> {len(mapping[nm])} account(s)')
    print()

    grand = Counter()
    for col in LANES:
        moves, ambiguous, unmappable, already = plan_lane(sess, col, mapping)
        total = len(moves) + len(ambiguous) + len(unmappable) + len(already)
        print('=' * 72)
        print(f'{col}: {total} documents')
        print(f'   already account-keyed : {len(already)}')
        print(f'   TO MOVE               : {len(moves)}')
        print(f'   ambiguous (LEFT)      : {len(ambiguous)}')
        print(f'   unmappable (LEFT)     : {len(unmappable)}')

        # ⚠️ The list of documents whose id will be DELETED is printed BEFORE
        # anything happens, every run, apply or not. A migration whose blast
        # radius is only visible afterwards is not reviewable.
        if moves:
            by_name = Counter(m[3] for m in moves)
            print('\n   legacy ids to be deleted after their replacement is written:')
            for name, n in by_name.most_common():
                print(f'      {ascii_safe(name):34} {n:4d} documents')
            for old_id, new_id, _uid, _n, _f in moves[:5]:
                print(f'      e.g. {ascii_safe(old_id)}')
                print(f'        -> {ascii_safe(new_id)}')
            if len(moves) > 5:
                print(f'      ... and {len(moves) - 5} more')

        for label, rows in (('AMBIGUOUS', ambiguous), ('UNMAPPABLE', unmappable)):
            if rows:
                print(f'\n   {label} - left in place, by name:')
                seen = Counter((name, why) for _id, name, why in rows)
                for (name, why), n in seen.most_common():
                    print(f'      {ascii_safe(name or "(no displayName)"):28} '
                          f'{n:4d} docs — {why}')

        grand['total'] += total
        grand['moved_planned'] += len(moves)
        grand['ambiguous'] += len(ambiguous)
        grand['unmappable'] += len(unmappable)
        grand['already'] += len(already)

        if args.apply and moves:
            print(f'\n   applying {len(moves)} moves...')
            ok = 0
            for old_id, new_id, uid, name, fields in moves:
                done, err = apply_move(sess, col, old_id, new_id, uid, fields)
                if done:
                    ok += 1
                else:
                    print(f'      FAILED {ascii_safe(old_id)}: {ascii_safe(err)}')
            print(f'   moved {ok}/{len(moves)}')
            grand['moved_actual'] += ok
        print()

    print('=' * 72)
    print('SUMMARY')
    for k in ('total', 'already', 'moved_planned', 'moved_actual',
              'ambiguous', 'unmappable'):
        print(f'   {k:16} {grand[k]}')
    if not args.apply:
        print('\n   REPORT ONLY — nothing was written. Re-run with --apply.')

    # The removal condition for the legacy read-fallback, stated as a number.
    left = grand['ambiguous'] + grand['unmappable']
    print(f'\n   uid-less documents remaining: {left}')
    print('   The legacy read-fallback (legacyReadingListDocId and the '
          'uid-less branch of\n   ownsReadingListDoc) may be deleted when '
          'that number is 0.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
