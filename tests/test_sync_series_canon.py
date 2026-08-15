"""Tests for app/tools/sync_series_canon.py — the estate series canon sync tool
(normalization item 4).

Three jobs, mirroring tests/test_universes.py's structure for its sibling file:

  * pin plan()'s diffing logic against a fake canon doc, away from the real
    catalog-platform checkout, so these tests do not depend on what the estate
    canon currently contains;
  * prove the tool is additive and idempotent against a sandboxed
    catalog_overrides.json, which is the property the whole design rests on —
    a sync must never delete a local-only fold, and a second run must report
    nothing new;
  * run it once against the REAL catalog-platform checkout (skipped if absent,
    same as test_universes.py) to prove the two real files actually agree.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core import overrides_store as store
from app.core.overrides_store import OverridesError
from app.core.universes import find_platform_dir
from app.tools import sync_series_canon as sync

PLATFORM_DIR, _TRIED = find_platform_dir()
requires_platform = pytest.mark.skipif(
    PLATFORM_DIR is None,
    reason=f"catalog-platform not found (tried: {'; '.join(_TRIED)}). Set CATALOG_PLATFORM_DIR.",
)


def _canon_doc(*entries):
    return {"schemaVersion": 1, "entries": list(entries)}


def _entry(canonical, variants, why="measured 2026-08-14"):
    return {"canonical": canonical, "variants": variants, "evidence": why, "decidedHow": "seed"}


@pytest.fixture()
def sandbox(tmp_path: Path) -> Path:
    """A minimal but valid corrections file, with one LOCAL-ONLY fold already in it."""
    path = tmp_path / "catalog_overrides.json"
    path.write_text(
        json.dumps(
            {
                "canonical_series": {"lions quest": "Lion's Quest"},
                "overrides": [],
                "_unresolved": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------- #
# plan() — pure diffing logic, no real files involved
# --------------------------------------------------------------------------- #


def test_plan_reports_every_variant_including_the_canonical_spelling_folded_onto_itself():
    canon = _canon_doc(_entry("Ascend Online", ["Ascend Online", "Ascend Online [publication order]"]))
    data = {"canonical_series": {}}
    changes = sync.plan(canon, data)
    got = {(v, c) for v, c, _ in changes}
    assert got == {
        ("Ascend Online", "Ascend Online"),
        ("Ascend Online [publication order]", "Ascend Online"),
    }
    assert all(is_new for _, _, is_new in changes)


def test_plan_is_not_new_when_the_fold_already_matches():
    canon = _canon_doc(_entry("Ascend Online", ["Ascend Online", "Ascend Online [publication order]"]))
    data = {
        "canonical_series": {
            "ascend online": "Ascend Online",
            "ascend online [publication order]": "Ascend Online",
        }
    }
    changes = sync.plan(canon, data)
    assert changes and all(not is_new for _, _, is_new in changes)


def test_plan_is_new_when_the_existing_fold_points_somewhere_else():
    """A local override that disagrees with the estate canon is still reported as new —
    sync always proposes the estate's answer; --commit is what actually overwrites it."""
    canon = _canon_doc(_entry("Ascend Online", ["Ascend Online", "Ascend Online [publication order]"]))
    data = {"canonical_series": {"ascend online [publication order]": "Something Else"}}
    changes = sync.plan(canon, data)
    new = {(v, c) for v, c, is_new in changes if is_new}
    assert ("Ascend Online [publication order]", "Ascend Online") in new


def test_plan_skips_entries_with_no_canonical_or_empty_variant():
    canon = _canon_doc({"variants": ["orphan"]}, _entry("Real Series", ["", "  ", "Real Series Alt"]))
    changes = sync.plan(canon, {"canonical_series": {}})
    got = {v for v, _, _ in changes}
    assert "orphan" not in got
    assert "Real Series Alt" in got
    assert "" not in got


def test_plan_dedupes_a_variant_named_twice():
    canon = _canon_doc(_entry("Fae & Alchemy", ["Fae & Alchemy", "Fae & Alchemy", "The Fae & Alchemy Series"]))
    changes = sync.plan(canon, {"canonical_series": {}})
    assert len(changes) == 2


# --------------------------------------------------------------------------- #
# main() against a sandboxed corrections file — additive, idempotent
# --------------------------------------------------------------------------- #


def test_dry_run_writes_nothing(sandbox, tmp_path, capsys):
    canon_dir = tmp_path / "platform"
    (canon_dir / "data").mkdir(parents=True)
    (canon_dir / "data" / "series-canon.json").write_text(
        json.dumps(_canon_doc(_entry("Ascend Online", ["Ascend Online", "Ascend Online [publication order]"]))),
        encoding="utf-8",
    )
    before = sandbox.read_text(encoding="utf-8")

    rc = sync.main(["--overrides", str(sandbox), "--platform-dir", str(canon_dir)])

    assert rc == 0
    assert sandbox.read_text(encoding="utf-8") == before
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "Ascend Online [publication order]" in out


def test_commit_is_additive_and_keeps_the_local_only_fold(sandbox, tmp_path):
    canon_dir = tmp_path / "platform"
    (canon_dir / "data").mkdir(parents=True)
    (canon_dir / "data" / "series-canon.json").write_text(
        json.dumps(
            _canon_doc(
                _entry("Ascend Online", ["Ascend Online", "Ascend Online [publication order]"]),
                _entry("Harry Potter", ["Harry Potter", "Harry Potter (Full-Cast Editions)"]),
            )
        ),
        encoding="utf-8",
    )

    rc = sync.main(["--overrides", str(sandbox), "--platform-dir", str(canon_dir), "--commit"])
    assert rc == 0

    data = store.load(sandbox)
    canon = data["canonical_series"]
    # The local-only fold recorded before this tool ever ran must survive untouched.
    assert canon["lions quest"] == "Lion's Quest"
    # Every estate fold is now present, self-mapped canonical included.
    assert canon["ascend online"] == "Ascend Online"
    assert canon["ascend online [publication order]"] == "Ascend Online"
    assert canon["harry potter"] == "Harry Potter"
    assert canon["harry potter (full-cast editions)"] == "Harry Potter"


def test_a_second_commit_reports_nothing_new(sandbox, tmp_path, capsys):
    canon_dir = tmp_path / "platform"
    (canon_dir / "data").mkdir(parents=True)
    (canon_dir / "data" / "series-canon.json").write_text(
        json.dumps(_canon_doc(_entry("Fae & Alchemy", ["Fae & Alchemy", "The Fae & Alchemy Series"]))),
        encoding="utf-8",
    )
    argv = ["--overrides", str(sandbox), "--platform-dir", str(canon_dir), "--commit"]

    assert sync.main(argv) == 0
    capsys.readouterr()
    assert sync.main(argv) == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_writing_still_validates_and_refuses_a_broken_file(tmp_path):
    """save() refuses to write an invalid corrections file — proven here by handing it one."""
    broken = tmp_path / "catalog_overrides.json"
    broken.write_text(json.dumps({"canonical_series": {}, "overrides": "not a list"}), encoding="utf-8")
    canon_dir = tmp_path / "platform"
    (canon_dir / "data").mkdir(parents=True)
    (canon_dir / "data" / "series-canon.json").write_text(
        json.dumps(_canon_doc(_entry("X", ["X", "X Alt"]))), encoding="utf-8"
    )
    with pytest.raises(OverridesError):
        sync.main(["--overrides", str(broken), "--platform-dir", str(canon_dir), "--commit"])


def test_a_missing_series_canon_file_is_a_clean_failure(tmp_path, sandbox, capsys):
    empty_platform = tmp_path / "empty-platform"
    (empty_platform / "data").mkdir(parents=True)
    rc = sync.main(["--overrides", str(sandbox), "--platform-dir", str(empty_platform)])
    assert rc == 1
    assert "does not exist" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Against the REAL catalog-platform checkout
# --------------------------------------------------------------------------- #


@requires_platform
def test_the_real_estate_canon_has_no_pending_folds(capsys):
    """This tool was run --commit once as part of building it. If this fails, the
    real scripts/catalog_overrides.json has drifted from the real estate canon —
    re-run `python -m app.tools.sync_series_canon --commit`."""
    rc = sync.main([])
    assert rc == 0
    assert "Nothing to do" in capsys.readouterr().out


@requires_platform
def test_the_three_target_series_all_fold_through_the_live_corrections_layer():
    from app.core import catalog_overrides as co

    co.reload_overrides()
    try:
        assert co.canonicalize_series("Ascend Online [publication order]") == "Ascend Online"
        assert co.canonicalize_series("Harry Potter (Full-Cast Editions)") == "Harry Potter"
        assert co.canonicalize_series("The Fae & Alchemy Series") == "Fae & Alchemy"
    finally:
        co.reload_overrides()
