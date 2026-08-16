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
    apply_aliases,
    classify,
    load_exceptions,
    normalize_email,
)


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


if __name__ == "__main__":
    unittest.main()
