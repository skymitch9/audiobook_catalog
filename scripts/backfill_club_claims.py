"""Backfill managerUids on unclaimed clubs from their host/moderator display
names — dry run by default.

Owner ask: "Can we just set clubs back to their original owner and claim
them on their behalf?" Clubs record hosts/moderators by DISPLAY NAME
(club doc `hostSlug`/`hostDisplayName`, `members/{slug}.role`); the uid-based
claim model added 2026-08-14 (see firestore.rules `clubClaimed()` /
`isClubManager()` and site/clubs.js `claimManagerRole()`) binds a Firebase
Auth uid into the club doc's `managerUids` map instead:

    managerUids: { uid: { role: 'host'|'moderator', displayName, claimedAt } }

A club with a non-empty `managerUids` map is "claimed"; rules then enforce
manager-only writes against that map. Clubs created before 2026-08-14 (and
anything the legacy UI still creates) have NO `managerUids` key at all —
they are open to the trust-on-first-use claim flow. This script maps each
such club's host/moderator display names to a Firebase Auth account (email
+ uid) via the Admin API — same credential plumbing as
scripts/seed_site_admin.py — and prints the EXACT managerUids map it WOULD
write, so the owner can confirm before anything is written for real.

Matching is by normalized display name (trimmed, casefolded) against every
Firebase Auth user's displayName. Three outcomes per host/moderator member:

    matched          — exactly one Auth account shares that display name.
    no match         — no Auth account has that display name (never seeded,
                        different account, or the name changed since).
    multiple matches — two or more Auth accounts share that display name
                        (can't disambiguate from a name alone).

Only `matched` rows go into the managerUids map this script would write.
`no match` / `multiple matches` rows are printed as flags for the owner to
resolve by hand (or a follow-up run once the account situation is clearer)
— never guessed.

Already-claimed clubs (non-empty managerUids) are SKIPPED entirely: this
script never overwrites an existing roster, matched-only or not. Both data
lanes are covered — `clubs` (prod) and `clubs_dev` (dev) — since the claim
model is identical in both (firestore.rules mirrors the same rules block).

Usage (from the repo root; needs scripts/firebase_service_account.json or
$FIREBASE_SERVICE_ACCOUNT, same as scripts/seed_site_admin.py):

    python scripts/backfill_club_claims.py               # dry run, both lanes
    python scripts/backfill_club_claims.py --lane prod    # clubs only
    python scripts/backfill_club_claims.py --lane dev     # clubs_dev only
    python scripts/backfill_club_claims.py --commit       # write matched claims

⚠️ --commit exists for completeness but is NOT to be run casually — always
review the dry-run table with the owner first. It still only ever writes
`managerUids` for clubs that are currently unclaimed and only for members
that matched exactly one Auth account; it never touches an already-claimed
club and never guesses at `no match` / `multiple matches` rows.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

# Only these member roles are eligible for the manager roster — 'member' is
# never a manager (mirrors site/clubs.js: managerUids only ever gets 'host'
# or 'moderator' entries via createClub()/claimManagerRole()).
MANAGER_ROLES = ("host", "moderator")

LANES = {
    "prod": "clubs",
    "dev": "clubs_dev",
}


class AuthUser(NamedTuple):
    uid: str
    email: str
    display_name: str


class MemberMatch(NamedTuple):
    slug: str
    display_name: str
    role: str
    status: str  # 'matched' | 'no_match' | 'multiple_match'
    candidates: List[AuthUser]


class ClubPlan(NamedTuple):
    lane: str
    club_id: str
    club_name: str
    already_claimed: bool
    member_matches: List[MemberMatch]
    manager_uids: Dict[str, Dict[str, Any]]  # what --commit would write


def normalize_name(name: Optional[str]) -> str:
    """Trim + casefold so 'Nate ' / 'nate' / 'NATE' all key together."""
    return (name or "").strip().casefold()


def build_auth_directory(users: List[AuthUser]) -> Dict[str, List[AuthUser]]:
    """Group Firebase Auth users by normalized display name. Users with no
    display name at all can never be matched by name and are skipped."""
    directory: Dict[str, List[AuthUser]] = {}
    for user in users:
        key = normalize_name(user.display_name)
        if not key:
            continue
        directory.setdefault(key, []).append(user)
    return directory


def match_member(display_name: str, directory: Dict[str, List[AuthUser]]) -> tuple[str, List[AuthUser]]:
    """Resolve one member's display name against the Auth directory.
    Returns (status, candidates)."""
    candidates = directory.get(normalize_name(display_name), [])
    if len(candidates) == 0:
        return "no_match", []
    if len(candidates) > 1:
        return "multiple_match", candidates
    return "matched", candidates


def is_club_claimed(club: Dict[str, Any]) -> bool:
    """Mirror of firestore.rules clubClaimed() / site/clubs.js isClubClaimed()."""
    m = club.get("managerUids")
    return isinstance(m, dict) and len(m) > 0


def plan_club_claims(
    lane: str,
    club: Dict[str, Any],
    members: List[Dict[str, Any]],
    directory: Dict[str, List[AuthUser]],
    claimed_at_ms: Optional[int] = None,
) -> ClubPlan:
    """Work out what one club's backfill would do. Never mutates anything —
    pure function over the data already fetched."""
    club_id = club["id"]
    club_name = club.get("name", "?")

    if is_club_claimed(club):
        # Skip entirely: never overwrite an existing roster, even a partial
        # one. No point computing matches for a club we won't touch.
        return ClubPlan(lane, club_id, club_name, True, [], {})

    claimed_at_ms = int(time.time() * 1000) if claimed_at_ms is None else claimed_at_ms

    matches: List[MemberMatch] = []
    manager_uids: Dict[str, Dict[str, Any]] = {}
    for member in members:
        role = member.get("role")
        if role not in MANAGER_ROLES:
            continue
        display_name = member.get("displayName", "")
        status, candidates = match_member(display_name, directory)
        matches.append(MemberMatch(member.get("id", ""), display_name, role, status, candidates))
        if status == "matched":
            uid = candidates[0].uid
            # Same shape site/clubs.js claimManagerRole() writes: a plain
            # epoch-ms claimedAt (not serverTimestamp), one host/mod at a time.
            manager_uids[uid] = {
                "role": role,
                "displayName": display_name,
                "claimedAt": claimed_at_ms,
            }

    return ClubPlan(lane, club_id, club_name, False, matches, manager_uids)


def format_plan_table(plans: List[ClubPlan]) -> str:
    """Render the full backfill plan as a plain-text table for the owner."""
    lines: List[str] = []
    claimed_skipped = [p for p in plans if p.already_claimed]
    actionable = [p for p in plans if not p.already_claimed]

    lines.append(f"=== Club claims backfill — dry run ({len(plans)} club(s) surveyed) ===")
    lines.append("")

    if claimed_skipped:
        lines.append(f"-- Already claimed, SKIPPED ({len(claimed_skipped)}) --")
        for p in claimed_skipped:
            lines.append(f"  [{p.lane}] {p.club_name!r} ({p.club_id}) — has a manager roster already")
        lines.append("")

    lines.append(f"-- Unclaimed clubs surveyed ({len(actionable)}) --")
    for p in actionable:
        lines.append(f"  [{p.lane}] {p.club_name!r} ({p.club_id})")
        if not p.member_matches:
            lines.append("      (no host/moderator members found)")
            continue
        for m in p.member_matches:
            if m.status == "matched":
                cand = m.candidates[0]
                lines.append(
                    f"      MATCHED    {m.role:<9} {m.display_name!r} -> uid={cand.uid} ({cand.email})"
                )
            elif m.status == "no_match":
                lines.append(f"      NO MATCH   {m.role:<9} {m.display_name!r} -> no Auth account with this name")
            else:
                emails = ", ".join(c.email for c in m.candidates)
                lines.append(
                    f"      AMBIGUOUS  {m.role:<9} {m.display_name!r} -> {len(m.candidates)} accounts match: {emails}"
                )
        if p.manager_uids:
            lines.append(f"      WOULD WRITE managerUids ({len(p.manager_uids)}):")
            for uid, entry in p.manager_uids.items():
                lines.append(f"        {uid}: {entry}")
        else:
            lines.append("      WOULD WRITE managerUids: (none — no clean matches)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Firestore / Auth plumbing
# ---------------------------------------------------------------------------

class ClaimsSource:
    """Thin data-access wrapper so the planning logic never touches the SDK
    directly (mirrors app/club_announcements.py FirestoreClubs)."""

    def __init__(self, db, collection_name: str):
        self._db = db
        self.collection_name = collection_name

    def clubs(self) -> List[Dict[str, Any]]:
        out = []
        for snap in self._db.collection(self.collection_name).stream():
            data = snap.to_dict() or {}
            data["id"] = snap.id
            out.append(data)
        return out

    def members(self, club_id: str) -> List[Dict[str, Any]]:
        out = []
        ref = self._db.collection(self.collection_name).document(club_id).collection("members")
        for snap in ref.stream():
            data = snap.to_dict() or {}
            data["id"] = snap.id
            out.append(data)
        return out


def _firestore_client():
    """Same credential plumbing as scripts/seed_site_admin.py /
    app/club_announcements.py. Returns (db, why_not)."""
    key_path = Path(os.getenv("FIREBASE_SERVICE_ACCOUNT") or (SCRIPT_DIR / "firebase_service_account.json"))
    if not key_path.exists():
        return None, f"no service account at {key_path}"
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore

        if not firebase_admin._apps:
            firebase_admin.initialize_app(credentials.Certificate(str(key_path)))
        return firestore.client(), None
    except ImportError:
        return None, "firebase-admin not installed"
    except Exception as e:  # bad key, no network, project mismatch …
        return None, f"{type(e).__name__}: {e}"


def fetch_auth_directory() -> Dict[str, List[AuthUser]]:
    """Page through every Firebase Auth user via the Admin API and group by
    normalized display name. Requires firebase_admin to already be
    initialised (see _firestore_client)."""
    from firebase_admin import auth

    users: List[AuthUser] = []
    for user in auth.list_users().iterate_all():
        users.append(AuthUser(uid=user.uid, email=user.email or "", display_name=user.display_name or ""))
    return build_auth_directory(users)


def apply_commit(db, plans: List[ClubPlan]) -> int:
    """Write managerUids for unclaimed clubs with at least one clean match.
    Never touches an already-claimed club. Returns the number of clubs
    written. NOT exercised against real Firestore by this task — --commit
    is the owner's call, not this script's."""
    written = 0
    for p in plans:
        if p.already_claimed or not p.manager_uids:
            continue
        collection_name = LANES[p.lane]
        db.collection(collection_name).document(p.club_id).update({"managerUids": p.manager_uids})
        written += 1
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lane", choices=["both", "prod", "dev"], default="both",
                        help="data lane to survey: clubs (prod), clubs_dev (dev), or both (default)")
    parser.add_argument("--commit", action="store_true",
                        help="write matched claims for real (default is dry-run/print-only)")
    args = parser.parse_args()

    db, why_not = _firestore_client()
    if db is None:
        sys.exit(f"Cannot reach Firestore: {why_not}")

    print("Resolving Firebase Auth directory (Admin API list_users)...")
    directory = fetch_auth_directory()
    total_named = sum(len(v) for v in directory.values())
    print(f"Directory built: {total_named} Auth account(s) with a display name "
          f"({len(directory)} distinct name(s)).\n")

    lanes = [args.lane] if args.lane != "both" else ["prod", "dev"]
    plans: List[ClubPlan] = []
    for lane in lanes:
        collection_name = LANES[lane]
        source = ClaimsSource(db, collection_name)
        for club in source.clubs():
            members = source.members(club["id"])
            plans.append(plan_club_claims(lane, club, members, directory))

    print(format_plan_table(plans))

    if args.commit:
        print("\n--commit passed: writing matched claims now...")
        written = apply_commit(db, plans)
        print(f"Wrote managerUids for {written} club(s).")
    else:
        print("\nDry run — nothing written. Re-run with --commit to write the matched claims above.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
