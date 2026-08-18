"""Reassign one person's reading-list documents to another ACCOUNT.

Written 2026-08-18 for the one case the account migration
(`migrate_tbr_to_uid.py`) deliberately refused to decide by itself: documents
belonging to a retired **v1 passphrase** account, which has no Firebase uid and
therefore no account to key to. That script leaves them and reports them by
name, because guessing an owner for somebody's reading list is invisible once
done and wrong forever.

⚠️ **THIS TOOL EXISTS BECAUSE THE OWNER DECIDED, NOT BECAUSE IT COULD BE
INFERRED.** It takes the source name and the receiving account on the command
line; it derives neither. Its whole job is to carry out a decision that was
made outside it, and to do so without losing or silently altering anything.

    python scripts/reassign_tbr_owner.py --from-name "<name>" --to-email <addr>
    python scripts/reassign_tbr_owner.py --from-name "<name>" --to-email <addr> --apply

Report-only by default. Nothing is written without `--apply`.

## ⚠️ DUPLICATES ARE SKIPPED, AND SKIPPING MEANS **DELETE WITHOUT WRITING**

If the receiving account already has that book on its list, the source document
is deleted and **nothing is written**. That is not a tidiness preference, it is
the only safe branch: the receiving account's own entry may be `tbr` while the
incoming one is `read`, and writing would silently downgrade a book they still
mean to read into one they have finished. Their list is theirs; a reassignment
may add books to it, never rewrite the ones already there.

Matching is on `bookId` across the receiving account's WHOLE list, both
statuses, for that same reason.

## What a carried document keeps, and what it must not

Kept verbatim: every original field, including `addedAt` and `status` — the
history being carried is the point, and a re-stamped date is a fact invented by
a migration.

Replaced: `uid` (the receiving account) and `displayName` (that account's
CURRENT display name, read from Firebase Auth). ⚠️ Carrying the source's
display name across would leave documents that read as one person and are owned
by another — exactly the name/account disagreement the 2026-08-18 migration
existed to remove, reintroduced by the cleanup meant to finish it.

⚠️ **Write, THEN delete** — the same ordering `migrate_tbr_to_uid.py` uses and
for the same reason: there is no cross-document transaction over REST, so an
interruption must be able to leave a duplicate (recoverable, entry visible)
rather than a lost reading list.

Needs scripts/firebase_service_account.json (gitignored).

⚠️ WINDOWS: every printed string goes through `ascii_safe()` — this console is
cp1252 and a stray character raises UnicodeEncodeError mid-run, which here
would mean dying between a write and its paired delete.
"""
import argparse
import sys
from collections import Counter

import google.auth.transport.requests
from google.oauth2 import service_account

PROJECT = 'audiobook-catalog'
BASE = f'https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents'
IDT = f'https://identitytoolkit.googleapis.com/v1/projects/{PROJECT}'
SA = 'scripts/firebase_service_account.json'
COL = 'readingLists'


def ascii_safe(s):
    return str(s).encode('ascii', 'replace').decode('ascii')


def sv(fields, key):
    return (fields.get(key) or {}).get('stringValue')


def fetch_all(sess, col):
    out, tok = [], None
    while True:
        url = f'{BASE}/{col}?pageSize=300' + (f'&pageToken={tok}' if tok else '')
        r = sess.get(url)
        if r.status_code == 404:
            return out
        r.raise_for_status()
        body = r.json()
        for d in body.get('documents', []):
            out.append((d['name'].rsplit('/', 1)[1], d.get('fields') or {}))
        tok = body.get('nextPageToken')
        if not tok:
            return out


def resolve_account(sess, email=None, uid_hint=None):
    """The receiving account, or exit.

    ⚠️ Agrees THREE stores before answering: Firebase Auth by email, Firebase
    Auth by display name, and site_roles. This hands one person's records to
    another, which is access-INCREASING and irreversible-ish, so an ambiguous
    answer stops the run rather than picking.

    ⚠️ `uid_hint` is an ALTERNATIVE ENTRY POINT, NOT A WEAKER ONE. Naming the
    account by uid instead of by address skips nothing: the uid is looked up,
    its own email is read off the account, and that email is then put through
    the identical three-store agreement below. A uid that does not resolve to
    exactly one account with a matching display name and a consistent
    site_roles row is refused exactly as an ambiguous address would be. It
    exists so an operator can name the account by the opaque id this tool
    prints, rather than passing somebody's address on a command line.
    """
    r = sess.get(f'{IDT}/accounts:batchGet?maxResults=1000')
    r.raise_for_status()
    users = r.json().get('users', [])

    if uid_hint:
        found = [u for u in users if u['localId'] == uid_hint]
        if len(found) != 1:
            print(f'REFUSING: {len(found)} accounts carry that uid, need exactly 1')
            sys.exit(2)
        email = found[0].get('email') or ''
        if not email:
            print('REFUSING: that account has no email, so it cannot be cross-checked')
            sys.exit(2)

    by_email = [u for u in users if (u.get('email') or '').lower() == email.lower()]
    if len(by_email) != 1:
        print(f'REFUSING: {len(by_email)} accounts carry that email, need exactly 1')
        sys.exit(2)
    acct = by_email[0]
    uid, name = acct['localId'], (acct.get('displayName') or '').strip()

    if uid_hint and uid != uid_hint:
        print(f'REFUSING: uid {uid_hint} and its own address disagree ({uid})')
        sys.exit(2)

    by_name = [u for u in users
               if (u.get('displayName') or '').strip().lower() == name.lower()]
    if len(by_name) != 1 or by_name[0]['localId'] != uid:
        print(f'REFUSING: display name {ascii_safe(name)!r} does not resolve to '
              f'exactly this one account ({len(by_name)} matches)')
        sys.exit(2)

    roles = [d for d, f in fetch_all(sess, 'site_roles')
             if (sv(f, 'email') or '').lower() == email.lower()]
    if roles and roles != [uid]:
        print(f'REFUSING: site_roles disagrees — {roles} vs {uid}')
        sys.exit(2)

    print(f'receiving account : {uid}')
    print(f'   display name   : {ascii_safe(name)}')
    print(f'   email verified : {acct.get("emailVerified")}')
    print(f'   site_roles row : {"yes" if roles else "no"}')
    return uid, name


def main():  # noqa: C901 — a linear plan/print/apply script; splitting it would
    # scatter the report across functions and make the ORDER of the write-then-
    # delete pair harder to see, which is the one thing here worth reading.
    ap = argparse.ArgumentParser(description='Reassign reading-list documents to an account.')
    ap.add_argument('--from-name', required=True,
                    help='the source displayName, as it appears on the documents')
    ap.add_argument('--to-email',
                    help='the receiving account, by email (resolved, never guessed)')
    ap.add_argument('--to-uid',
                    help='the receiving account by uid; cross-checked identically')
    ap.add_argument('--apply', action='store_true', help='perform it; otherwise report only')
    args = ap.parse_args()
    if bool(args.to_email) == bool(args.to_uid):
        ap.error('name the receiving account exactly one way: --to-email OR --to-uid')

    creds = service_account.Credentials.from_service_account_file(
        SA, scopes=['https://www.googleapis.com/auth/datastore',
                    'https://www.googleapis.com/auth/identitytoolkit',
                    'https://www.googleapis.com/auth/cloud-platform'])
    sess = google.auth.transport.requests.AuthorizedSession(creds)

    to_uid, to_name = resolve_account(sess, email=args.to_email, uid_hint=args.to_uid)
    print()

    docs = fetch_all(sess, COL)
    src_key = args.from_name.strip().lower()

    # ⚠️ The source set is chosen by displayName AND the absence of a uid. A
    # document that already carries an account is somebody's, whatever name is
    # printed on it, and must never be swept up by a name match.
    source = [(i, f) for i, f in docs
              if (sv(f, 'displayName') or '').strip().lower() == src_key
              and not sv(f, 'uid')]

    # The receiving account's existing books, by bookId, BOTH statuses.
    theirs = {sv(f, 'bookId'): (i, sv(f, 'status'))
              for i, f in docs if sv(f, 'uid') == to_uid and sv(f, 'bookId')}

    carry, skip, broken = [], [], []
    for doc_id, fields in source:
        book = sv(fields, 'bookId')
        if not book:
            broken.append((doc_id, 'no bookId'))
        elif book in theirs:
            skip.append((doc_id, book, sv(fields, 'status'), theirs[book][1]))
        else:
            carry.append((doc_id, f'{to_uid}_{book}', fields))

    print(f'source documents ({ascii_safe(args.from_name)}, uid-less) : {len(source)}')
    print(f'receiving account already holds                : {len(theirs)} books')
    print(f'   TO CARRY OVER          : {len(carry)}')
    print(f'   SKIP AS DUPLICATE      : {len(skip)}   (deleted, nothing written)')
    print(f'   unusable               : {len(broken)}')

    if skip:
        print('\n   duplicates — their existing entry is KEPT, source deleted:')
        for _id, book, src_status, their_status in skip:
            print(f'      {ascii_safe(book)[:58]:58} '
                  f'incoming={src_status} theirs={their_status}')
    if carry:
        print('\n   carried over (first 5):')
        for old, new, _f in carry[:5]:
            print(f'      {ascii_safe(old)[:64]}')
            print(f'        -> {ascii_safe(new)[:64]}')
        if len(carry) > 5:
            print(f'      ... and {len(carry) - 5} more')
    for _id, why in broken:
        print(f'      UNUSABLE {ascii_safe(_id)}: {why}')

    if not args.apply:
        print('\nREPORT ONLY - nothing was written. Re-run with --apply.')
        return 0

    print(f'\napplying: {len(carry)} carried, {len(skip)} deleted as duplicate...')
    carried = deleted = 0
    for old_id, new_id, fields in carry:
        new_fields = dict(fields)
        new_fields['uid'] = {'stringValue': to_uid}
        new_fields['displayName'] = {'stringValue': to_name}
        r = sess.patch(f'{BASE}/{COL}/{new_id}', json={'fields': new_fields})
        if r.status_code != 200:
            print(f'   WRITE FAILED {ascii_safe(old_id)}: {r.status_code} {r.text[:120]}')
            continue
        r = sess.delete(f'{BASE}/{COL}/{old_id}')
        if r.status_code != 200:
            print(f'   wrote {ascii_safe(new_id)} but DELETE of the source failed '
                  f'({r.status_code}) - duplicate left behind')
            continue
        carried += 1

    for doc_id, _b, _s, _t in skip:
        if sess.delete(f'{BASE}/{COL}/{doc_id}').status_code == 200:
            deleted += 1
        else:
            print(f'   delete failed for duplicate {ascii_safe(doc_id)}')

    print(f'   carried over        : {carried}/{len(carry)}')
    print(f'   deleted as duplicate: {deleted}/{len(skip)}')

    left = [i for i, f in fetch_all(sess, COL)
            if (sv(f, 'displayName') or '').strip().lower() == src_key and not sv(f, 'uid')]
    print(f'   source documents remaining: {len(left)} (target 0)')
    return 0 if not left else 1


if __name__ == '__main__':
    sys.exit(main())
