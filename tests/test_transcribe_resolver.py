# Pins resolve_m4b's join, added 2026-08-18 after the first real nightly book
# failed on it: the queue title carries a " - Series, Book N" tail that
# OpenAudible's filename may or may not keep. The fallback strips one " - "
# segment at a time, and REFUSES an ambiguous stripped match rather than pick
# one - transcribing the wrong book reports itself as success.
import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "transcribe_audiobook",
    Path(__file__).resolve().parents[1] / "scripts" / "transcribe_audiobook.py",
)
ta = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ta)


@pytest.fixture()
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(ta, "LIBRARY_ROOT", tmp_path)
    monkeypatch.setattr(ta, "load_catalog", lambda: [])
    return tmp_path


def _m4b(root: Path, name: str) -> Path:
    path = root / f"{name}.m4b"
    path.write_bytes(b"")
    return path


def test_exact_normalised_match_wins(library):
    # The colon-vs-dash case the docstring names: filename punctuation differs.
    want = _m4b(library, "The Primal Hunter 9- A LitRPG Adventure")
    assert ta.resolve_m4b("The Primal Hunter 9: A LitRPG Adventure") == want


def test_series_tail_stripped_when_filename_lacks_it(library):
    # The first real nightly book. File is title-only; queue title has a tail.
    want = _m4b(library, "A Court of Thorns and Roses (Part 1 of 2) (Dramatized Adaptation)")
    _m4b(library, "A Court of Thorns and Roses (Part 2 of 2) (Dramatized Adaptation)")
    got = ta.resolve_m4b(
        "A Court of Thorns and Roses (Part 1 of 2) (Dramatized Adaptation)"
        " - A Court of Thorns and Roses, Book 1")
    assert got == want


def test_filename_keeping_its_tail_still_matches_first(library):
    # Fourth Wing's shape: the filename kept the tail, first pass must win
    # before any stripping happens.
    want = _m4b(library, "Fourth Wing - Empyrean, Book 1")
    assert ta.resolve_m4b("Fourth Wing - Empyrean, Book 1") == want


def test_ambiguous_stripped_match_is_refused_with_words(library):
    # Two files would both answer the stripped title: refuse, never pick.
    _m4b(library, "Legion")
    (library / "other").mkdir()
    _m4b(library / "other", "Legion")
    with pytest.raises(FileNotFoundError, match="refusing to guess"):
        ta.resolve_m4b("Legion - The Many, Book 1")


def test_no_match_still_says_not_found(library):
    _m4b(library, "Some Other Book")
    with pytest.raises(FileNotFoundError, match="no .m4b under"):
        ta.resolve_m4b("This Book Does Not Exist - Nowhere, Book 99")
