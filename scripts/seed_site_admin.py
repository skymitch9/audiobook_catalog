"""Seed (or verify) the site's rules-enforced admin role: site_roles/{uid}.

Browsers can NEVER write site_roles (firestore.rules: allow write: if false)
and can read only their own doc — so granting a role is strictly a
server-side act, done here through the Firebase service account, which
bypasses rules. This is the owner's break-glass for club surgery: the club
clauses in firestore.rules accept a manager-gated write from any uid whose
site_roles doc says role == 'admin'.

The uid is NEVER passed by hand. It is resolved from the email via the
Firebase Auth Admin API at run time, so a typo'd uid cannot be seeded, and
the doc records the email + display name it resolved for the audit trail.

Usage (from the repo root; needs scripts/firebase_service_account.json or
$FIREBASE_SERVICE_ACCOUNT, same plumbing as app/pipeline_status.py):

    python scripts/seed_site_admin.py --dry-run          # resolve + show only
    python scripts/seed_site_admin.py                    # seed the owner
    python scripts/seed_site_admin.py --email who@x.com  # seed someone else
    python scripts/seed_site_admin.py --revoke           # delete the role doc

The collection is deliberately UNSUFFIXED (no *_dev twin, like pipeline_*):
a role belongs to the person, not the data lane, and both lanes' rules read
the same doc.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

OWNER_EMAIL = "nbaslamking@gmail.com"
COLLECTION = "site_roles"

SCRIPT_DIR = Path(__file__).resolve().parent


def _admin_app():
    """Initialise firebase_admin from the service account. Exits with a
    plain message rather than a stack trace when the key is missing —
    the key is the one piece of state this script cannot conjure."""
    key_path = Path(
        os.getenv("FIREBASE_SERVICE_ACCOUNT")
        or (SCRIPT_DIR / "firebase_service_account.json")
    )
    if not key_path.exists():
        sys.exit(f"No service account key at {key_path} "
                 "(set FIREBASE_SERVICE_ACCOUNT or place the JSON there).")
    import firebase_admin
    from firebase_admin import credentials

    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(str(key_path)))
    return firebase_admin


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", default=OWNER_EMAIL,
                        help=f"Google account to grant admin (default: {OWNER_EMAIL})")
    parser.add_argument("--dry-run", action="store_true",
                        help="resolve the uid and show what would be written, write nothing")
    parser.add_argument("--revoke", action="store_true",
                        help="delete the role doc for --email instead of writing it")
    args = parser.parse_args()

    _admin_app()
    from firebase_admin import auth, firestore

    # Resolve uid from the email via Firebase Auth — the verification step.
    try:
        user = auth.get_user_by_email(args.email)
    except auth.UserNotFoundError:
        sys.exit(f"No Firebase Auth user for {args.email} — nothing seeded.")
    print(f"Resolved {args.email} -> uid {user.uid} "
          f"(displayName={user.display_name!r}, providers="
          f"{[p.provider_id for p in user.provider_data]})")

    db = firestore.client()
    ref = db.collection(COLLECTION).document(user.uid)
    existing = ref.get()
    print(f"Existing {COLLECTION}/{user.uid}: "
          f"{existing.to_dict() if existing.exists else '(none)'}")

    if args.dry_run:
        print("Dry run — nothing written.")
        return 0

    if args.revoke:
        ref.delete()
        print(f"Revoked: deleted {COLLECTION}/{user.uid}.")
        return 0

    doc = {
        "role": "admin",
        "email": args.email,
        "displayName": user.display_name or "",
        "grantedAt": firestore.SERVER_TIMESTAMP,
        "grantedBy": "scripts/seed_site_admin.py",
    }
    ref.set(doc)
    written = ref.get().to_dict()
    print(f"Seeded {COLLECTION}/{user.uid}: {written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
