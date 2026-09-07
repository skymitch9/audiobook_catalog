"""Tests for scripts/audit_ebooks_r2.py — the FIRST independent read of the
`estate-ebooks` bucket.

⚠️ The pure helpers only. Nothing here touches R2, needs a credential, or is
skipped in CI: `diff_keys` and `total_bytes` are where a wrong answer would be
believed, because the whole point of the script is that its output is the only
thing anybody has ever measured about that bucket. A listing bug that reported
"all agree" would be indistinguishable from a healthy bucket.
"""

import unittest

from scripts.audit_ebooks_r2 import diff_keys, human_bytes, total_bytes


def rec(**sizes):
    return {k: {"size": v} for k, v in sizes.items()}


def buc(**sizes):
    return {k: {"size": v, "etag": "x"} for k, v in sizes.items()}


class DiffKeysTests(unittest.TestCase):
    def test_agreement_reports_nothing_and_counts_the_overlap(self):
        d = diff_keys(rec(a=10, b=20), buc(a=10, b=20))
        self.assertEqual(d["missing"], [])
        self.assertEqual(d["orphans"], [])
        self.assertEqual(d["size_mismatch"], [])
        self.assertEqual(d["agreed"], 2)

    def test_a_key_the_record_claims_and_the_bucket_lacks_is_MISSING(self):
        # 🔴 The one that hurts: the reader resolves anchor -> path and asks
        # for this key, so this is a 404 in somebody's book.
        d = diff_keys(rec(a=10, gone=99), buc(a=10))
        self.assertEqual(d["missing"], ["gone"])
        self.assertEqual(d["orphans"], [])

    def test_a_key_only_the_bucket_has_is_an_ORPHAN_not_a_missing_one(self):
        # ⚠️ The two are never summed. An orphan is expected and harmless — a
        # re-filed book leaves its old object behind — and a fix that treated
        # it like a missing object would DELETE from the bucket.
        d = diff_keys(rec(a=10), buc(a=10, leftover=5))
        self.assertEqual(d["orphans"], ["leftover"])
        self.assertEqual(d["missing"], [])

    def test_both_directions_at_once_stay_separate(self):
        d = diff_keys(rec(a=1, b=2), buc(b=2, c=3))
        self.assertEqual(d["missing"], ["a"])
        self.assertEqual(d["orphans"], ["c"])
        self.assertEqual(d["agreed"], 1)

    def test_a_size_disagreement_is_reported_with_BOTH_numbers(self):
        # A count alone sends the next person to re-derive which side is wrong.
        d = diff_keys(rec(a=100), buc(a=90))
        self.assertEqual(d["missing"], [])
        self.assertEqual(
            d["size_mismatch"], [{"key": "a", "record_bytes": 100, "bucket_bytes": 90}]
        )

    def test_a_record_entry_with_NO_size_is_not_compared_as_zero(self):
        # ⚠️ Absent is not zero. An entry written before the size field existed
        # would otherwise invent a mismatch against every real object.
        d = diff_keys({"a": {}}, buc(a=100))
        self.assertEqual(d["size_mismatch"], [])
        self.assertEqual(d["agreed"], 1)

    def test_keys_come_back_sorted_so_two_runs_are_comparable(self):
        d = diff_keys(rec(z=1, a=1, m=1), buc())
        self.assertEqual(d["missing"], ["a", "m", "z"])

    def test_an_empty_bucket_is_every_key_missing_not_a_pass(self):
        # The failure this script exists to end: a listing that came back empty
        # for any reason must read as total drift, never as agreement.
        d = diff_keys(rec(a=1, b=2), {})
        self.assertEqual(d["missing"], ["a", "b"])
        self.assertEqual(d["agreed"], 0)


class TotalBytesTests(unittest.TestCase):
    def test_sums_when_every_entry_carries_a_size(self):
        self.assertEqual(total_bytes(rec(a=10, b=32)), 42)

    def test_ONE_missing_size_makes_the_whole_total_unknown(self):
        # ⚠️ All-or-nothing on purpose: a partial sum is a smaller number
        # wearing a complete one's clothes, and it gets compared against the
        # bucket's real total.
        self.assertIsNone(total_bytes({"a": {"size": 10}, "b": {}}))

    def test_a_non_integer_size_does_not_slip_through(self):
        self.assertIsNone(total_bytes({"a": {"size": "10"}}))

    def test_empty_is_zero_not_unknown(self):
        self.assertEqual(total_bytes({}), 0)


class HumanBytesTests(unittest.TestCase):
    def test_unknown_is_said_in_words_never_rendered_as_zero(self):
        self.assertEqual(human_bytes(None), "unknown")

    def test_scales_and_keeps_the_unit_visible(self):
        self.assertEqual(human_bytes(512), "512 B")
        self.assertIn("MiB", human_bytes(5 * 1024 * 1024))
        self.assertIn("GiB", human_bytes(3 * 1024**3))


if __name__ == "__main__":
    unittest.main()
