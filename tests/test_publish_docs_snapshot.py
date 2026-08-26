"""scripts/publish_docs_snapshot.py — the GABI docs snapshot's five gates.

⚠️ THIS FILE GUARDS A SECURITY SPINE, not a formatter. The design
(catalog-platform docs/info/gabi-docs-assistant-design.md §3) trades a
per-file allowlist away — because one fails OPEN on omission, and a
silently-stale corpus is the whole feature lost — and pays for that trade with
a denylist plus a fail-closed content scanner. If the tests below stop passing,
the trade is no longer paid for.

The central test is `test_scanner_catches_a_planted_credential`: it writes a
fake credential into a scratch docs file, runs the publisher over that scratch
tree, and asserts the run REFUSES. Its partner,
`test_refusal_does_not_strip_the_offending_file`, asserts the opposite of what
a "helpful" fix would do — the file must still be in the bundle, whole.
Silent stripping is a named estate defect ("a validator that silently strips
instead of rejecting"), and here it would be worse than the leak it pretends to
fix: a stripped doc is a doc GABI answers from, missing the line that mattered.
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import publish_docs_snapshot as pds  # noqa: E402


# ---------------------------------------------------------------------------
# A scratch three-repo docs tree, so nothing here reads the real corpus.
# ---------------------------------------------------------------------------

def make_tree(tmp_path: Path, files: dict[str, str]) -> list[tuple[str, Path]]:
    """`files` keys are '<repo>/<path under that repo's docs dir>'."""
    repos = ["catalog-platform", "library_catalog", "audiobook_catalog"]
    pairs = []
    for repo in repos:
        docs = tmp_path / repo / "docs"
        docs.mkdir(parents=True, exist_ok=True)
        pairs.append((repo, docs))
    for key, body in files.items():
        repo, rel = key.split("/", 1)
        target = tmp_path / repo / "docs" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return pairs


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    def _build(files: dict[str, str]):
        pairs = make_tree(tmp_path, files)
        monkeypatch.setattr(pds, "REPOS", pairs)
        return pairs
    return _build


# ---------------------------------------------------------------------------
# Layer 3 — the denylist
# ---------------------------------------------------------------------------

def test_credentials_md_is_first_and_permanent_on_the_denylist():
    # ⚠️ Pinned by INDEX, not membership. CREDENTIALS.md's whole job is to be
    # the one place credential locations are written down; nothing that leaves
    # this machine carries it. If this assertion is what is in your way, the
    # change you are making is a decision to publish the estate's credential
    # index, not a refactor.
    assert pds.DENYLIST[0] == "audiobook_catalog/access/CREDENTIALS.md"


def test_credentials_md_is_excluded_by_the_denylist(scratch):
    scratch({
        "audiobook_catalog/access/CREDENTIALS.md": "# Credentials\n\nsomething\n",
        "audiobook_catalog/access/OTHER.md": "# Other\n\nfine\n",
    })
    built = pds.build_snapshot("shadow")
    paths = [f["path"] for f in built["bundle"]["files"]]
    assert "audiobook_catalog/docs/access/CREDENTIALS.md" not in paths
    assert "audiobook_catalog/docs/access/OTHER.md" in paths
    assert built["denied"] == ["audiobook_catalog/access/CREDENTIALS.md"]
    # And the whole bundle must not carry a byte of it.
    assert "# Credentials" not in json.dumps(built["bundle"])


# ---------------------------------------------------------------------------
# Layer 2 — .md only, BY CONSTRUCTION
# ---------------------------------------------------------------------------

def test_non_markdown_is_excluded_by_construction(scratch):
    # The seven real non-.md files in the estate's docs trees include two that
    # carry live people's permission data (drive-exceptions.json,
    # permission-snapshot-*.json). Excluding them is this rule's FIRST job.
    scratch({
        "catalog-platform/keep.md": "# Keep\n\ntext\n",
        "catalog-platform/deploys.log": "2026-08-18 deployed\n",
        "audiobook_catalog/access/permission-snapshot-2026-08-17.json": '{"person":"someone"}',
        "audiobook_catalog/access/SHELF_SERVER.fragment.html": "<p>runbook</p>",
        "library_catalog/DRIVE_AUDIT_REPORT.csv": "a,b\n1,2\n",
    })
    built = pds.build_snapshot("shadow")
    paths = [f["path"] for f in built["bundle"]["files"]]
    assert paths == ["catalog-platform/docs/keep.md"]
    assert set(built["non_md"]) == {
        "catalog-platform/deploys.log",
        "audiobook_catalog/access/permission-snapshot-2026-08-17.json",
        "audiobook_catalog/access/SHELF_SERVER.fragment.html",
        "library_catalog/DRIVE_AUDIT_REPORT.csv",
    }
    blob = json.dumps(built["bundle"])
    assert "runbook" not in blob and '"person"' not in blob


# ---------------------------------------------------------------------------
# Layer 4 — the fail-closed content scanner. THE CENTRAL TEST.
# ---------------------------------------------------------------------------

# Assembled at runtime so this literal is not itself a credential-shaped string
# sitting in a tracked file — the scanner would flag this very test file if the
# docs tree ever contained it, which is the joke and also the reason.
FAKE_KEY = "sk-ant-" + "api03" + "-" + ("Zq7" * 12) + "AA"


def test_scanner_catches_a_planted_credential(scratch, capsys):
    scratch({
        "catalog-platform/info/harmless.md": "# Harmless\n\nNothing to see.\n",
        "catalog-platform/access/leaky.md": (
            "# Leaky\n\n## The key\n\nSomebody pasted this in:\n\n"
            f"    ANTHROPIC_API_KEY={FAKE_KEY}\n"
        ),
    })

    # SHADOW (the shipped default): logs the finding, publishes anyway.
    assert pds.main(["--dry-run", "--scanner", "shadow"]) == 0
    shadow_out = capsys.readouterr().out
    assert "catalog-platform/docs/access/leaky.md" in shadow_out
    assert "anthropic_key" in shadow_out

    # ENFORCE: refuses, non-zero, nothing uploaded.
    assert pds.main(["--dry-run", "--scanner", "enforce"]) == 1
    enforce_out = capsys.readouterr().out
    assert "REFUSED" in enforce_out


def test_scanner_findings_never_carry_the_matched_text(scratch):
    scratch({"catalog-platform/access/leaky.md": f"# L\n\nkey: {FAKE_KEY}\n"})
    built = pds.build_snapshot("enforce")
    findings = built["findings"]
    assert findings, "the planted credential was not detected at all"
    # ⚠️ A findings log that quotes what it found has published the secret to a
    # SECOND place. The receipt goes into the bucket and the console line goes
    # into the pipeline log; neither may carry the value.
    assert FAKE_KEY not in json.dumps(findings)
    assert FAKE_KEY not in json.dumps(built["receipt"]["scanner"])
    for f in findings:
        assert set(f.keys()) == {"path", "line", "rule"}


def test_refusal_does_not_strip_the_offending_file(scratch):
    # ⚠️ THE ANTI-STRIPPING ASSERTION. The publisher REFUSES; it must never
    # "helpfully" drop or redact the file and publish the rest. A stripped doc
    # is a doc GABI answers from, missing the line that mattered.
    scratch({"catalog-platform/access/leaky.md": f"# L\n\n## K\n\nkey: {FAKE_KEY}\n"})
    built = pds.build_snapshot("enforce")
    paths = [f["path"] for f in built["bundle"]["files"]]
    assert "catalog-platform/docs/access/leaky.md" in paths
    assert FAKE_KEY in json.dumps(built["bundle"]), "the scanner stripped instead of refusing"


def test_emergency_hatch_is_honoured_only_in_enforce(scratch, monkeypatch, capsys):
    scratch({"catalog-platform/access/leaky.md": f"# L\n\nkey: {FAKE_KEY}\n"})
    monkeypatch.setenv(pds.ALLOW_SUSPECT_ENV, "1")
    assert pds.main(["--dry-run", "--scanner", "enforce"]) == 0
    assert pds.ALLOW_SUSPECT_ENV in capsys.readouterr().out


@pytest.mark.parametrize("planted", [
    "-----BEGIN RSA PRIVATE KEY-----",
    "ghp_" + "a1B2c3D4e5" * 4,
    "AKIA" + "ABCDEFGHIJKLMNOP",
    "AIza" + "Sy" + "B" * 33,
])
def test_scanner_catches_each_provider_shape(planted):
    findings = pds.scan_text(f"# D\n\nvalue {planted}\n", "x/y.md")
    assert findings, f"undetected: {planted[:8]}…"


@pytest.mark.parametrize("benign", [
    # ⚠️ EVERY LINE HERE IS A REAL SHAPE FROM THE REAL CORPUS. The five
    # false positives the first shadow run produced (measured 2026-08-18) were
    # all of the first two shapes; the rest are what the estate's access docs
    # are FOR — secret NAMES and where they live, never values.
    "[MDN](https://developer.mozilla.org/en-US/docs/Web/API/HTMLMediaElement/playbackRate)",
    "| List her secrets | `npm run secret:list:friend` |",
    "`wrangler secret put ESTATE_APP_TOKEN_LIBRARY` (pipe the value, never paste it)",
    "The commit is 3b9c6b3f4a2e1d0c9b8a7f6e5d4c3b2a19087654 on main.",
    "sha256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "token: <your-token-here>",
    "PIPELINE_TRIGGER_TOKEN=$env:PIPELINE_TRIGGER_TOKEN",
    "password: changeme",
    # ⚠️ KI-6, closed 2026-08-26. The exact literal that refused the live
    # corpus for a day: an npm SCRIPT NAME with a bracketed optional segment,
    # read as `secret:` + the value `list[:friend]`. It was 4 of the 4 live
    # findings, across three files, and no secret was present in any of them.
    "| `npm run secret:list[:friend]` | List secret NAMES | Either |",
    "`secret:list[:friend]` in `library_catalog/docs/access/secrets.md` as",
    "npm run secret:friend -- NAME",
])
def test_scanner_leaves_the_estate_own_documentation_alone(benign):
    assert pds.scan_text(f"# D\n\n{benign}\n", "x/y.md") == []


@pytest.mark.parametrize("planted", [
    # ⚠️ THE OTHER HALF OF KI-6. Loosening the assignment rule is only safe if
    # the shapes it exists for are still caught, so these are pinned in the
    # same breath as the exemptions above. A future session that widens the
    # rule again has to keep both lists green.
    "API_KEY=0f1e2d3c4b5a69788796a5b4c3d2e1f0",
    "token: \"aB3xK9mQ7zP2wR5tY8uL1nD4vC6\"",
    "client_secret=Zq7Zq7Zq7Zq7Zq7Zq7Zq7Zq7",
])
def test_the_assignment_rule_still_catches_a_real_looking_value(planted):
    findings = pds.scan_text(f"# D\n\n{planted}\n", "x/y.md")
    assert [f["rule"] for f in findings] == ["assigned_secret_value"]


def test_an_un_backticked_paste_is_still_scanned():
    # ⚠️ The inline-code exemption is for a command a doc NAMES, not a licence
    # to hide a value. A bare `export …=<key>` line is exactly how a real paste
    # arrives, so it must still refuse.
    assert pds.scan_text("# D\n\nexport ACCESS_KEY=0f1e2d3c4b5a69788796a5b4c3d2e1f0\n",
                         "x/y.md")


# ---------------------------------------------------------------------------
# Layer 1 — the directory allowlist, and its fail-closed behaviour
# ---------------------------------------------------------------------------

def test_a_missing_repo_tree_refuses_rather_than_publishing_a_partial_corpus(tmp_path, monkeypatch):
    # ⚠️ The failure design §2.2 calls the worst possible one for a docs
    # assistant: two of three trees published, reported as success. GABI would
    # answer "I don't have anything on that" about a third of the estate while
    # every dashboard read green.
    pairs = make_tree(tmp_path, {"catalog-platform/a.md": "# A\n"})
    pairs.append(("nonexistent_repo", tmp_path / "nope" / "docs"))
    monkeypatch.setattr(pds, "REPOS", pairs)
    with pytest.raises(SystemExit) as exc:
        pds.build_snapshot("shadow")
    assert "nonexistent_repo" in str(exc.value)


def test_the_allowlist_is_exactly_three_repos():
    assert [r for r, _ in pds.REPOS] == ["catalog-platform", "library_catalog", "audiobook_catalog"]
    assert pds.ALLOWED_SUFFIXES == {".md"}


# ---------------------------------------------------------------------------
# Sectioning — the 8 KB ceiling the read route promises
# ---------------------------------------------------------------------------

def test_no_section_exceeds_the_ceiling_however_big_the_file():
    # A 200 KB H2 with no H3 inside it is the DONE.md shape: neither the H2 nor
    # the H3 cut can help, so the hard split has to. The route promises a
    # bounded section; the PUBLISHER is what keeps that promise, so nothing has
    # to be truncated at serve time.
    body = "# Archive\n\n## One giant section\n\n" + ("filler line of text\n" * 12000)
    sections = pds.split_sections(body, "Archive")
    assert len(sections) > 1
    for s in sections:
        assert s["bytes"] <= pds.SECTION_MAX_BYTES, s["heading"]
    assert any("cont." in str(s["heading"]) for s in sections)


def test_sections_reconstruct_the_file_exactly():
    # No offsets, no whole-file copy: concatenation IS the file. If this breaks,
    # the read route starts answering with text that begins mid-word.
    body = "# T\n\nlead-in\n\n## A\n\naaa\n\n## B\n\nbbb\n"
    sections = pds.split_sections(body, "T")
    assert "\n".join(s["text"] for s in sections) == body.rstrip("\n")


def test_the_preamble_before_the_first_h2_is_its_own_section():
    # An estate doc's header block (audience, status, last-verified) lives
    # there, and is often the single most useful thing in the file.
    sections = pds.split_sections("# Title\n\n> Audience: Claude.\n\n## First\n\nx\n", "Title")
    assert sections[0]["heading"] == "Title"
    assert "Audience" in sections[0]["text"]


def test_headings_inside_fenced_code_do_not_cut_a_section():
    body = "# T\n\n## Real\n\n```sh\n## not a heading\n```\n\ntail\n"
    sections = pds.split_sections(body, "T")
    assert [s["heading"] for s in sections] == ["T", "Real"]


# ---------------------------------------------------------------------------
# Bundle + receipt shape
# ---------------------------------------------------------------------------

def test_gzip_is_deterministic_so_the_sha_skip_works(scratch):
    scratch({"catalog-platform/a.md": "# A\n\n## S\n\ntext\n"})
    built = pds.build_snapshot("shadow")
    bundle = dict(built["bundle"])
    bundle["generated_at"] = "2026-08-18T00:00:00Z"   # the only per-run field
    first = pds.gzip_bytes(bundle)
    second = pds.gzip_bytes(bundle)
    assert first == second
    assert json.loads(gzip.decompress(first))["files"][0]["path"] == "catalog-platform/docs/a.md"


def test_the_skip_digest_ignores_the_timestamp_but_not_the_text(scratch):
    # ⚠️ REGRESSION GUARD for a bug the first real run exposed (2026-08-18):
    # the skip key hashed the GZIPPED BUNDLE, which carries `generated_at`, so
    # it changed every invocation and the 8-hourly pipeline step would have
    # re-PUT 1.2 MB forever while printing "no change in the included set"
    # right beside it. "Idempotent by content" was true of the code and false
    # of the behaviour.
    scratch({"catalog-platform/a.md": "# A\n\ntext\n"})
    a = pds.build_snapshot("shadow")["bundle"]
    b = json.loads(json.dumps(a))
    b["generated_at"] = "1999-01-01T00:00:00Z"
    assert pds.content_sha(a) == pds.content_sha(b)

    c = json.loads(json.dumps(a))
    c["files"][0]["sections"][0]["text"] += " and one more word"
    assert pds.content_sha(a) != pds.content_sha(c)


def test_receipt_names_every_included_file_with_bytes_and_sha(scratch):
    scratch({"catalog-platform/a.md": "# A\n", "library_catalog/b.md": "# B\n"})
    receipt = pds.build_snapshot("shadow")["receipt"]
    assert receipt["totals"]["files"] == 2
    for entry in receipt["files"]:
        assert entry["bytes"] > 0
        assert len(entry["sha256"]) == 64
    # The receipt is what makes a DIRECTORY allowlist auditable — it must name
    # the denylist it applied, so a removed entry is visible in the audit trail
    # rather than only in a diff nobody reads.
    assert receipt["denylist"] == pds.DENYLIST


def test_receipt_diff_reports_drift_in_the_included_set():
    receipt = {"files": [{"path": "a"}, {"path": "b"}]}
    added, removed = pds.receipt_diff({"paths": ["b", "c"]}, receipt)
    assert added == ["a"]
    assert removed == ["c"]


# ---------------------------------------------------------------------------
# The growth tripwire
# ---------------------------------------------------------------------------

def test_growth_tripwire_thresholds_are_exactly_10mb_warn_and_25mb_refuse():
    # Spelled as raw byte counts so an "equivalent" refactor cannot move them.
    assert pds.CORPUS_WARN_BYTES == 10_485_760
    assert pds.CORPUS_REFUSE_BYTES == 26_214_400
    assert pds.SECTION_MAX_BYTES == 8192


def test_corpus_over_the_ceiling_refuses(scratch, monkeypatch, capsys):
    scratch({"catalog-platform/a.md": "# A\n\n" + ("x" * 5000) + "\n"})
    monkeypatch.setattr(pds, "CORPUS_REFUSE_BYTES", 1000)
    assert pds.main(["--dry-run"]) == 1
    assert "5.4" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The real corpus — a measured guard, skipped where the trees are absent.
# ---------------------------------------------------------------------------

def _real_trees_present() -> bool:
    return all(docs.is_dir() for _, docs in pds.REPOS)


@pytest.mark.skipif(not _real_trees_present(),
                    reason="the three docs trees only coexist on the owner's machine "
                           "(audiobook_catalog/docs is gitignored) — see design §2.2")
def test_the_live_corpus_is_clean_and_under_the_warning_line():
    built = pds.build_snapshot("shadow")
    assert built["findings"] == [], (
        "the scanner found a suspected credential in the live corpus — investigate "
        "before publishing; do NOT relax a rule to make this pass"
    )
    assert built["total_bytes"] < pds.CORPUS_WARN_BYTES
    paths = [f["path"] for f in built["bundle"]["files"]]
    assert "audiobook_catalog/docs/access/CREDENTIALS.md" not in paths
    assert any(p.startswith("catalog-platform/") for p in paths)
    assert any(p.startswith("library_catalog/") for p in paths)
    assert any(p.startswith("audiobook_catalog/") for p in paths)
