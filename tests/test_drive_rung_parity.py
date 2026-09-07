"""
Unit tests for scripts/drive_rung_parity.py — Phase 4d, the Drive <-> role
reconciler for the NEW RUNGS (`member` and `contributor`).

⚠️ THE THING UNDER TEST IS REPORT-ONLY AND MUST STAY THAT WAY. The sibling
script (drive_role_parity.py) auto-applies role->Drive on every pipeline
cycle; this one applies nothing, ever, in either direction. There is a source
guard below with a teeth test, in the same idiom as that script's own
direction lock.

Per repo policy these tests touch NO live Drive, D1 or Firestore. Everything
exercised here is pure: the ladder constants, the two mapping tables, and
reconcile(), against small synthetic inputs.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from scripts.drive_rung_parity import (
    DRIVE_LEVEL_MIN_RUNG,
    ROLE_LADDER,
    RUNG_MIN_DRIVE_LEVEL,
    reconcile,
    rung_from_drive_level,
    rung_from_site_role,
    rung_rank,
    summarize,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
# .../vs-code-repos/bookbuddy/audiobook_catalog -> .../vs-code-repos/catalog-platform
LADDER_TS = (
    REPO_ROOT.parent.parent
    / "catalog-platform"
    / "apps"
    / "auth-worker"
    / "src"
    / "role-ladder.ts"
)


def _exceptions(**overrides):
    base = {
        "measured_at": "2026-09-07",
        "pending_outreach": {},
        "permanent_exceptions": {},
        "estate_members_without_drive": set(),
        "alias_pairs": [],
    }
    base.update(overrides)
    return base


def _drive(level: str, i: int = 0):
    return {"role": level, "id": f"perm{i}", "displayName": ""}


def _estate(status: str = "approved"):
    return {"status": status, "is_approver": False, "is_devops": False}


_UNSET = object()  # so a caller can pass estate_rows=None to mean "D1 unreadable"


def _reconcile(drive_perms=None, estate_rows=_UNSET, site_roles=None, exceptions=None,
               drive_owner_email="owner@gmail.com", alias_display=None):
    return reconcile(
        drive_owner_email=drive_owner_email,
        drive_perms=drive_perms or {},
        estate_rows={} if estate_rows is _UNSET else estate_rows,
        site_roles=site_roles or {},
        exceptions=exceptions or _exceptions(),
        alias_display=alias_display or {},
    )


def _bucket_emails(result, rung, bucket):
    return [r["raw_email"] for r in result["by_rung"][rung][bucket]]


# ---------------------------------------------------------------------------
# The ladder itself — the reason 4d exists is that two rungs were added and
# the old reconciler never learned them.
# ---------------------------------------------------------------------------


class LadderTestCase(unittest.TestCase):
    def test_the_ladder_is_the_six_rungs_in_order(self):
        self.assertEqual(
            ROLE_LADDER,
            ("guest", "member", "contributor", "moderator", "admin", "owner"),
        )

    def test_it_matches_the_typescript_source_of_truth(self):
        """⚠️ The ladder is DEFINED in catalog-platform's role-ladder.ts and
        mirrored here. A mirror nobody checks is how two systems drift into
        disagreeing about who may do what, so the check is mechanical."""
        if not LADDER_TS.exists():
            self.skipTest(f"sibling repo not present at {LADDER_TS}")
        src = LADDER_TS.read_text(encoding="utf-8")
        m = re.search(r"export const ROLE_LADDER = \[(.*?)\] as const;", src, re.S)
        self.assertIsNotNone(m, "ROLE_LADDER not found in role-ladder.ts")
        names = tuple(re.findall(r"'([a-z]+)'", m.group(1)))
        self.assertEqual(names, ROLE_LADDER)

    def test_rank_is_cumulative_and_ordered(self):
        ranks = [rung_rank(r) for r in ROLE_LADDER]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(len(set(ranks)), len(ranks))

    def test_rung_rank_refuses_a_role_it_does_not_know(self):
        with self.assertRaises(ValueError):
            rung_rank("superuser")


class MappingTestCase(unittest.TestCase):
    """ROLES.md §2's table, both directions."""

    def test_drive_level_to_minimum_rung(self):
        self.assertEqual(
            DRIVE_LEVEL_MIN_RUNG,
            {"none": "guest", "reader": "member", "writer": "contributor", "owner": "owner"},
        )

    def test_rung_to_minimum_drive_level(self):
        self.assertEqual(
            RUNG_MIN_DRIVE_LEVEL,
            {
                "guest": "none",
                "member": "reader",
                "contributor": "writer",
                "moderator": "writer",
                "admin": "writer",
                "owner": "owner",
            },
        )

    def test_the_two_new_rungs_are_covered(self):
        """⚠️ THE WHOLE POINT OF 4d. drive_role_parity.py knows only
        admin/moderator; `member` and `contributor` are stored roles now
        (role-ladder.ts SITE_ROLES) and had no mapping in the reconciler."""
        for rung in ("member", "contributor"):
            self.assertIn(rung, RUNG_MIN_DRIVE_LEVEL)
            self.assertIn(rung, ROLE_LADDER)
        self.assertEqual(RUNG_MIN_DRIVE_LEVEL["member"], "reader")
        self.assertEqual(RUNG_MIN_DRIVE_LEVEL["contributor"], "writer")

    def test_no_stored_role_is_guest_never_an_invented_rung(self):
        self.assertEqual(rung_from_site_role(None), "guest")
        self.assertEqual(rung_from_site_role(""), "guest")

    def test_an_unrecognised_stored_role_is_not_guessed(self):
        self.assertIsNone(rung_from_site_role("wizard"))

    def test_the_old_vocabulary_is_not_silently_accepted(self):
        """`viewer`/`reader` were the pre-2026-08-16 names for guest/member.
        Accepting them here would hide a store that never migrated."""
        self.assertIsNone(rung_from_site_role("viewer"))
        self.assertIsNone(rung_from_site_role("reader"))

    def test_drive_level_none_when_absent(self):
        self.assertEqual(rung_from_drive_level("none"), "guest")
        self.assertEqual(rung_from_drive_level("reader"), "member")
        self.assertEqual(rung_from_drive_level("writer"), "contributor")
        self.assertEqual(rung_from_drive_level("owner"), "owner")


# ---------------------------------------------------------------------------
# reconcile() — the per-rung matched / drive-only / role-only partition
# ---------------------------------------------------------------------------


class ReconcileTestCase(unittest.TestCase):
    def test_contributor_holding_drive_writer_is_matched_at_contributor(self):
        res = _reconcile(
            drive_perms={"c@x.com": _drive("writer")},
            estate_rows={"c@x.com": _estate()},
            site_roles={"c@x.com": {"role": "contributor", "uid": "u1", "displayName": ""}},
        )
        self.assertEqual(_bucket_emails(res, "contributor", "matched"), ["c@x.com"])

    def test_contributor_holding_only_drive_reader_is_role_only_at_contributor(self):
        """The ladder says contributor (Drive writer); Drive grants reader.
        Before 4d this row read as 'approved, no elevated site role on file'
        and was reported OK."""
        res = _reconcile(
            drive_perms={"c@x.com": _drive("reader")},
            estate_rows={"c@x.com": _estate()},
            site_roles={"c@x.com": {"role": "contributor", "uid": "u1", "displayName": ""}},
        )
        self.assertEqual(_bucket_emails(res, "contributor", "role_only"), ["c@x.com"])

    def test_member_holding_drive_writer_is_drive_only_at_contributor(self):
        """Drive grants more than the rung — the direction that MATTERS,
        because it is access nobody granted on the ladder."""
        res = _reconcile(
            drive_perms={"m@x.com": _drive("writer")},
            estate_rows={"m@x.com": _estate()},
            site_roles={"m@x.com": {"role": "member", "uid": "u2", "displayName": ""}},
        )
        self.assertEqual(_bucket_emails(res, "contributor", "drive_only"), ["m@x.com"])

    def test_member_with_no_drive_access_is_role_only_at_member(self):
        res = _reconcile(
            drive_perms={},
            estate_rows={"m@x.com": _estate()},
            site_roles={"m@x.com": {"role": "member", "uid": "u2", "displayName": ""}},
        )
        self.assertEqual(_bucket_emails(res, "member", "role_only"), ["m@x.com"])

    def test_member_with_drive_reader_is_matched_at_member(self):
        res = _reconcile(
            drive_perms={"m@x.com": _drive("reader")},
            estate_rows={"m@x.com": _estate()},
            site_roles={"m@x.com": {"role": "member", "uid": "u2", "displayName": ""}},
        )
        self.assertEqual(_bucket_emails(res, "member", "matched"), ["m@x.com"])

    def test_no_role_doc_with_drive_reader_is_drive_only_at_member(self):
        res = _reconcile(
            drive_perms={"g@x.com": _drive("reader")},
            estate_rows={"g@x.com": _estate()},
            site_roles={},
        )
        self.assertEqual(_bucket_emails(res, "member", "drive_only"), ["g@x.com"])

    def test_admin_and_moderator_still_imply_drive_writer(self):
        """No regression against the direction drive_role_parity.py already
        enforces — the two reconcilers must not disagree about admins."""
        res = _reconcile(
            drive_perms={"a@x.com": _drive("writer"), "mo@x.com": _drive("reader")},
            estate_rows={"a@x.com": _estate(), "mo@x.com": _estate()},
            site_roles={
                "a@x.com": {"role": "admin", "uid": "u3", "displayName": ""},
                "mo@x.com": {"role": "moderator", "uid": "u4", "displayName": ""},
            },
        )
        self.assertEqual(_bucket_emails(res, "admin", "matched"), ["a@x.com"])
        self.assertEqual(_bucket_emails(res, "moderator", "role_only"), ["mo@x.com"])

    def test_guest_with_no_drive_and_no_role_is_matched_at_guest(self):
        res = _reconcile(
            drive_perms={},
            estate_rows={"nobody@x.com": _estate()},
            site_roles={},
        )
        self.assertEqual(_bucket_emails(res, "guest", "matched"), ["nobody@x.com"])

    def test_every_identity_lands_in_exactly_one_bucket(self):
        """The partition property. A person counted twice inflates drift; a
        person counted zero times hides it, which is worse."""
        drive_perms = {
            "a@x.com": _drive("writer", 1),
            "b@x.com": _drive("reader", 2),
            "c@x.com": _drive("reader", 3),
        }
        estate_rows = {e: _estate() for e in ("a@x.com", "b@x.com", "c@x.com", "d@x.com")}
        site_roles = {
            "a@x.com": {"role": "member", "uid": "u1", "displayName": ""},
            "c@x.com": {"role": "contributor", "uid": "u2", "displayName": ""},
            "d@x.com": {"role": "moderator", "uid": "u3", "displayName": ""},
        }
        res = _reconcile(drive_perms, estate_rows, site_roles)
        seen = []
        for rung in ROLE_LADDER:
            for bucket in ("matched", "drive_only", "role_only"):
                seen += _bucket_emails(res, rung, bucket)
        self.assertEqual(sorted(seen), ["a@x.com", "b@x.com", "c@x.com", "d@x.com"])
        self.assertEqual(len(seen), len(set(seen)))

    def test_summary_counts_agree_with_the_rows(self):
        res = _reconcile(
            drive_perms={"a@x.com": _drive("writer"), "b@x.com": _drive("reader", 2)},
            estate_rows={"a@x.com": _estate(), "b@x.com": _estate()},
            site_roles={"a@x.com": {"role": "member", "uid": "u1", "displayName": ""}},
        )
        summary = summarize(res)
        for rung in ROLE_LADDER:
            for bucket in ("matched", "drive_only", "role_only"):
                self.assertEqual(
                    summary["by_rung"][rung][bucket],
                    len(res["by_rung"][rung][bucket]),
                    f"{rung}/{bucket} count disagrees with its rows",
                )
        self.assertEqual(summary["total_rows"], 2)


class ExceptionRailTestCase(unittest.TestCase):
    """Excepted people are still COUNTED (the partition must hold) but are
    never reported as actionable drift — same rails as the sibling script."""

    def test_pending_outreach_is_flagged_not_actionable(self):
        exc = _exceptions(pending_outreach={"p@x.com": {"email": "p@x.com"}})
        res = _reconcile(
            drive_perms={"p@x.com": _drive("writer")},
            estate_rows={},
            site_roles={},
            exceptions=exc,
        )
        rows = res["by_rung"]["contributor"]["drive_only"]
        self.assertEqual([r["raw_email"] for r in rows], ["p@x.com"])
        self.assertEqual(rows[0]["excepted"], "pending_outreach")
        self.assertEqual(summarize(res)["actionable"], 0)

    def test_permanent_exception_is_flagged(self):
        exc = _exceptions(permanent_exceptions={"k@x.com": {"email": "k@x.com"}})
        res = _reconcile(
            drive_perms={"k@x.com": _drive("reader")},
            estate_rows={},
            site_roles={},
            exceptions=exc,
        )
        rows = res["by_rung"]["member"]["drive_only"]
        self.assertEqual(rows[0]["excepted"], "permanent")

    def test_the_owners_own_accounts_are_flagged(self):
        res = _reconcile(
            drive_perms={"mitchlandtv@gmail.com": _drive("writer")},
            estate_rows={},
            site_roles={},
        )
        rows = res["by_rung"]["contributor"]["drive_only"]
        self.assertEqual(rows[0]["excepted"], "owner_protected")

    def test_the_drive_folder_owner_is_flagged(self):
        res = _reconcile(
            drive_perms={"owner@gmail.com": _drive("owner")},
            estate_rows={},
            site_roles={},
        )
        rows = res["by_rung"]["owner"]["drive_only"]
        self.assertEqual(rows[0]["excepted"], "drive_folder_owner")

    def test_actionable_counts_only_unexcepted_drift(self):
        exc = _exceptions(pending_outreach={"p@x.com": {"email": "p@x.com"}})
        res = _reconcile(
            drive_perms={"p@x.com": _drive("writer", 1), "q@x.com": _drive("writer", 2)},
            estate_rows={"q@x.com": _estate()},
            site_roles={"q@x.com": {"role": "member", "uid": "u1", "displayName": ""}},
            exceptions=exc,
        )
        self.assertEqual(summarize(res)["actionable"], 1)


class HonestyTestCase(unittest.TestCase):
    """Things it must report rather than invent."""

    def test_an_unrecognised_stored_role_is_named_not_bucketed_as_guest(self):
        res = _reconcile(
            drive_perms={"w@x.com": _drive("reader")},
            estate_rows={"w@x.com": _estate()},
            site_roles={"w@x.com": {"role": "wizard", "uid": "u9", "displayName": ""}},
        )
        self.assertEqual(
            [r["raw_email"] for r in res["unrecognised_site_roles"]], ["w@x.com"]
        )
        seen = []
        for rung in ROLE_LADDER:
            for bucket in ("matched", "drive_only", "role_only"):
                seen += _bucket_emails(res, rung, bucket)
        self.assertNotIn("w@x.com", seen)

    def test_estate_unreadable_is_reported_not_invented(self):
        res = _reconcile(
            drive_perms={"a@x.com": _drive("reader")},
            estate_rows=None,
            site_roles={},
        )
        self.assertTrue(res["estate_unreadable"])
        row = res["by_rung"]["member"]["drive_only"][0]
        self.assertIn("UNKNOWN", row["estate"])

    def test_a_non_approved_estate_status_is_carried_on_the_row(self):
        res = _reconcile(
            drive_perms={"r@x.com": _drive("reader")},
            estate_rows={"r@x.com": _estate("revoked")},
            site_roles={"r@x.com": {"role": "member", "uid": "u1", "displayName": ""}},
        )
        row = res["by_rung"]["member"]["matched"][0]
        self.assertIn("revoked", row["estate"])
        self.assertEqual(
            [r["raw_email"] for r in res["role_rungs_without_approved_estate"]],
            ["r@x.com"],
        )


# ---------------------------------------------------------------------------
# REPORT-ONLY — the source guard, with teeth
# ---------------------------------------------------------------------------

_WRITE_VERBS = (".set(", ".update(", ".delete(", ".add(", ".create(", ".insert(")
_STORE_TOKENS = (
    "site_roles",
    "db.collection",
    "document(",
    "firestore.client",
    "batch(",
    "permissions()",
    "service.permissions",
)


def _write_offenders(source: str) -> list[str]:
    offenders = []
    for i, line in enumerate(source.splitlines(), 1):
        code = line.split("#", 1)[0]
        if any(v in code for v in _WRITE_VERBS) and any(t in code for t in _STORE_TOKENS):
            offenders.append(f"{i}: {line.strip()}")
    return offenders


class ReportOnlyTestCase(unittest.TestCase):
    def setUp(self):
        self.path = REPO_ROOT / "scripts" / "drive_rung_parity.py"
        self.source = self.path.read_text(encoding="utf-8")

    def test_it_writes_nothing_to_drive_or_firestore(self):
        self.assertEqual(_write_offenders(self.source), [])

    def test_the_guard_has_teeth(self):
        mutated = self.source + (
            "\ndef _sneaky(service, folder_id, pid):\n"
            "    service.permissions().delete(fileId=folder_id, permissionId=pid)\n"
        )
        offenders = _write_offenders(mutated)
        self.assertTrue(offenders, "a mutation adding a Drive write MUST fail this guard")

    def test_it_offers_no_commit_or_apply_flag(self):
        """There is deliberately no way to turn this into a mutator from the
        command line — a report-only tool with an --apply flag is one flag
        away from being an unattended one."""
        for flag in ("--commit", "--apply-to-drive", "--apply-to-roles", "--fix"):
            self.assertNotIn(flag, self.source, f"{flag} must not exist here")

    def test_it_says_report_only_in_its_own_header(self):
        self.assertIn("REPORT-ONLY", self.source)


class SharedLoadersTestCase(unittest.TestCase):
    """'Put it beside the existing parity script and SHARE its loaders rather
    than copying them' — a second copy of the Drive/D1/Firestore readers is a
    second thing to keep in step with three live systems."""

    def setUp(self):
        self.source = (REPO_ROOT / "scripts" / "drive_rung_parity.py").read_text(
            encoding="utf-8"
        )

    def test_it_imports_the_loaders_from_the_sibling_script(self):
        self.assertIn("from drive_role_parity import", self.source)
        for name in (
            "fetch_drive_permissions",
            "fetch_estate_directory",
            "fetch_site_roles",
            "load_exceptions",
            "apply_aliases",
        ):
            self.assertIn(name, self.source)

    def test_it_does_not_redefine_any_loader(self):
        for name in (
            "fetch_drive_permissions",
            "fetch_estate_directory",
            "fetch_site_roles",
            "load_exceptions",
            "apply_aliases",
        ):
            self.assertNotIn(f"def {name}(", self.source, f"{name} must not be copied")

    def test_it_reuses_the_owner_protected_list(self):
        self.assertIn("OWNER_PROTECTED_EMAILS", self.source)


if __name__ == "__main__":
    unittest.main()
