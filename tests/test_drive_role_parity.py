"""
Unit tests for scripts/drive_role_parity.py — the pure parts only.

Per repo policy this test module does NOT touch live Drive, D1, or
Firestore; it exercises normalize_email, load_exceptions (fail-loud on a
missing file, correct parsing of a present one), apply_aliases (the
co-email folding that must never leave a person double-reported), and
classify() against small synthetic inputs covering each problem class.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.drive_role_parity import (
    MASS_DRIFT_CAP,
    OWNER_PROTECTED_EMAILS,
    apply_aliases,
    apply_to_drive,
    classify,
    fuse_check,
    load_exceptions,
    normalize_email,
    plan_drive_changes,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _write_exceptions(dir_path: Path, **overrides) -> Path:
    data = {
        "measured_at": "2026-08-16",
        "permanent_exceptions": [],
        "pending_outreach": [],
        "aliases": {"pairs": []},
        "estate_members_without_drive": {"emails": []},
    }
    data.update(overrides)
    p = dir_path / "drive-exceptions.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


class NormalizeEmailTestCase(unittest.TestCase):
    def test_lowercases_and_strips(self):
        self.assertEqual(normalize_email("  Mitchlandtv@Gmail.com "), "mitchlandtv@gmail.com")

    def test_empty_stays_empty(self):
        self.assertEqual(normalize_email(""), "")
        self.assertEqual(normalize_email(None), "")


class LoadExceptionsTestCase(unittest.TestCase):
    def test_missing_file_fails_loudly(self):
        """A missing exception list must exit, never silently default to
        'no exceptions' — that would treat every drive-only person as fair
        game for revocation."""
        with TemporaryDirectory() as td:
            missing = Path(td) / "does-not-exist.json"
            with self.assertRaises(SystemExit) as ctx:
                load_exceptions(missing)
            self.assertIn("FATAL", str(ctx.exception))

    def test_parses_present_file(self):
        with TemporaryDirectory() as td:
            path = _write_exceptions(
                Path(td),
                pending_outreach=[{"email": "Someone@Gmail.com", "implies_role": "reader"}],
                permanent_exceptions=[{"email": "keeper@gmail.com", "reason": "trusted"}],
                aliases={"pairs": [{"site_account": "a@gmail.com", "drive_account": "b@gmail.com", "note": "same person"}]},
            )
            exc = load_exceptions(path)
            self.assertIn("someone@gmail.com", exc["pending_outreach"])
            self.assertIn("keeper@gmail.com", exc["permanent_exceptions"])
            self.assertEqual(exc["alias_pairs"][0]["site_account"], "a@gmail.com")
            self.assertEqual(exc["alias_pairs"][0]["drive_account"], "b@gmail.com")

    def test_invalid_json_fails_loudly(self):
        with TemporaryDirectory() as td:
            path = Path(td) / "drive-exceptions.json"
            path.write_text("{not valid json", encoding="utf-8")
            with self.assertRaises(SystemExit):
                load_exceptions(path)


class ApplyAliasesTestCase(unittest.TestCase):
    def test_folds_drive_side_into_site_side(self):
        """A Drive permission held under the alias address must attribute to
        the canonical (site_account) identity, not create a second person."""
        drive_perms = {"sylvenixreferral@gmail.com": {"role": "reader", "id": "1", "displayName": ""}}
        estate_rows = {"sparkling.ember.bengal@gmail.com": {"status": "approved", "is_approver": False, "is_devops": False}}
        site_roles = {}
        pairs = [
            {
                "site_account": "sparkling.ember.bengal@gmail.com",
                "drive_account": "sylvenixreferral@gmail.com",
                "note": "same person",
            }
        ]
        new_drive, new_estate, new_roles, alias_display = apply_aliases(
            drive_perms, estate_rows, site_roles, pairs
        )
        self.assertIn("sparkling.ember.bengal@gmail.com", new_drive)
        self.assertNotIn("sylvenixreferral@gmail.com", new_drive)
        self.assertEqual(
            alias_display["sparkling.ember.bengal@gmail.com"]["drive_account"],
            "sylvenixreferral@gmail.com",
        )

    def test_no_pairs_is_a_no_op(self):
        drive_perms = {"a@gmail.com": {"role": "reader"}}
        new_drive, new_estate, new_roles, alias_display = apply_aliases(
            drive_perms, None, {}, []
        )
        self.assertEqual(new_drive, drive_perms)
        self.assertIsNone(new_estate)
        self.assertEqual(alias_display, {})

    def test_handles_none_estate_rows(self):
        """D1-unreadable degrade path: estate_rows is None and must stay None."""
        _, new_estate, _, _ = apply_aliases({}, None, {}, [])
        self.assertIsNone(new_estate)


class ClassifyTestCase(unittest.TestCase):
    def setUp(self):
        with TemporaryDirectory() as td:
            pass  # exceptions built inline per test below

    def _exceptions(self, **overrides):
        base = {
            "measured_at": "2026-08-16",
            "pending_outreach": {},
            "permanent_exceptions": {},
            "estate_members_without_drive": set(),
            "alias_pairs": [],
        }
        base.update(overrides)
        return base

    def test_drive_only_untriaged(self):
        drive_perms = {"stranger@gmail.com": {"role": "reader"}}
        buckets = classify(
            drive_owner_email="owner@gmail.com",
            drive_perms=drive_perms,
            estate_rows={},
            site_roles={},
            exceptions=self._exceptions(),
            alias_display={},
        )
        self.assertEqual(len(buckets["drive_only_untriaged"]), 1)
        self.assertEqual(buckets["drive_only_untriaged"][0]["email"], "stranger@gmail.com")

    def test_pending_outreach_is_excepted_not_drive_only(self):
        drive_perms = {"waiting@gmail.com": {"role": "reader"}}
        exc = self._exceptions(
            pending_outreach={"waiting@gmail.com": {"implies_role": "reader"}}
        )
        buckets = classify(
            drive_owner_email="owner@gmail.com",
            drive_perms=drive_perms,
            estate_rows={},
            site_roles={},
            exceptions=exc,
            alias_display={},
        )
        self.assertEqual(len(buckets["drive_only_untriaged"]), 0)
        self.assertEqual(len(buckets["excepted_pending_outreach"]), 1)

    def test_role_only(self):
        estate_rows = {"member@gmail.com": {"status": "approved", "is_approver": False, "is_devops": False}}
        buckets = classify(
            drive_owner_email="owner@gmail.com",
            drive_perms={},
            estate_rows=estate_rows,
            site_roles={},
            exceptions=self._exceptions(),
            alias_display={},
        )
        self.assertEqual(len(buckets["role_only"]), 1)

    def test_admin_role_matching_writer_is_ok(self):
        drive_perms = {"admin@gmail.com": {"role": "writer"}}
        estate_rows = {"admin@gmail.com": {"status": "approved", "is_approver": True, "is_devops": False}}
        site_roles = {"admin@gmail.com": {"role": "admin", "uid": "x", "displayName": "A"}}
        buckets = classify(
            drive_owner_email="owner@gmail.com",
            drive_perms=drive_perms,
            estate_rows=estate_rows,
            site_roles=site_roles,
            exceptions=self._exceptions(),
            alias_display={},
        )
        self.assertEqual(len(buckets["ok"]), 1)
        self.assertEqual(len(buckets["mismatch"]), 0)

    def test_admin_role_with_reader_drive_is_mismatch(self):
        drive_perms = {"admin@gmail.com": {"role": "reader"}}
        estate_rows = {"admin@gmail.com": {"status": "approved", "is_approver": True, "is_devops": False}}
        site_roles = {"admin@gmail.com": {"role": "admin", "uid": "x", "displayName": "A"}}
        buckets = classify(
            drive_owner_email="owner@gmail.com",
            drive_perms=drive_perms,
            estate_rows=estate_rows,
            site_roles=site_roles,
            exceptions=self._exceptions(),
            alias_display={},
        )
        self.assertEqual(len(buckets["mismatch"]), 1)

    def test_revoked_with_drive_access_is_mismatch(self):
        drive_perms = {"gone@gmail.com": {"role": "reader"}}
        estate_rows = {"gone@gmail.com": {"status": "revoked", "is_approver": False, "is_devops": False}}
        buckets = classify(
            drive_owner_email="owner@gmail.com",
            drive_perms=drive_perms,
            estate_rows=estate_rows,
            site_roles={},
            exceptions=self._exceptions(),
            alias_display={},
        )
        self.assertEqual(len(buckets["mismatch"]), 1)

    def test_owner_email_is_never_a_finding(self):
        drive_perms = {"owner@gmail.com": {"role": "owner"}}
        buckets = classify(
            drive_owner_email="owner@gmail.com",
            drive_perms=drive_perms,
            estate_rows={"owner@gmail.com": {"status": "approved", "is_approver": True, "is_devops": False}},
            site_roles={},
            exceptions=self._exceptions(),
            alias_display={},
        )
        self.assertEqual(len(buckets["owner_protected"]), 1)
        for bucket_name, rows in buckets.items():
            if bucket_name != "owner_protected":
                self.assertFalse(
                    any("owner@gmail.com" in r["email"] for r in rows),
                    f"owner leaked into {bucket_name}",
                )

    def test_unreadable_estate_directory_degrades_to_mismatch_with_unknown(self):
        drive_perms = {"someone@gmail.com": {"role": "reader"}}
        buckets = classify(
            drive_owner_email="owner@gmail.com",
            drive_perms=drive_perms,
            estate_rows=None,
            site_roles={},
            exceptions=self._exceptions(),
            alias_display={},
        )
        self.assertEqual(len(buckets["mismatch"]), 1)
        self.assertIn("UNKNOWN", buckets["mismatch"][0]["estate"])


# ---------------------------------------------------------------------------
# THE AUTO-APPLY (owner order 2026-08-17, wired at sync_to_drive.py STEP 8).
#
# Everything below tests the half that runs with NOBODY WATCHING. The rails
# were already correct when a human read the report first; what changed is
# that nothing reads it now, so each rail gets a test that would fail if a
# refactor quietly removed it.
# ---------------------------------------------------------------------------


def _exceptions(**overrides):
    base = {
        "measured_at": "2026-08-17",
        "pending_outreach": {},
        "permanent_exceptions": {},
        "estate_members_without_drive": set(),
        "alias_pairs": [],
    }
    base.update(overrides)
    return base


def _revoked_drive_holders(n: int, start: int = 0):
    """n people the estate directory says are revoked but who still hold Drive
    reader — the cleanest 'known role, enforceable' drift there is."""
    drive_perms = {}
    estate_rows = {}
    for i in range(start, start + n):
        email = f"gone{i}@gmail.com"
        drive_perms[email] = {"role": "reader", "id": f"perm{i}", "displayName": ""}
        estate_rows[email] = {"status": "revoked", "is_approver": False, "is_devops": False}
    return drive_perms, estate_rows


def _buckets_for(drive_perms, estate_rows, site_roles=None, exceptions=None):
    return classify(
        drive_owner_email="owner@gmail.com",
        drive_perms=drive_perms,
        estate_rows=estate_rows,
        site_roles=site_roles or {},
        exceptions=exceptions or _exceptions(),
        alias_display={},
    )


class FuseTestCase(unittest.TestCase):
    """MASS_DRIFT_CAP — the blast-radius rail added when this became
    unattended. Real drift is one person at a time; a tick that wants to
    change many is one bad read of Drive/D1/Firestore, not many coincidences."""

    def test_cap_is_small_and_named(self):
        self.assertEqual(MASS_DRIFT_CAP, 3)

    def test_four_changes_apply_nothing(self):
        """⚠️ ALL-OR-NOTHING. Half of a plan that is too big to trust is still
        untrustworthy, and harder to undo because nobody can tell from outside
        which half ran."""
        drive_perms, estate_rows = _revoked_drive_holders(4)
        buckets = _buckets_for(drive_perms, estate_rows)
        planned = plan_drive_changes(buckets, _exceptions())
        self.assertEqual(len(planned), 4)

        allowed, reason = fuse_check(planned)
        self.assertFalse(allowed)
        self.assertIn("smells like a data problem", reason)
        self.assertIn("run manually", reason)

        # And through the real entry point: nothing applied, fuse reported.
        result = apply_to_drive(
            "folder-id", "owner@gmail.com", buckets,
            dry_run=True, exceptions=_exceptions(),
        )
        self.assertTrue(result["fuse_tripped"])
        self.assertEqual(result["applied"], [])
        self.assertEqual(result["planned"], 4)

    def test_two_changes_are_applied(self):
        """Deliberate single-person drift must still flow within its own tick —
        that is the whole point of auto-apply. Two is under the cap."""
        drive_perms, estate_rows = _revoked_drive_holders(2)
        buckets = _buckets_for(drive_perms, estate_rows)
        planned = plan_drive_changes(buckets, _exceptions())
        self.assertEqual(len(planned), 2)
        self.assertTrue(all(p["action"] == "remove" for p in planned))

        allowed, reason = fuse_check(planned)
        self.assertTrue(allowed)
        self.assertIn("within the cap", reason)

        result = apply_to_drive(
            "folder-id", "owner@gmail.com", buckets,
            dry_run=True, exceptions=_exceptions(),
        )
        self.assertFalse(result["fuse_tripped"])
        self.assertEqual(result["planned"], 2)

    def test_exactly_at_the_cap_passes(self):
        """The cap is inclusive — three is fine, four is not. Pinned because an
        off-by-one here is invisible until the day it matters."""
        drive_perms, estate_rows = _revoked_drive_holders(MASS_DRIFT_CAP)
        planned = plan_drive_changes(_buckets_for(drive_perms, estate_rows), _exceptions())
        self.assertTrue(fuse_check(planned)[0])

    def test_override_is_available_but_explicit(self):
        drive_perms, estate_rows = _revoked_drive_holders(6)
        planned = plan_drive_changes(_buckets_for(drive_perms, estate_rows), _exceptions())
        self.assertFalse(fuse_check(planned)[0])
        allowed, reason = fuse_check(planned, override=True)
        self.assertTrue(allowed)
        self.assertIn("DRIVE_PARITY_FUSE_OVERRIDE", reason)


class ApplySetRailsTestCase(unittest.TestCase):
    """WHO can be in the apply set. Each of these protects a real person."""

    def test_owner_accounts_are_never_in_the_apply_set(self):
        owner_alt = "mitchlandtv@gmail.com"
        self.assertIn(owner_alt, OWNER_PROTECTED_EMAILS)
        # Drifted on paper: site role admin (implies writer) but only reader
        # on Drive — a row that WOULD be enforceable for anyone else.
        buckets = _buckets_for(
            drive_perms={owner_alt: {"role": "reader", "id": "p", "displayName": ""}},
            estate_rows={owner_alt: {"status": "approved", "is_approver": True, "is_devops": True}},
            site_roles={owner_alt: {"role": "admin", "uid": "u", "displayName": ""}},
        )
        self.assertEqual(len(buckets["mismatch"]), 1, "the drift is still REPORTED")
        self.assertEqual(plan_drive_changes(buckets, _exceptions()), [],
                         "but the owner is never in the apply set")

    def test_owner_is_filtered_even_if_classify_regresses(self):
        """⚠️ The second, independent check. If a future edit to classify()
        ever stamped drive_fix on an owner row, plan_drive_changes() must
        still refuse it. The rail that matters most is checked twice on
        purpose — this test is what makes the second check real rather than
        decorative."""
        rogue = {
            "raw_email": "nbaslamking@gmail.com",
            "drive_account": "nbaslamking@gmail.com",
            "email": "nbaslamking@gmail.com",
            "drive": "writer",
            "estate": "revoked (estate_user.status)",
            "difference": "synthetic regression",
            "drive_fix": "remove",
        }
        self.assertEqual(plan_drive_changes({"mismatch": [rogue]}, _exceptions()), [])

    def test_exception_list_emails_are_never_in_the_apply_set(self):
        """The migration queue: the owner is contacting these people. A silent
        revocation here is exactly what drive-exceptions.json exists to
        prevent — and it must hold even for a row that reached `mismatch`."""
        waiting = "waiting@gmail.com"
        exc = _exceptions(pending_outreach={waiting: {"implies_role": "reader"}})
        rogue = {
            "raw_email": waiting,
            "drive_account": waiting,
            "email": waiting,
            "drive": "reader",
            "estate": "revoked (estate_user.status)",
            "difference": "synthetic regression",
            "drive_fix": "remove",
        }
        self.assertEqual(plan_drive_changes({"mismatch": [rogue]}, exc), [])

    def test_permanent_exceptions_are_never_in_the_apply_set(self):
        keeper = "keeper@gmail.com"
        exc = _exceptions(permanent_exceptions={keeper: {"reason": "owner says so"}})
        rogue = {
            "raw_email": keeper, "drive_account": keeper, "email": keeper,
            "drive": "writer", "estate": "pending (estate_user.status)",
            "difference": "synthetic", "drive_fix": "remove",
        }
        self.assertEqual(plan_drive_changes({"mismatch": [rogue]}, exc), [])

    def test_unknown_tier_is_never_applied(self):
        """'Approved, no elevated role' has NO stored role (reader/contributor
        aren't built). There is nothing to enforce, so nothing is planned —
        and note the direction: guessing here would GRANT access."""
        buckets = _buckets_for(
            drive_perms={"m@gmail.com": {"role": "reader", "id": "p", "displayName": ""}},
            estate_rows={"m@gmail.com": {"status": "approved", "is_approver": False, "is_devops": False}},
        )
        self.assertEqual(plan_drive_changes(buckets, _exceptions()), [])

    def test_untriaged_drive_only_people_are_never_revoked_automatically(self):
        """Someone with Drive and no estate account is a triage question for a
        human, not an unattended revocation."""
        buckets = _buckets_for(
            drive_perms={"stranger@gmail.com": {"role": "reader", "id": "p", "displayName": ""}},
            estate_rows={},
        )
        self.assertEqual(len(buckets["drive_only_untriaged"]), 1)
        self.assertEqual(plan_drive_changes(buckets, _exceptions()), [])

    def test_role_only_people_are_never_granted_automatically(self):
        """The mirror gap: an estate member with no Drive access. Granting
        INCREASES access to DRM-stripped files and the role that would justify
        it does not exist yet — so the reconciler reports and stops."""
        buckets = _buckets_for(
            drive_perms={},
            estate_rows={"member@gmail.com": {"status": "approved", "is_approver": False, "is_devops": False}},
        )
        self.assertEqual(len(buckets["role_only"]), 1)
        self.assertEqual(plan_drive_changes(buckets, _exceptions()), [])

    def test_unreadable_estate_directory_plans_nothing(self):
        """⚠️ D1 unreadable = one of the two role sources is missing. Every row
        still reports, and NOT ONE is applied: reconciling against a half-read
        world is the mass-drift shape the fuse exists for, caught earlier and
        cheaper."""
        drive_perms = {f"p{i}@gmail.com": {"role": "reader", "id": str(i), "displayName": ""} for i in range(5)}
        buckets = _buckets_for(drive_perms, estate_rows=None)
        self.assertEqual(len(buckets["mismatch"]), 5)
        self.assertEqual(plan_drive_changes(buckets, _exceptions()), [])

    def test_a_completed_revocation_is_ok_not_a_permanent_finding(self):
        """⚠️ REGRESSION, found 2026-08-17 by running the reconciler for real
        right after its first live apply. 'Revoked, and correctly holds no
        Drive access' matched no branch and fell through to the unclassified
        MISMATCH fallback — so fixing the drift created a phantom finding that
        no further reconciling could ever clear, and the /status drift count
        would never return to zero. A row that cannot go green teaches people
        to ignore the colour."""
        buckets = _buckets_for(
            drive_perms={},
            estate_rows={"gone@gmail.com": {"status": "revoked", "is_approver": False, "is_devops": False}},
        )
        self.assertEqual(len(buckets["mismatch"]), 0)
        self.assertEqual(len(buckets["ok"]), 1)
        self.assertEqual(plan_drive_changes(buckets, _exceptions()), [])

    def test_admin_with_reader_drive_is_upgraded_not_removed(self):
        buckets = _buckets_for(
            drive_perms={"mod@gmail.com": {"role": "reader", "id": "p", "displayName": ""}},
            estate_rows={"mod@gmail.com": {"status": "approved", "is_approver": False, "is_devops": False}},
            site_roles={"mod@gmail.com": {"role": "moderator", "uid": "u", "displayName": ""}},
        )
        planned = plan_drive_changes(buckets, _exceptions())
        self.assertEqual([p["action"] for p in planned], ["update_to_writer"])

    def test_alias_rows_carry_the_drive_side_address(self):
        """⚠️ An aliased person is keyed by their SITE account, but Drive knows
        them by the other address. A mutation looked up by the canonical email
        would find no permission — so the row carries drive_account, and the
        applier uses it."""
        site, drive = "sparkling@gmail.com", "sylvenix@gmail.com"
        buckets = classify(
            drive_owner_email="owner@gmail.com",
            drive_perms={site: {"role": "reader", "id": "p", "displayName": ""}},
            estate_rows={site: {"status": "revoked", "is_approver": False, "is_devops": False}},
            site_roles={},
            exceptions=_exceptions(),
            alias_display={site: {"site_account": site, "drive_account": drive, "note": ""}},
        )
        planned = plan_drive_changes(buckets, _exceptions())
        self.assertEqual(planned[0]["email"], site)
        self.assertEqual(planned[0]["drive_account"], drive)


# ---------------------------------------------------------------------------
# THE DIRECTION LOCK
# ---------------------------------------------------------------------------

# Firestore write verbs, and the tokens that mean "this line is talking to
# Firestore". A line carrying both is a role WRITE. Drive's own
# `service.permissions().update(...)` carries a write verb and no Firestore
# token, which is exactly the distinction this needs to make.
_WRITE_VERBS = (".set(", ".update(", ".delete(", ".add(", ".create(")
_FIRESTORE_TOKENS = ("site_roles", "db.collection", "document(", "firestore.client", "batch(")


def _role_write_offenders(source: str) -> list[str]:
    """Lines that would WRITE a site role. Pure text scan, on purpose: the
    guarantee is about the file, so the file is what gets checked."""
    offenders = []
    for i, line in enumerate(source.splitlines(), 1):
        code = line.split("#", 1)[0]
        if any(v in code for v in _WRITE_VERBS) and any(t in code for t in _FIRESTORE_TOKENS):
            offenders.append(f"{i}: {line.strip()}")
    return offenders


class DirectionLockTestCase(unittest.TestCase):
    """⚠️ Drive → role is REPORT-ONLY, FOREVER. Granting a site role is a human
    act in the admin UI (ROLES.md §2). The auto-apply made the other direction
    unattended; this asserts it did not quietly make BOTH directions so."""

    def test_the_script_never_writes_a_site_role(self):
        source = (REPO_ROOT / "scripts" / "drive_role_parity.py").read_text(encoding="utf-8")
        self.assertEqual(_role_write_offenders(source), [])

    def test_the_guard_has_teeth(self):
        """A check that can never fail is worse than no check — it buys false
        confidence. So mutate the real source by adding a role write and prove
        the scan catches it."""
        source = (REPO_ROOT / "scripts" / "drive_role_parity.py").read_text(encoding="utf-8")
        mutated = source + (
            '\ndef _sneaky(db, uid):\n'
            '    db.collection("site_roles").document(uid).set({"role": "admin"})\n'
        )
        offenders = _role_write_offenders(mutated)
        self.assertTrue(offenders, "a mutation adding a role write MUST fail this guard")
        self.assertIn("site_roles", offenders[0])

    def test_apply_to_roles_still_advertises_itself_as_report_only(self):
        source = (REPO_ROOT / "scripts" / "drive_role_parity.py").read_text(encoding="utf-8")
        self.assertIn("report-only, always", source)
        self.assertIn("granting a site role is a human act", source)


# ---------------------------------------------------------------------------
# THE PIPELINE WIRING (STEP 8)
# ---------------------------------------------------------------------------


class Step8WiringTestCase(unittest.TestCase):
    def setUp(self):
        self.sync = (REPO_ROOT / "scripts" / "sync_to_drive.py").read_text(encoding="utf-8")

    def test_parity_runs_on_both_cycle_paths(self):
        """⚠️ The STEP 7 lesson, and it bites harder here: a step wired only
        beside the commit skips every quiet cycle, and most cycles are quiet.
        Drift arrives when a PERSON changes, which has nothing to do with
        whether a book arrived — so the idle path is just as valid a tick."""
        self.assertIn("def _run_drive_parity", self.sync)
        idle = self.sync.split("Nothing to upload. All books are synced!")[1].split("finish_run")[0]
        self.assertIn("_run_drive_parity()", idle, "an idle cycle must still reconcile")
        busy = self.sync.split("[STEP 6] Auto-commit & push")[1].split("Fulfill any flagged books")[0]
        self.assertIn("_run_drive_parity()", busy, "a busy cycle must reconcile too")

    def test_parity_is_not_wired_into_rebuild_only_or_publish(self):
        """Deliberate divergence from STEP 7, recorded in the rebuild-only
        ledger: --rebuild-only and the manual `publish` step are book-shaped
        operations, and mutating a person's Drive access as a side effect of
        'republish this tag fix' is a surprise with a blast radius."""
        rebuild = self.sync.split("def _run_rebuild_only_body")[1].split("\ndef ")[0]
        self.assertNotIn("_run_drive_parity", rebuild)
        publish = self.sync.split("def _step_publish")[1].split("\ndef ")[0]
        self.assertNotIn("_run_drive_parity", publish)

    def test_the_child_process_is_forced_to_utf8(self):
        """⚠️ The cp1252 trap. The report prints em-dashes and ⚠️; captured
        through a legacy-codepage pipe it dies with UnicodeEncodeError
        mid-report. The script's own reconfigure() fixes a terminal, not a
        captured pipe — so the PARENT sets the child's encoding."""
        body = self.sync.split("def _run_drive_parity")[1].split("\ndef ")[0]
        self.assertIn('env["PYTHONIOENCODING"] = "utf-8"', body)
        self.assertIn('encoding="utf-8"', body)

    def test_only_the_drive_direction_is_requested(self):
        body = self.sync.split("def _run_drive_parity")[1].split("\ndef ")[0]
        self.assertIn('"--apply-to-drive"', body)
        self.assertNotIn("--apply-to-roles", body)

    def test_parity_runs_as_a_subprocess_with_a_timeout(self):
        """Two failure modes, one mechanism: the parity script exits FATAL by
        design (a missing exception list must never degrade), and Drive OAuth
        can decide it wants an interactive BROWSER — run_local_server() blocks
        forever on an unattended machine. A subprocess with a hard timeout
        turns both into a named line instead of a dead 8-hourly run."""
        body = self.sync.split("def _run_drive_parity")[1].split("\ndef ")[0]
        self.assertIn("subprocess.run", body)
        self.assertIn("timeout=DRIVE_PARITY_TIMEOUT_S", body)
        self.assertIn("except subprocess.TimeoutExpired", body)

    def test_a_parity_failure_cannot_fail_the_cycle(self):
        """Independent domain, same contract as STEP 7: a WARN, the previous
        permission state stands (the safe direction), the next cycle retries."""
        body = self.sync.split("def _run_drive_parity")[1].split("\ndef ")[0]
        raises = [ln.strip() for ln in body.splitlines() if ln.split("#", 1)[0].strip().startswith("raise")]
        self.assertEqual(raises, [], "STEP 8 must never propagate an exception into the cycle")
        self.assertIn("[WARN]", body)
        self.assertIn("except Exception", body)

    def test_a_missing_drive_token_is_a_named_skip(self):
        body = self.sync.split("def _run_drive_parity")[1].split("\ndef ")[0]
        self.assertIn("DRIVE_PARITY_TOKEN.exists()", body)
        self.assertIn('_parity_report("skipped"', body)

    def test_status_carries_counts_not_names(self):
        """⚠️ pipeline_status/current is world-readable and the /status page
        renders it. The emails belong in the local pipeline log, which is the
        audit trail for an unattended change; the dashboard gets counts."""
        body = self.sync.split("def _report_parity_summary")[1].split("\ndef ")[0]
        self.assertIn("_parity_report(", body)
        applied_report = [ln for ln in body.splitlines() if "_parity_report(\"applied\"" in ln]
        self.assertTrue(applied_report)
        self.assertNotIn("', '.join(applied)", applied_report[0])


if __name__ == "__main__":
    unittest.main()
