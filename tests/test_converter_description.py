"""The converter writes Audible's publisher summary into each NEW m4b, and the
single-file path hands the export row (not ffmpeg's argument list) to the tag
writer (both 2026-11-24 evening). No network, no real download."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.tools import audible_download as ad


def test_audible_tags_carries_the_summary_only_when_there_is_one():
    assert ad.audible_tags("A1", {}, "", (), summary="  A tale.  ")["ldes"] == "A tale."
    assert "ldes" not in ad.audible_tags("A1", {}, "", (), summary="")


def test_fetch_publisher_summary_is_plain_text_and_never_raises(monkeypatch):
    seen = {}

    def fake(profile, *args, timeout=0):
        seen["args"] = args
        return SimpleNamespace(returncode=0, stdout=json.dumps(
            {"product": {"asin": "A1", "publisher_summary": "<p>One &amp; two.</p>"}}))

    monkeypatch.setattr(ad, "_audible", fake)
    assert ad.fetch_publisher_summary("A1", "skylar") == "One & two."
    assert seen["args"][:2] == ("api", "1.0/catalog/products/A1")

    monkeypatch.setattr(ad, "_audible", lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))
    assert ad.fetch_publisher_summary("A1", "skylar") == ""

    def boom(*a, **k):
        raise TimeoutError

    monkeypatch.setattr(ad, "_audible", boom)
    assert ad.fetch_publisher_summary("A1", "skylar") == ""
    assert ad.fetch_publisher_summary("", "skylar") == ""
    assert ad.fetch_publisher_summary("A1", None) == ""


def test_single_file_download_passes_the_export_row_to_the_tagger(monkeypatch, tmp_path):
    """The bug: `meta` was reassigned to ffmpeg's ['-metadata', 'composer=…']
    list, so write_audible_tags received a list and wrote nothing."""
    row = {"asin": "A1", "genres": "Fantasy", "release_date": "2020-01-01"}

    def fake_audible(profile, *args, timeout=0):
        out = Path(args[args.index("--output-dir") + 1])
        (out / "book.aax").write_bytes(b"x")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_run(cmd, timeout=0):
        Path(cmd[-1]).write_bytes(b"m4b")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    got = {}
    monkeypatch.setattr(ad, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(ad, "_audible", fake_audible)
    monkeypatch.setattr(ad, "_run", fake_run)
    monkeypatch.setattr(ad, "activation_bytes", lambda p: "deadbeef")
    monkeypatch.setattr(ad, "_tag_new_file", lambda dest, asin, meta, profile=None: got.update(meta=meta, profile=profile))

    dest = ad.download_and_convert("A1", "Book", "skylar", out_dir=tmp_path, narrator="Someone", meta=row)
    assert dest.name == "Book.m4b"
    assert got == {"meta": row, "profile": "skylar"}


FFMPEG = shutil.which("ffmpeg")


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not on PATH")
def test_written_summary_is_what_the_catalogue_reads(tmp_path):
    """Exercise the real tag write and the real reader on a 1-second m4b."""
    from mutagen.mp4 import MP4

    from app.metadata import _extract_description

    m4b = tmp_path / "t.m4b"
    subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t", "1",
                    "-c:a", "aac", str(m4b)], check=True, capture_output=True)
    ad.write_audible_tags(m4b, "A1", {}, summary="One & two.\n\nThree.")
    assert _extract_description(MP4(str(m4b)).tags) == "One & two.\n\nThree."

    # a container that already has a description keeps it
    mp4 = MP4(str(m4b))
    mp4.tags["ldes"] = ["Own blurb"]
    mp4.save()
    ad.write_audible_tags(m4b, "A1", {}, summary="Audible's")
    assert _extract_description(MP4(str(m4b)).tags) == "Own blurb"
