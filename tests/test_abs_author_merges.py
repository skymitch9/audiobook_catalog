# tests/test_abs_author_merges.py
#
# `scripts/merge_abs_authors.py` writes to a LIVE Audiobookshelf server and its
# writes are not cheaply reversible — a merge folds two author records into one
# and there is no undo button. So the table is guarded here, and the guard that
# matters most is the LAST class in this file:
#
#   ⚠️ every merge target must be the plain un-flip of its source.
#
# That is what stops somebody quietly adding `"Rik Hoskin" -> "Brandon
# Sanderson"` because `scripts/author_shelf_aliases.json` says so. That file
# decides WHERE FILES ARE SHELVED; this table decides WHO WROTE A BOOK, and
# `app/author_names.py` opens by recording the 2026-08-09 incident in which
# reading one as the other merged two people's bibliographies.

from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.merge_abs_authors import MERGES_PATH, check_rows, load_merges
from scripts.rename_epubs import normalize_creator

REPO_ROOT = Path(__file__).resolve().parent.parent


def _author(aid, name, books, image=False):
    return {"id": aid, "name": name, "numBooks": books, "imagePath": "x" if image else None}


class TableIntegrity(unittest.TestCase):
    def setUp(self):
        self.rows = load_merges()

    def test_the_shipped_table_loads_and_is_not_empty(self):
        self.assertGreaterEqual(len(self.rows), 20)

    def test_no_row_renames_an_author_to_itself(self):
        for row in self.rows:
            self.assertNotEqual(row["from"], row["to"], row)

    def test_no_duplicate_sources(self):
        froms = [r["from"] for r in self.rows]
        self.assertEqual(len(froms), len(set(froms)))

    def test_every_row_says_why(self):
        # A merge nobody explained is a merge nobody can review.
        for row in self.rows:
            self.assertTrue((row.get("reason") or "").strip(), row["from"])

    def test_every_source_id_is_a_uuid_shaped_string(self):
        for row in self.rows:
            self.assertRegex(row["from_id"], r"^[0-9a-f-]{36}$", row["from"])

    def test_a_self_renaming_row_is_rejected_at_load(self):
        bad = REPO_ROOT / "tests" / "fixtures" / "_bad_merges.json"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text(json.dumps({"merges": [
            {"from": "A", "from_id": "x", "to": "A", "expect_books": 1}]}),
            encoding="utf-8")
        try:
            with self.assertRaises(ValueError):
                load_merges(bad)
        finally:
            bad.unlink()


class LiveAssertions(unittest.TestCase):
    """⚠️ The refusals. A row is checked against the server, never repaired."""

    ROWS = [{"from": "Wight, Will", "from_id": "id-1", "expect_books": 15,
             "to": "Will Wight", "expect_merge_into": "id-2",
             "reason": "test"}]

    def test_a_matching_row_is_applicable(self):
        authors = [_author("id-1", "Wight, Will", 15), _author("id-2", "Will Wight", 3)]
        applicable, problems = check_rows(self.ROWS, authors)
        self.assertEqual(problems, [])
        self.assertEqual(len(applicable), 1)
        self.assertEqual(applicable[0]["_state"], "merge")
        self.assertEqual(applicable[0]["_target_books"], 3)

    def test_a_renamed_source_is_refused_not_adapted_to(self):
        authors = [_author("id-1", "Somebody Else", 15), _author("id-2", "Will Wight", 3)]
        applicable, problems = check_rows(self.ROWS, authors)
        self.assertEqual(applicable, [])
        self.assertIn("the table is stale", problems[0])

    def test_a_drifted_book_count_is_refused(self):
        # The count is the cheapest proof that the row still describes the same
        # situation somebody looked at. If books arrived or left, re-measure.
        authors = [_author("id-1", "Wight, Will", 16), _author("id-2", "Will Wight", 3)]
        applicable, problems = check_rows(self.ROWS, authors)
        self.assertEqual(applicable, [])
        self.assertIn("re-measure", problems[0])

    def test_a_missing_source_id_is_refused(self):
        # ⚠️ Neither the source NOR the target present. With the target
        # present this is the already-applied case below, and telling those two
        # apart is the whole reason the idempotency check looks at both.
        authors = [_author("id-9", "Someone Else", 1)]
        applicable, problems = check_rows(self.ROWS, authors)
        self.assertEqual(applicable, [])
        self.assertIn("not in the library", problems[0])

    def test_the_wrong_merge_target_is_refused(self):
        # The name is right and the id is not: two authors called Will Wight,
        # or a target that was itself merged since the table was written.
        authors = [_author("id-1", "Wight, Will", 15), _author("id-9", "Will Wight", 3)]
        applicable, problems = check_rows(self.ROWS, authors)
        self.assertEqual(applicable, [])
        self.assertIn("expected to merge into", problems[0])

    def test_an_unexpected_target_turns_a_rename_into_a_merge_and_is_refused(self):
        rows = [{"from": "Krout, Dakota", "from_id": "id-1", "expect_books": 1,
                 "to": "Dakota Krout", "expect_merge_into": None, "reason": "t"}]
        authors = [_author("id-1", "Krout, Dakota", 1), _author("id-7", "Dakota Krout", 46)]
        applicable, problems = check_rows(rows, authors)
        self.assertEqual(applicable, [])
        self.assertIn("would", problems[0])

    def test_an_already_applied_row_is_idempotent_not_an_error(self):
        # Re-running after a successful commit must not look like a failure.
        authors = [_author("id-2", "Will Wight", 18)]
        applicable, problems = check_rows(self.ROWS, authors)
        self.assertEqual(problems, [])
        self.assertEqual(applicable[0]["_state"], "already-applied")

    def test_a_row_may_target_a_name_an_earlier_row_creates(self):
        # 'Hoskin, Rik' -> 'Rik Hoskin' precedes 'Rik Hoskin, Julius Gopez' ->
        # 'Rik Hoskin'. The second must be labelled a MERGE in the dry run, or
        # the preview lies about what the real run will do.
        rows = [
            {"from": "Hoskin, Rik", "from_id": "id-a", "expect_books": 1,
             "to": "Rik Hoskin", "expect_merge_into": None, "reason": "t"},
            {"from": "Rik Hoskin, Julius Gopez", "from_id": "id-b", "expect_books": 1,
             "to": "Rik Hoskin", "expect_merge_into": None, "reason": "t"},
        ]
        authors = [_author("id-a", "Hoskin, Rik", 1),
                   _author("id-b", "Rik Hoskin, Julius Gopez", 1)]
        applicable, problems = check_rows(rows, authors)
        self.assertEqual(problems, [])
        self.assertEqual([r["_state"] for r in applicable], ["rename", "merge"])


class TargetsAreAuthorshipNotShelving(unittest.TestCase):
    """🔴 THE GUARD THIS FILE EXISTS FOR."""

    def test_every_target_is_the_plain_unflip_of_its_source(self):
        # Two escape hatches, both of which must be declared IN THE ROW:
        #   "variant": a spelling variant of the same pen name (the one-file
        #              ©ART typo 'Mashton X X'), not a comma flip;
        #   "judgement": a decision a human made and wrote down (the two-person
        #              credit string on whitesand.epub).
        # Anything else must be exactly what normalize_creator produces, which
        # is what keeps a shelf alias from becoming an authorship claim.
        for row in load_merges():
            if row.get("variant") or row.get("judgement"):
                continue
            with self.subTest(source=row["from"]):
                self.assertEqual(normalize_creator(row["from"])[0], row["to"])

    def test_the_declared_exceptions_are_still_only_the_ones_reviewed(self):
        # If a third exception appears, somebody should have to come here and
        # say so on purpose.
        flagged = [r["from"] for r in load_merges() if r.get("variant") or r.get("judgement")]
        self.assertEqual(sorted(flagged),
                         ["Mashton X X", "Mashton X Y", "Rik Hoskin, Julius Gopez"])

    def test_no_target_is_a_shelf_alias_of_its_source(self):
        # The direct form of the same rule, read out of the live alias file:
        # author_shelf_aliases.json says 'Rik Hoskin' -> 'Brandon Sanderson'
        # and 'Travis Deverell' -> 'Shirtaloon'. Neither may appear as a merge.
        from app.author_names import load_shelf_aliases

        aliases = load_shelf_aliases()
        for row in load_merges():
            if row.get("variant") or row.get("judgement"):
                # ⚠️ Declared and reviewed. 'Mashton X X' -> 'Mashton XX' is
                # ALSO a shelf-alias row, and coincidentally so: both files
                # agree that one m4b's ©ART tag spelled the same pen name with
                # spaces. The guard exists to catch UNDECLARED agreement with
                # that file, which is the dangerous kind.
                continue
            shelf = aliases.get(row["to"].lower())
            if shelf and shelf != row["to"]:
                self.assertNotEqual(
                    row["to"], shelf,
                    f"{row['from']!r} -> {row['to']!r} follows a SHELVING alias")
            # And the target must never BE the alias value for the un-flipped
            # source, which is the shape the 2026-08-09 incident took.
            unflipped = normalize_creator(row["from"])[0] or ""
            aliased = aliases.get(unflipped.lower())
            if aliased and aliased != unflipped:
                self.assertNotEqual(
                    row["to"], aliased,
                    f"{row['from']!r} -> {row['to']!r} is the SHELF alias of "
                    f"{unflipped!r}; this table is authorship")

    def test_the_table_and_the_shipped_file_are_the_same_thing(self):
        self.assertTrue(MERGES_PATH.is_file())
        self.assertEqual(MERGES_PATH.name, "abs_author_merges.json")


if __name__ == "__main__":
    unittest.main()
