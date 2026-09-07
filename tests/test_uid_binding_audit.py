"""
Unit tests for scripts/uid_binding_audit.py — Phase 5's MEASUREMENT.

⚠️ THE THING UNDER TEST CHANGES NOTHING. Phase 5 proposes binding member
self-writes to a uid; the recommendation was to build the measurement first,
so this script reads `firestore.rules` and Firestore and reports. It deploys
no rules, flips no shadow/enforce flag and writes no document — the source
guard at the bottom has teeth.

Pure halves only here: the rules parser (which write paths exist, and which
of them can currently land without a uid) and the per-document uid probe.
Nothing in this file touches live Firestore.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from scripts.uid_binding_audit import (
    SELF_WRITE_PROBES,
    UID_SHAPE,
    classify_uid,
    parse_rules,
    resolve_condition,
    strip_comments,
    summarize_paths,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RULES = REPO_ROOT / "firestore.rules"


def _path(parsed, path, verb="create"):
    for p in parsed["paths"]:
        if p["path"] == path and verb in p["verbs"]:
            return p
    raise AssertionError(f"no `allow {verb}` found for {path}")


class StripCommentsTestCase(unittest.TestCase):
    def test_it_strips_a_line_comment(self):
        self.assertEqual(strip_comments("a = 1; // nope\nb = 2;"), "a = 1; \nb = 2;")

    def test_it_does_not_eat_a_double_slash_inside_a_string(self):
        """⚠️ validClubSettings() matches 'https://(discord|discordapp)...'.
        A naive `line.split('//')` truncates that rule mid-expression and the
        parser then reads a condition that does not exist."""
        src = "x.matches('https://discord.com/api') // real comment"
        self.assertEqual(strip_comments(src).strip(), "x.matches('https://discord.com/api')")

    def test_it_strips_a_block_comment(self):
        self.assertEqual(" ".join(strip_comments("a /* gone */ b").split()), "a b")


class ParseRulesSyntheticTestCase(unittest.TestCase):
    SRC = """
    service cloud.firestore {
      match /databases/{database}/documents {
        function needsAuth() {
          return request.auth != null;
        }
        function shapeOnly() {
          return request.resource.data.text is string;
        }
        match /open/{id} {
          allow read: if true;
          allow create, update: if shapeOnly();
          allow delete: if false;
        }
        match /bound/{id} {
          allow write: if needsAuth() && shapeOnly();
        }
        match /nested/{a} {
          match /inner/{b} {
            allow create: if shapeOnly();
          }
        }
      }
    }
    """

    def setUp(self):
        self.parsed = parse_rules(self.SRC)

    def test_it_finds_the_functions(self):
        self.assertIn("needsAuth", self.parsed["functions"])
        self.assertIn("shapeOnly", self.parsed["functions"])

    def test_it_finds_each_write_path(self):
        paths = {p["path"] for p in self.parsed["paths"]}
        self.assertIn("/open/{id}", paths)
        self.assertIn("/bound/{id}", paths)
        self.assertIn("/nested/{a}/inner/{b}", paths)

    def test_verbs_are_split(self):
        p = _path(self.parsed, "/open/{id}", "update")
        self.assertEqual(sorted(p["verbs"]), ["create", "update"])

    def test_a_shape_only_path_does_not_require_auth(self):
        self.assertFalse(_path(self.parsed, "/open/{id}")["requires_auth"])

    def test_auth_is_found_THROUGH_a_function_call(self):
        """The whole reason the parser resolves calls: `if needsAuth()` has no
        `request.auth` in it textually, and a scan that stopped at the allow
        line would call the bound path unbound."""
        p = _path(self.parsed, "/bound/{id}", "write")
        self.assertNotIn("request.auth", p["condition"])
        self.assertTrue(p["requires_auth"])

    def test_the_resolution_has_teeth(self):
        """Mutate the validator to add an auth check and the verdict must
        flip — a checker that cannot change its answer is not measuring."""
        mutated = self.SRC.replace(
            "return request.resource.data.text is string;",
            "return request.auth != null && request.resource.data.text is string;",
        )
        self.assertTrue(_path(parse_rules(mutated), "/open/{id}")["requires_auth"])

    def test_allow_false_is_not_counted_as_a_client_write_path(self):
        p = _path(self.parsed, "/open/{id}", "delete")
        self.assertFalse(p["client_writable"])

    def test_summarize_counts_the_unbound_write_paths(self):
        s = summarize_paths(self.parsed["paths"])
        self.assertEqual(s["client_write_paths"], 3)
        self.assertEqual(s["write_paths_without_uid"], 2)


class ResolveConditionTestCase(unittest.TestCase):
    def test_it_does_not_loop_forever_on_recursion(self):
        fns = {"a": "b()", "b": "a()"}
        self.assertIsInstance(resolve_condition("a()", fns), str)

    def test_it_leaves_an_unknown_call_alone(self):
        self.assertIn("mystery()", resolve_condition("mystery()", {}))


# ---------------------------------------------------------------------------
# The REAL firestore.rules — the measurement Phase 5 asked for
# ---------------------------------------------------------------------------


class RealRulesTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parsed = parse_rules(RULES.read_text(encoding="utf-8"))

    def test_both_lanes_are_seen(self):
        paths = {p["path"] for p in self.parsed["paths"]}
        self.assertIn("/reviews/{reviewId}", paths)
        self.assertIn("/reviews_dev/{reviewId}", paths)

    def test_the_string_with_a_double_slash_survived_parsing(self):
        p = _path(self.parsed, "/clubs/{clubId}/settings/{settingId}")
        self.assertIn("discordapp", p["resolved"])

    def test_reviews_can_still_be_written_without_a_uid(self):
        """TODO Phase 5, verbatim: 'reviews, comments, RSVPs and progress are
        not [uid-bound]'. This is the half of that claim rules can prove."""
        self.assertFalse(_path(self.parsed, "/reviews/{reviewId}")["requires_auth"])

    def test_comments_rsvps_and_progress_can_be_written_without_a_uid(self):
        for path, verb in (
            ("/clubs/{clubId}/reads/{readId}/comments/{commentId}", "create"),
            ("/clubs/{clubId}/rsvps/{userId}", "create"),
            ("/clubs/{clubId}/reads/{readId}/progress/{userId}", "write"),
            ("/clubs/{clubId}/reads/{readId}/ratings/{userId}", "create"),
        ):
            with self.subTest(path=path):
                self.assertFalse(_path(self.parsed, path, verb)["requires_auth"])

    def test_the_three_uid_bound_collections_really_do_require_auth(self):
        """The other half of the same TODO sentence: readingPositions,
        readingLists and user_content_warnings ARE bound. ⚠️ Note the third is
        bound on DELETE only — create/update stay shape-only, which is why the
        script reports the verb and not just the collection."""
        self.assertTrue(_path(self.parsed, "/readingLists/{docId}")["requires_auth"])
        self.assertTrue(_path(self.parsed, "/readingPositions/{docId}")["requires_auth"])
        self.assertFalse(
            _path(self.parsed, "/user_content_warnings/{docId}")["requires_auth"]
        )
        self.assertTrue(
            _path(self.parsed, "/user_content_warnings/{docId}", "delete")["requires_auth"]
        )

    def test_audio_positions_is_auth_gated(self):
        self.assertTrue(_path(self.parsed, "/audio_positions/{anchor}")["requires_auth"])

    def test_every_self_write_probe_names_a_path_the_rules_actually_have(self):
        """A probe pointed at a collection that does not exist measures 0 and
        looks like good news."""
        paths = {p["path"] for p in self.parsed["paths"]}
        for probe in SELF_WRITE_PROBES:
            with self.subTest(collection=probe["collection"]):
                self.assertTrue(
                    any(probe["rules_path"] == p for p in paths),
                    f"{probe['rules_path']} is not in firestore.rules",
                )


# ---------------------------------------------------------------------------
# The per-document uid probe
# ---------------------------------------------------------------------------


class ClassifyUidTestCase(unittest.TestCase):
    FIELD_PROBE = {"uid_fields": ("authorUid",), "docid": "none"}
    PREFIX_PROBE = {"uid_fields": (), "docid": "uid_prefix"}
    WHOLE_PROBE = {"uid_fields": (), "docid": "uid"}
    SLUG_PROBE = {"uid_fields": (), "docid": "slug"}

    def test_a_uid_field_is_evidence(self):
        self.assertEqual(
            classify_uid("x", {"authorUid": "abc"}, self.FIELD_PROBE, set()),
            "field:authorUid",
        )

    def test_an_empty_uid_field_is_not_evidence(self):
        self.assertIsNone(classify_uid("x", {"authorUid": ""}, self.FIELD_PROBE, set()))

    def test_a_non_string_uid_field_is_not_evidence(self):
        self.assertIsNone(classify_uid("x", {"authorUid": 7}, self.FIELD_PROBE, set()))

    def test_a_known_uid_in_the_docid_prefix_is_evidence(self):
        known = {"Nq7pLm2aBcDeFgHiJkLmNoPqRsT1"}
        self.assertEqual(
            classify_uid("Nq7pLm2aBcDeFgHiJkLmNoPqRsT1_some-book", {}, self.PREFIX_PROBE, known),
            "docid:known-uid",
        )

    def test_a_uid_SHAPED_prefix_is_weaker_evidence_and_says_so(self):
        """⚠️ Shape is a heuristic, not proof. It is reported under its own
        label so a reader can discount it — an id can look like a uid and be
        something else, and a measurement must not launder a guess."""
        self.assertEqual(
            classify_uid("Nq7pLm2aBcDeFgHiJkLmNoPqRsT1_b", {}, self.PREFIX_PROBE, set()),
            "docid:uid-shaped",
        )

    def test_a_display_name_slug_is_not_a_uid(self):
        self.assertIsNone(classify_uid("skylar_the-way-of-kings", {}, self.PREFIX_PROBE, set()))

    def test_a_slug_keyed_collection_never_reports_a_uid_from_its_id(self):
        """clubs/*/progress/{slug} is keyed by slugifyName(displayName). Even
        a slug that happened to be uid-shaped is not a binding, because
        nothing checks it against request.auth."""
        self.assertIsNone(
            classify_uid("Nq7pLm2aBcDeFgHiJkLmNoPqRsT1", {}, self.SLUG_PROBE, set())
        )

    def test_a_whole_docid_uid_is_evidence(self):
        known = {"uid-1"}
        self.assertEqual(classify_uid("uid-1", {}, self.WHOLE_PROBE, known), "docid:known-uid")

    def test_prefer_docid_ignores_a_uid_field_the_rule_does_not_look_at(self):
        """⚠️ Verify with the RIGHT instrument. canWriteReadingList() compares
        request.auth.uid to the DOC ID. A legacy display-name-keyed document
        that still carries a `uid` field is NOT bound by that rule, and a probe
        reading the field first would report it as bound."""
        probe = {"uid_fields": ("uid",), "docid": "uid_prefix", "prefer": "docid"}
        self.assertIsNone(classify_uid("skylar_some-book", {"uid": "abc"}, probe, set()))

    def test_prefer_docid_still_falls_back_to_the_field(self):
        probe = {"uid_fields": ("uid",), "docid": "none", "prefer": "docid"}
        self.assertEqual(classify_uid("x", {"uid": "abc"}, probe, set()), "field:uid")

    def test_the_two_control_probes_prefer_the_docid(self):
        for probe in SELF_WRITE_PROBES:
            if probe["collection"] in ("readingLists", "readingPositions"):
                with self.subTest(collection=probe["collection"]):
                    self.assertEqual(probe.get("prefer"), "docid")

    def test_the_shape_regex_rejects_something_obviously_not_a_uid(self):
        for bad in ("skylar", "a", "has-a-dash-and-is-long-enough-maybe", "with space"):
            with self.subTest(bad=bad):
                self.assertIsNone(UID_SHAPE.match(bad))

    def test_the_shape_regex_accepts_a_firebase_uid(self):
        self.assertIsNotNone(UID_SHAPE.match("Nq7pLm2aBcDeFgHiJkLmNoPqRsT1"))


class ProbeTableTestCase(unittest.TestCase):
    def test_every_probe_is_fully_specified(self):
        for probe in SELF_WRITE_PROBES:
            with self.subTest(collection=probe["collection"]):
                for key in ("collection", "kind", "rules_path", "docid", "uid_fields", "why"):
                    self.assertIn(key, probe)
                self.assertIn(probe["kind"], ("root", "group"))
                self.assertIn(probe["docid"], ("none", "slug", "uid", "uid_prefix", "auto"))

    def test_the_four_collections_the_todo_names_are_all_probed(self):
        names = {p["collection"] for p in SELF_WRITE_PROBES}
        for required in ("reviews", "comments", "rsvps", "progress"):
            self.assertIn(required, names)

    def test_the_three_already_bound_collections_are_probed_too(self):
        """They are the CONTROL. A measurement of the unbound half alone
        cannot say whether the probe works at all."""
        names = {p["collection"] for p in SELF_WRITE_PROBES}
        for required in ("readingLists", "readingPositions", "user_content_warnings"):
            self.assertIn(required, names)


# ---------------------------------------------------------------------------
# READ-ONLY — the source guard, with teeth
# ---------------------------------------------------------------------------

_WRITE_VERBS = (".set(", ".update(", ".delete(", ".add(", ".create(")
_STORE_TOKENS = ("db.collection", "document(", "firestore.client", "batch(", "collection_group")


def _write_offenders(source: str) -> list[str]:
    offenders = []
    for i, line in enumerate(source.splitlines(), 1):
        code = line.split("#", 1)[0]
        if any(v in code for v in _WRITE_VERBS) and any(t in code for t in _STORE_TOKENS):
            offenders.append(f"{i}: {line.strip()}")
    return offenders


class ReadOnlyTestCase(unittest.TestCase):
    def setUp(self):
        self.source = (REPO_ROOT / "scripts" / "uid_binding_audit.py").read_text(
            encoding="utf-8"
        )

    def test_it_writes_no_document(self):
        self.assertEqual(_write_offenders(self.source), [])

    def test_the_guard_has_teeth(self):
        mutated = self.source + (
            "\ndef _sneaky(db, uid):\n"
            '    db.collection("reviews").document(uid).update({"authorUid": uid})\n'
        )
        self.assertTrue(_write_offenders(mutated))

    def test_it_deploys_no_rules_and_flips_no_flag(self):
        """Phase 5's enforcement is an OWNER decision. This tool must not be
        one command away from making it.

        ⚠️ The tokens are MECHANISMS, not the word "enforce" — the header talks
        about enforcement at length and must be free to, or the guard would
        push the reasoning out of the file to keep itself green."""
        for forbidden in (
            "firebase deploy",
            "--only firestore",
            "ESTATE_CHECK",
            "wrangler secret",
            "AUTH_ROUTES_",
        ):
            self.assertNotIn(forbidden, self.source, f"{forbidden!r} must not appear here")

    def test_it_says_read_only_in_its_own_header(self):
        self.assertIn("READ-ONLY", self.source)


if __name__ == "__main__":
    unittest.main()
