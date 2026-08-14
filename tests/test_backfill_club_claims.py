"""
Unit tests for scripts/backfill_club_claims.py — the club claims backfill
dry-run tool. Everything runs against plain dicts / fakes: no Firestore, no
Auth Admin API, no network. Mirrors the FakeSource style of
tests/test_club_announcements.py.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from backfill_club_claims import (  # noqa: E402
    AuthUser,
    ClaimsSource,
    apply_commit,
    build_auth_directory,
    format_plan_table,
    is_club_claimed,
    match_member,
    normalize_name,
    plan_club_claims,
)

NATE = AuthUser(uid="uid-nate", email="nate@example.com", display_name="Nate")
NATE_DUP = AuthUser(uid="uid-nate-2", email="nate2@example.com", display_name="Nate")
SKY = AuthUser(uid="uid-sky", email="sky@example.com", display_name="Skylar")
NO_NAME = AuthUser(uid="uid-blank", email="blank@example.com", display_name="")

CLAIMED_AT = 1_760_000_000_000


def member(slug, display_name, role):
    return {"id": slug, "displayName": display_name, "role": role, "status": "active"}


class NormalizeNameTests(unittest.TestCase):
    def test_trims_and_casefolds(self):
        self.assertEqual(normalize_name("  Nate "), "nate")
        self.assertEqual(normalize_name("NATE"), "nate")
        self.assertEqual(normalize_name("nate"), "nate")

    def test_none_and_empty(self):
        self.assertEqual(normalize_name(None), "")
        self.assertEqual(normalize_name(""), "")
        self.assertEqual(normalize_name("   "), "")


class BuildAuthDirectoryTests(unittest.TestCase):
    def test_groups_by_normalized_name(self):
        directory = build_auth_directory([NATE, SKY])
        self.assertEqual(directory["nate"], [NATE])
        self.assertEqual(directory["skylar"], [SKY])

    def test_duplicate_display_names_group_together(self):
        directory = build_auth_directory([NATE, NATE_DUP])
        self.assertEqual(directory["nate"], [NATE, NATE_DUP])

    def test_blank_display_name_is_skipped(self):
        directory = build_auth_directory([NO_NAME])
        self.assertEqual(directory, {})

    def test_empty_input(self):
        self.assertEqual(build_auth_directory([]), {})


class MatchMemberTests(unittest.TestCase):
    def setUp(self):
        self.directory = build_auth_directory([NATE, SKY])

    def test_single_match(self):
        status, candidates = match_member("Nate", self.directory)
        self.assertEqual(status, "matched")
        self.assertEqual(candidates, [NATE])

    def test_match_ignores_case_and_whitespace(self):
        status, candidates = match_member(" nate  ", self.directory)
        self.assertEqual(status, "matched")
        self.assertEqual(candidates, [NATE])

    def test_no_match(self):
        status, candidates = match_member("Somebody Else", self.directory)
        self.assertEqual(status, "no_match")
        self.assertEqual(candidates, [])

    def test_multiple_match(self):
        directory = build_auth_directory([NATE, NATE_DUP])
        status, candidates = match_member("Nate", directory)
        self.assertEqual(status, "multiple_match")
        self.assertEqual(candidates, [NATE, NATE_DUP])


class IsClubClaimedTests(unittest.TestCase):
    def test_no_manager_uids_key(self):
        self.assertFalse(is_club_claimed({"name": "Book Club"}))

    def test_empty_manager_uids(self):
        self.assertFalse(is_club_claimed({"managerUids": {}}))

    def test_non_empty_manager_uids(self):
        self.assertTrue(is_club_claimed({"managerUids": {"uid1": {"role": "host"}}}))

    def test_non_dict_manager_uids_is_not_claimed(self):
        # Defensive: malformed data should never crash the survey.
        self.assertFalse(is_club_claimed({"managerUids": None}))


class PlanClubClaimsTests(unittest.TestCase):
    def setUp(self):
        self.directory = build_auth_directory([NATE, SKY])

    def test_already_claimed_club_is_skipped(self):
        club = {"id": "c1", "name": "Claimed Club", "managerUids": {"uid-x": {"role": "host"}}}
        members = [member("nate", "Nate", "host")]
        plan = plan_club_claims("prod", club, members, self.directory, CLAIMED_AT)
        self.assertTrue(plan.already_claimed)
        self.assertEqual(plan.member_matches, [])
        self.assertEqual(plan.manager_uids, {})

    def test_unclaimed_club_matched_host(self):
        club = {"id": "c2", "name": "Fantasy Club"}
        members = [member("nate", "Nate", "host")]
        plan = plan_club_claims("prod", club, members, self.directory, CLAIMED_AT)
        self.assertFalse(plan.already_claimed)
        self.assertEqual(len(plan.member_matches), 1)
        self.assertEqual(plan.member_matches[0].status, "matched")
        self.assertEqual(
            plan.manager_uids,
            {"uid-nate": {"role": "host", "displayName": "Nate", "claimedAt": CLAIMED_AT}},
        )

    def test_host_plus_moderator_both_matched(self):
        club = {"id": "c3", "name": "Two Manager Club"}
        members = [member("nate", "Nate", "host"), member("skylar", "Skylar", "moderator")]
        plan = plan_club_claims("dev", club, members, self.directory, CLAIMED_AT)
        self.assertEqual(set(plan.manager_uids.keys()), {"uid-nate", "uid-sky"})
        self.assertEqual(plan.manager_uids["uid-sky"]["role"], "moderator")

    def test_no_match_flagged_and_excluded_from_write(self):
        club = {"id": "c4", "name": "Mystery Host Club"}
        members = [member("ghost", "Someone Unknown", "host")]
        plan = plan_club_claims("prod", club, members, self.directory, CLAIMED_AT)
        self.assertEqual(plan.member_matches[0].status, "no_match")
        self.assertEqual(plan.manager_uids, {})

    def test_multiple_match_flagged_and_excluded_from_write(self):
        directory = build_auth_directory([NATE, NATE_DUP])
        club = {"id": "c5", "name": "Ambiguous Host Club"}
        members = [member("nate", "Nate", "host")]
        plan = plan_club_claims("prod", club, members, directory, CLAIMED_AT)
        self.assertEqual(plan.member_matches[0].status, "multiple_match")
        self.assertEqual(len(plan.member_matches[0].candidates), 2)
        self.assertEqual(plan.manager_uids, {})

    def test_member_role_is_never_a_manager_candidate(self):
        club = {"id": "c6", "name": "Plain Member Club"}
        members = [member("nate", "Nate", "member")]
        plan = plan_club_claims("prod", club, members, self.directory, CLAIMED_AT)
        self.assertEqual(plan.member_matches, [])
        self.assertEqual(plan.manager_uids, {})

    def test_club_with_no_host_or_moderator_members(self):
        club = {"id": "c7", "name": "Empty Roster Club"}
        plan = plan_club_claims("prod", club, [], self.directory, CLAIMED_AT)
        self.assertEqual(plan.member_matches, [])
        self.assertEqual(plan.manager_uids, {})

    def test_mixed_matched_and_unmatched_in_same_club(self):
        club = {"id": "c8", "name": "Mixed Club"}
        members = [member("nate", "Nate", "host"), member("ghost", "Nobody", "moderator")]
        plan = plan_club_claims("prod", club, members, self.directory, CLAIMED_AT)
        statuses = {m.display_name: m.status for m in plan.member_matches}
        self.assertEqual(statuses, {"Nate": "matched", "Nobody": "no_match"})
        # Only the clean match makes it into the write map.
        self.assertEqual(list(plan.manager_uids.keys()), ["uid-nate"])


class FormatPlanTableTests(unittest.TestCase):
    def setUp(self):
        self.directory = build_auth_directory([NATE, SKY])

    def test_table_reports_skip_and_matches(self):
        claimed = {"id": "c1", "name": "Claimed Club", "managerUids": {"uid-x": {"role": "host"}}}
        unclaimed = {"id": "c2", "name": "Open Club"}
        plans = [
            plan_club_claims("prod", claimed, [member("nate", "Nate", "host")], self.directory, CLAIMED_AT),
            plan_club_claims("prod", unclaimed, [member("nate", "Nate", "host")], self.directory, CLAIMED_AT),
        ]
        table = format_plan_table(plans)
        self.assertIn("Claimed Club", table)
        self.assertIn("SKIPPED", table)
        self.assertIn("Open Club", table)
        self.assertIn("MATCHED", table)
        self.assertIn("uid-nate", table)

    def test_table_flags_no_match_and_ambiguous(self):
        club_a = {"id": "ca", "name": "No Match Club"}
        club_b = {"id": "cb", "name": "Ambiguous Club"}
        dup_directory = build_auth_directory([NATE, NATE_DUP])
        plans = [
            plan_club_claims("prod", club_a, [member("g", "Ghost", "host")], self.directory, CLAIMED_AT),
            plan_club_claims("prod", club_b, [member("n", "Nate", "host")], dup_directory, CLAIMED_AT),
        ]
        table = format_plan_table(plans)
        self.assertIn("NO MATCH", table)
        self.assertIn("AMBIGUOUS", table)

    def test_empty_plans_does_not_crash(self):
        table = format_plan_table([])
        self.assertIn("0 club(s) surveyed", table)


class FakeDocRef:
    def __init__(self, store, key):
        self._store = store
        self._key = key

    def update(self, data):
        self._store.setdefault(self._key, {}).update(data)


class FakeCollectionRef:
    def __init__(self, store, name):
        self._store = store
        self._name = name

    def document(self, doc_id):
        return FakeDocRef(self._store, (self._name, doc_id))


class FakeDb:
    """Just enough of the firestore.Client surface for apply_commit()."""

    def __init__(self):
        self.writes = {}

    def collection(self, name):
        return FakeCollectionRef(self.writes, name)


class ApplyCommitTests(unittest.TestCase):
    def setUp(self):
        self.directory = build_auth_directory([NATE, SKY])

    def test_commit_writes_only_unclaimed_matched_clubs(self):
        claimed = {"id": "c1", "name": "Claimed", "managerUids": {"uid-x": {"role": "host"}}}
        matched = {"id": "c2", "name": "Matched"}
        no_match = {"id": "c3", "name": "NoMatch"}
        plans = [
            plan_club_claims("prod", claimed, [member("nate", "Nate", "host")], self.directory, CLAIMED_AT),
            plan_club_claims("prod", matched, [member("nate", "Nate", "host")], self.directory, CLAIMED_AT),
            plan_club_claims("prod", no_match, [member("g", "Ghost", "host")], self.directory, CLAIMED_AT),
        ]
        db = FakeDb()
        written = apply_commit(db, plans)

        self.assertEqual(written, 1)
        self.assertNotIn(("clubs", "c1"), db.writes)  # already claimed: untouched
        self.assertNotIn(("clubs", "c3"), db.writes)  # no clean match: untouched
        self.assertEqual(
            db.writes[("clubs", "c2")],
            {"managerUids": {"uid-nate": {"role": "host", "displayName": "Nate", "claimedAt": CLAIMED_AT}}},
        )

    def test_commit_uses_dev_lane_collection(self):
        club = {"id": "c9", "name": "Dev Club"}
        plans = [plan_club_claims("dev", club, [member("nate", "Nate", "host")], self.directory, CLAIMED_AT)]
        db = FakeDb()
        apply_commit(db, plans)
        self.assertIn(("clubs_dev", "c9"), db.writes)


class FakeMemberSnap:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    def to_dict(self):
        return dict(self._data)


class FakeMembersRef:
    def __init__(self, docs):
        self._docs = docs

    def stream(self):
        return list(self._docs)


class FakeClubDocRef:
    def __init__(self, members_docs):
        self._members_docs = members_docs

    def collection(self, name):
        assert name == "members"
        return FakeMembersRef(self._members_docs)


class FakeClubsCollection:
    def __init__(self, club_snaps, members_by_id):
        self._club_snaps = club_snaps
        self._members_by_id = members_by_id

    def stream(self):
        return list(self._club_snaps)

    def document(self, doc_id):
        return FakeClubDocRef(self._members_by_id.get(doc_id, []))


class FakeSourceDb:
    def __init__(self, collection_name, club_snaps, members_by_id):
        self._collection_name = collection_name
        self._collection = FakeClubsCollection(club_snaps, members_by_id)

    def collection(self, name):
        assert name == self._collection_name
        return self._collection


class ClaimsSourceTests(unittest.TestCase):
    def test_clubs_and_members_round_trip(self):
        club_snap = FakeMemberSnap("c1", {"name": "Fellowship"})
        members_docs = [FakeMemberSnap("nate", {"displayName": "Nate", "role": "host"})]
        db = FakeSourceDb("clubs", [club_snap], {"c1": members_docs})
        source = ClaimsSource(db, "clubs")

        clubs = source.clubs()
        self.assertEqual(clubs, [{"name": "Fellowship", "id": "c1"}])

        members = source.members("c1")
        self.assertEqual(members, [{"displayName": "Nate", "role": "host", "id": "nate"}])


if __name__ == "__main__":
    unittest.main()
