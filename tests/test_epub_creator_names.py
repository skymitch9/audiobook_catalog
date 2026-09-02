# tests/test_epub_creator_names.py
#
# `scripts/rename_epubs.normalize_creator` — the one place the pipeline turns a
# publisher's `<dc:creator>` into a display-order author name.
#
# ⚠️ EVERY CASE BELOW IS A STRING MEASURED IN THE LIVE LIBRARY on 2026-09-02
# (132 epubs under ROOT_DIR, read straight out of their OPFs). They are not
# invented examples: the flipped ones are why Audiobookshelf grew 22 author
# records like `Wight, Will` beside the real ones, and the ambiguous one is why
# it grew an author who does not exist.
#
# The value this pins is a PERSISTED one — it reaches `site/ebooks.json`, the
# gated ebook manifest, the search index's `creator` field and epub filenames —
# so a change here is a migration, not an edit.

from __future__ import annotations

import unittest

from scripts.rename_epubs import normalize_creator


class NormalizeCreatorFlips(unittest.TestCase):
    """Unambiguous "Surname, Given" forms, all measured in the library."""

    def test_bare_surname_on_the_left_is_a_flip(self):
        for raw, expected in [
            ("Wight, Will", "Will Wight"),
            ("English, Miles", "Miles English"),
            ("Krout, Dakota", "Dakota Krout"),
            ("Myth, Selkie", "Selkie Myth"),
            ("Rae, Honour", "Honour Rae"),
            ("Yarros, Rebecca", "Rebecca Yarros"),
            ("Omer, Mike", "Mike Omer"),
            ("Swain, James", "James Swain"),
            ("Colombe, Erik", "Erik Colombe"),
            ("Marcum, Diana", "Diana Marcum"),
            ("Niemitz, David", "David Niemitz"),
            ("Javernick, Ellen", "Ellen Javernick"),
            ("Deverell, Travis", "Travis Deverell"),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_creator(raw), (expected, False))

    def test_multi_token_given_names_survive_the_flip(self):
        # The GIVEN side may be several tokens; only the surname side is the
        # signal. All three are real rows.
        self.assertEqual(normalize_creator("Clayton, Meg Waite"),
                         ("Meg Waite Clayton", False))
        self.assertEqual(normalize_creator("King, Kerry Anne"),
                         ("Kerry Anne King", False))
        self.assertEqual(normalize_creator("Saileri, J. R."),
                         ("J. R. Saileri", False))
        self.assertEqual(normalize_creator("Durand, Maxime J."),
                         ("Maxime J. Durand", False))

    def test_all_caps_surname_is_not_mangled(self):
        # ⚠️ `Mashton XX` is a pen name and the caps are load-bearing. A
        # title-casing normaliser turns it into `Mashton Xx` and splits the
        # author; this one must not touch case at all.
        self.assertEqual(normalize_creator("XX, Mashton"), ("Mashton XX", False))

    def test_a_particle_opens_a_surname_not_a_person(self):
        # "the Mad" cannot be somebody's complete name, so the comma is a flip
        # even though the left side has two tokens.
        self.assertEqual(normalize_creator("the Mad, Sir Bedivere"),
                         ("Sir Bedivere the Mad", False))


class NormalizeCreatorRefusals(unittest.TestCase):
    """⚠️ The half that matters: what it declines to decide."""

    def test_two_full_names_are_left_alone_and_flagged(self):
        # 🔴 THE REGRESSION THIS FILE EXISTS FOR. The old heuristic
        # (`", " in author and author.count(",") == 1`) turned this into
        # "Julius Gopez Rik Hoskin" — a person who does not exist — and that
        # name reached the manifest, the index and an ABS author record.
        name, ambiguous = normalize_creator("Rik Hoskin, Julius Gopez")
        self.assertEqual(name, "Rik Hoskin, Julius Gopez")
        self.assertTrue(ambiguous)
        self.assertNotIn("Julius Gopez Rik", name)

    def test_more_than_one_comma_is_never_guessed_at(self):
        # Two flipped people, or three plain ones? Not recoverable.
        name, ambiguous = normalize_creator("Hoskin, Rik, Sanderson, Brandon")
        self.assertEqual(name, "Hoskin, Rik, Sanderson, Brandon")
        self.assertTrue(ambiguous)

    def test_ambiguous_is_false_when_there_was_nothing_to_decide(self):
        self.assertEqual(normalize_creator("Brandon Sanderson"),
                         ("Brandon Sanderson", False))
        self.assertEqual(normalize_creator("Shirtaloon"), ("Shirtaloon", False))


class NormalizeCreatorHygiene(unittest.TestCase):
    def test_trailing_space_inside_the_element_does_not_defeat_the_flip(self):
        # Measured: one Bog Standard Isekai epub's creator is "English, Miles ".
        self.assertEqual(normalize_creator("English, Miles "),
                         ("Miles English", False))

    def test_runs_of_whitespace_and_newlines_collapse(self):
        self.assertEqual(normalize_creator("Wight,\n   Will"),
                         ("Will Wight", False))

    def test_empty_and_missing_creators_return_none(self):
        for raw in (None, "", "   ", ","):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_creator(raw), (None, False))


if __name__ == "__main__":
    unittest.main()
