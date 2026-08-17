"""Tests for the content-warning filter (dedup by topic, severity wins)."""

import unittest

from app.tools.fetch_content_warnings import (
    already_answered,
    carry_over,
    catalog_title_index,
    filter_warnings,
)

URL = "https://example.com/warnings"


def w(label, url=URL):
    return {"label": label, "source_url": url}


class FilterWarningsTestCase(unittest.TestCase):
    def test_severity_tiers_dedupe_to_highest(self):
        out = filter_warnings([
            w("Moderate: Death"), w("Graphic: Death"), w("Minor: Death"),
        ])
        self.assertEqual(out, [w("Graphic: Death")])

    def test_first_seen_order_is_kept(self):
        out = filter_warnings([
            w("Graphic: Violence"), w("Moderate: Death"), w("Graphic: Death"),
        ])
        self.assertEqual([x["label"] for x in out], ["Graphic: Violence", "Graphic: Death"])

    def test_unprefixed_label_upgrades_to_prefixed(self):
        out = filter_warnings([w("Death"), w("Graphic: Death")])
        self.assertEqual(out, [w("Graphic: Death")])

    def test_case_insensitive_topic_match(self):
        out = filter_warnings([w("graphic: death"), w("Moderate: DEATH")])
        self.assertEqual(out, [w("graphic: death")])

    def test_distinct_topics_survive(self):
        out = filter_warnings([w("Child abuse"), w("Domestic violence")])
        self.assertEqual(len(out), 2)

    def test_question_labels_dropped(self):
        # DoesTheDogDie topics phrased as questions are not warnings.
        out = filter_warnings([
            w("Is a child abused"), w("Does the dog die"),
            w("Are there jump scares?"), w("Graphic: Is there gore"),
        ])
        self.assertEqual(out, [])

    def test_statement_labels_and_ambiguous_kept(self):
        # Clear statements pass; a label that isn't obviously a question stays.
        out = filter_warnings([w("Animal death"), w("Self-harm")])
        self.assertEqual([x["label"] for x in out], ["Animal death", "Self-harm"])

    def test_missing_or_bad_source_urls_dropped(self):
        out = filter_warnings([
            w("Death", url=""), w("Violence", url="ftp://nope"), w("War"),
        ])
        self.assertEqual(out, [w("War")])

    def test_caps_at_forty_topics(self):
        out = filter_warnings([w(f"Topic {i}") for i in range(50)])
        self.assertEqual(len(out), 40)


class AlreadyAnsweredTestCase(unittest.TestCase):
    """⚠️ The queue dedupe — 'not relook them up' (owner, 2026-08-17).

    `content_warnings.json` is keyed by this catalog's full title string, and a
    request may name the same book by its plain one. Re-running the chain is
    what the owner said not to do, and it files a SECOND entry for one work that
    can then drift from the first.

    Every fixture below is a real catalog pair.
    """

    # (title, author) exactly as `catalog_books()` yields them.
    ROWS = [
        ("Onyx Storm - Empyrean, Book 3", "Rebecca Yarros"),
        ("Words of Radiance - The Stormlight Archive, Book 2", "Brandon Sanderson"),
        # The measured ambiguous bucket: one main title, two real editions.
        ("Elantris", "Brandon Sanderson"),
        ("Elantris - Tenth Anniversary Special Edition", "Brandon Sanderson"),
    ]

    def setUp(self):
        self.index = catalog_title_index(self.ROWS)
        self.data = {
            "Onyx Storm - Empyrean, Book 3": {
                "warnings": [w("Death"), w("War")],
                "source": "hardcover",
                "checked_at": 1_755_000_000,
            },
            # Checked and found nothing — a different fact, deliberately.
            "Words of Radiance - The Stormlight Archive, Book 2": {
                "warnings": [], "source": "none", "checked_at": 1_755_000_001,
            },
        }

    def test_a_plain_title_resolves_to_the_answered_full_title(self):
        self.assertEqual(
            already_answered("Onyx Storm", "Rebecca Yarros", self.data, self.index),
            "Onyx Storm - Empyrean, Book 3",
        )

    def test_a_title_this_file_already_holds_answers_itself(self):
        self.assertEqual(
            already_answered("Onyx Storm - Empyrean, Book 3", "Rebecca Yarros",
                             self.data, self.index),
            "Onyx Storm - Empyrean, Book 3",
        )

    def test_an_unknown_book_is_not_answered(self):
        self.assertIsNone(
            already_answered("Goodnight Moon", "Margaret Wise Brown", self.data, self.index))

    def test_a_queued_request_with_no_author_still_resolves(self):
        # ⚠️ THE case the queue actually presents: a `cw_requests` document
        # carries a `bookTitle` and no author whatsoever. An author-only rung
        # would never fire on the real queue, which is how the first draft of
        # this dedupe silently did nothing.
        self.assertEqual(
            already_answered("Onyx Storm", "", self.data, self.index),
            "Onyx Storm - Empyrean, Book 3",
        )

    def test_a_main_title_two_authors_share_is_refused(self):
        # The title-only rung is STRICTER about collisions, not looser: a main
        # title held by two different books is dropped whoever asks. Both of
        # these are really in this catalog.
        index = catalog_title_index([
            ("Wicked - A Wicked Saga, Book 1", "Jennifer L. Armentrout"),
            ("Wicked - The Life and Times of the Wicked Witch of the West", "Gregory Maguire"),
        ])
        data = {"Wicked - A Wicked Saga, Book 1": {"warnings": [w("Death")]}}
        self.assertNotIn("wicked", index["by_title"])
        self.assertIsNone(already_answered("Wicked", "", data, index))

    def test_a_checked_clean_entry_is_not_carried(self):
        # "Published sources were searched and listed none" is not an answer
        # worth propagating — those get a fresh look, which is existing
        # behaviour this must not quietly change.
        self.assertIsNone(
            already_answered("Words of Radiance", "Brandon Sanderson",
                             self.data, self.index))

    def test_an_ambiguous_main_title_is_refused_not_guessed(self):
        # Elantris and its Tenth Anniversary edition share a main title, so the
        # bucket is dropped from the index entirely. Measured 2026-08-17: 5 of
        # 1,069 buckets are like this. Carrying one volume's warnings onto
        # another is a wrong answer, and a wrong warning is worse than none.
        self.assertNotIn(("elantris", "brandon sanderson"), self.index["by_author"])
        self.assertNotIn("elantris", self.index["by_title"])
        self.assertIsNone(
            already_answered("Elantris", "Brandon Sanderson", self.data, self.index))

    def test_carry_over_copies_the_answer_and_records_where_from(self):
        entry = carry_over(self.data, "Onyx Storm", "Onyx Storm - Empyrean, Book 3")
        self.assertEqual([x["label"] for x in entry["warnings"]], ["Death", "War"])
        # ⚠️ The canonical entry's own provenance survives: `checked_at` is when
        # THAT answer was found, not when it was copied. Claiming a fresh check
        # would be a measurement wearing a guess's clothes.
        self.assertEqual(entry["source"], "hardcover")
        self.assertEqual(entry["checked_at"], 1_755_000_000)
        self.assertEqual(entry["carried_from"], "Onyx Storm - Empyrean, Book 3")
        # A copy, not a pointer: three front ends in two repos read this file
        # unchanged, and the consumer contract is `warnings` + `checked_at`.
        self.assertEqual(self.data["Onyx Storm"]["warnings"], entry["warnings"])

    def test_carrying_does_not_disturb_the_canonical_entry(self):
        before = dict(self.data["Onyx Storm - Empyrean, Book 3"])
        carry_over(self.data, "Onyx Storm", "Onyx Storm - Empyrean, Book 3")
        self.assertEqual(self.data["Onyx Storm - Empyrean, Book 3"], before)

    def test_the_carried_entry_is_itself_answered_so_it_never_requeues(self):
        # The loop-closing property: once carried, the requested spelling is a
        # key in its own right, so the same book cannot re-enter the queue every
        # hour and pay for the chain again.
        carry_over(self.data, "Onyx Storm", "Onyx Storm - Empyrean, Book 3")
        self.assertEqual(
            already_answered("Onyx Storm", "Rebecca Yarros", self.data, self.index),
            "Onyx Storm",
        )


if __name__ == "__main__":
    unittest.main()
