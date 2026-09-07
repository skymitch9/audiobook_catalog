"""Tests for scripts/survey_moov_atoms.py — the faststart survey.

⚠️ The box walk is the whole measurement, and a walk that desynchronises does
not crash: it reports a `moov` position it invented, and the table looks fine.
So every arm of the MP4 size encoding gets a fixture built byte by byte here,
including the 64-bit `largesize` form a >4 GiB `mdat` is REQUIRED to use — the
library has files of that size, so the untested arm would have been the one
that ran.

No media and no ffprobe: these build containers in memory.
"""

import io
import struct
import unittest

from scripts.survey_moov_atoms import (
    HEAD,
    TAIL,
    UNKNOWN,
    classify,
    summarise,
    tail_read_bytes,
    walk_top_level,
)


def box(box_type: str, payload: bytes = b"", force64: bool = False) -> bytes:
    """One top-level box. `force64` writes the size-1 + largesize form."""
    body = box_type.encode("latin-1") + payload
    if force64:
        return struct.pack(">I", 1) + body[:4] + struct.pack(">Q", len(body) + 12) + body[4:]
    return struct.pack(">I", len(body) + 4) + body


def walk(data: bytes):
    return walk_top_level(io.BytesIO(data), len(data))


class WalkTests(unittest.TestCase):
    def test_a_faststart_file_yields_moov_before_mdat(self):
        data = box("ftyp", b"M4A ") + box("moov", b"\x00" * 40) + box("mdat", b"\x00" * 500)
        types = [b[0] for b in walk(data)]
        self.assertEqual(types, ["ftyp", "moov", "mdat"])

    def test_the_shape_the_library_actually_has(self):
        # Measured 2026-09-07: 1,004 of 1,084 books are exactly this order.
        data = box("ftyp", b"M4A ") + box("free", b"\x00" * 8) + box("mdat", b"\x00" * 500) + box("moov", b"\x00" * 40)
        types = [b[0] for b in walk(data)]
        self.assertEqual(types, ["ftyp", "free", "mdat", "moov"])

    def test_offsets_and_sizes_are_the_real_byte_positions(self):
        ftyp = box("ftyp", b"M4A ")
        mdat = box("mdat", b"\x00" * 100)
        data = ftyp + mdat + box("moov", b"\x00" * 40)
        boxes = walk(data)
        self.assertEqual(boxes[1], ("mdat", len(ftyp), len(mdat)))
        self.assertEqual(boxes[2][1], len(ftyp) + len(mdat))

    def test_the_64_bit_largesize_form_does_not_desynchronise_the_walk(self):
        # ⚠️ The arm that MUST work: an mdat over 4 GiB has no other encoding,
        # and getting it wrong makes every later offset fiction.
        data = box("ftyp", b"M4A ") + box("mdat", b"\x00" * 64, force64=True) + box("moov", b"\x00" * 40)
        boxes = walk(data)
        self.assertEqual([b[0] for b in boxes], ["ftyp", "mdat", "moov"])
        self.assertEqual(boxes[2][1], len(data) - (40 + 8))

    def test_a_size_zero_box_runs_to_end_of_file(self):
        head = box("ftyp", b"M4A ")
        data = head + struct.pack(">I", 0) + b"mdat" + b"\x00" * 60
        boxes = walk(data)
        self.assertEqual([b[0] for b in boxes], ["ftyp", "mdat"])
        self.assertEqual(boxes[1][2], len(data) - len(head))

    def test_a_corrupt_size_STOPS_the_walk_rather_than_guessing_a_step(self):
        # A walk that invents a step forward reports a moov position it made
        # up, which is worse than reporting nothing.
        data = box("ftyp", b"M4A ") + struct.pack(">I", 3) + b"junk" + b"\x00" * 40
        self.assertEqual([b[0] for b in walk(data)], ["ftyp"])

    def test_a_truncated_header_ends_the_walk_cleanly(self):
        data = box("ftyp", b"M4A ") + b"\x00\x00"
        self.assertEqual([b[0] for b in walk(data)], ["ftyp"])

    def test_an_empty_file_yields_nothing(self):
        self.assertEqual(walk(b""), [])


class ClassifyTests(unittest.TestCase):
    def test_moov_first_is_head(self):
        self.assertEqual(classify(24, 900), HEAD)

    def test_moov_last_is_tail(self):
        self.assertEqual(classify(900, 24), TAIL)

    def test_no_moov_is_UNKNOWN_and_never_folded_into_either_count(self):
        # ⚠️ Unknown is the absence of an answer. Calling it `tail` would
        # inflate the number this survey exists to establish; calling it `head`
        # would hide a real seek cost.
        self.assertEqual(classify(None, 24), UNKNOWN)

    def test_a_moov_with_no_mdat_claims_nothing(self):
        self.assertEqual(classify(24, None), UNKNOWN)


class TailReadTests(unittest.TestCase):
    def test_it_is_the_distance_from_moov_to_the_end(self):
        # Skyward, measured 2026-09-07: 884,534,282 bytes, moov at 874,852,410.
        self.assertEqual(tail_read_bytes(884_534_282, 874_852_410), 9_681_872)

    def test_an_unknown_moov_gives_None_and_NOT_zero(self):
        # 0 would read as "no extra fetch needed", which is the opposite claim.
        self.assertIsNone(tail_read_bytes(1000, None))

    def test_it_never_goes_negative(self):
        self.assertEqual(tail_read_bytes(100, 500), 0)


class SummariseTests(unittest.TestCase):
    def rows(self):
        return [
            {"path": "a", "size": 300, "placement": TAIL, "moov_offset": 290, "tail_read_bytes": 10},
            {"path": "b", "size": 900, "placement": TAIL, "moov_offset": 880, "tail_read_bytes": 20},
            {"path": "c", "size": 100, "placement": HEAD, "moov_offset": 8, "tail_read_bytes": None},
            {"path": "d", "size": 50, "placement": UNKNOWN, "moov_offset": None, "tail_read_bytes": None},
        ]

    def test_counts_keep_the_three_kinds_separate(self):
        s = summarise(self.rows())
        self.assertEqual(s["counts"][TAIL], 2)
        self.assertEqual(s["counts"][HEAD], 1)
        self.assertEqual(s["counts"][UNKNOWN], 1)
        self.assertEqual(s["files"], 4)

    def test_the_largest_tail_files_come_back_biggest_first(self):
        s = summarise(self.rows())
        self.assertEqual([r["path"] for r in s["largest_tail_moov"]], ["b", "a"])

    def test_the_worst_tail_read_is_over_TAIL_files_only(self):
        s = summarise(self.rows())
        self.assertEqual(s["tail_read_bytes_max"], 20)

    def test_a_library_with_no_tail_files_reports_None_not_zero(self):
        s = summarise([r for r in self.rows() if r["placement"] != TAIL])
        self.assertIsNone(s["tail_read_bytes_max"])
        self.assertEqual(s["largest_tail_moov"], [])


if __name__ == "__main__":
    unittest.main()
