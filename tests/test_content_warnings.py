"""Tests for the content-warning filter (dedup by topic, severity wins)."""

import unittest

from app.tools.fetch_content_warnings import filter_warnings

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


if __name__ == "__main__":
    unittest.main()
