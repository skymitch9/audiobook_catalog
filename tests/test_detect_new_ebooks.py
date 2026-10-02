"""The ebook Discord post runs in GitHub Actions, exactly like the audiobook one
(owner 2026-10-02: "lets use github actions, i want to match the audiobook
catalog perfectly") — app/tools/detect_new_ebooks.py + send_discord_notification
--ebooks, wired in deploy.yml.

What these tests pin:
  * the message IS the audiobook message with the noun changed (one builder);
  * first run baselines and announces nothing;
  * a re-filed ebook (new path/anchor) is not news; a duplicate row is one book;
  * the TRACKED snapshot holds hashes, never a title (the repo is public);
  * the log prints counts, never a title (public Actions logs);
  * no manifest = skipped, never "zero ebooks", never a baseline;
  * deploy.yml never commits the downloaded manifest or the payload.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.tools import detect_new_ebooks as detect
from app.tools.send_discord_notification import create_embed

SITE = "https://audiobooks.heygabi.ai/"
REPO = Path(__file__).resolve().parents[1]


def _row(title, author="Will Wight", path=None):
    return {"title": title, "author": author, "format": "epub",
            "path": path or f"{author}/{title}.epub",
            "cover_url": f"https://covers.heygabi.ai/ebooks/{title}.jpg"}


@pytest.fixture
def ci(tmp_path, monkeypatch):
    """A tmp checkout: cwd-relative snapshot/output like the real job."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "catalog.csv").write_text("title,author\nA,B\nC,D\n", encoding="utf-8")
    monkeypatch.setenv("EBOOKS_MANIFEST_PATH", str(tmp_path / "ebooks_manifest.json"))
    gh = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh))
    monkeypatch.setattr("sys.argv", ["detect_new_ebooks"])

    def manifest(rows):
        (tmp_path / "ebooks_manifest.json").write_text(json.dumps({"ebooks": rows}), encoding="utf-8")

    def outputs():
        out = {}
        for line in gh.read_text(encoding="utf-8").splitlines():
            k, v = line.split("=", 1)
            out[k] = v
        gh.write_text("", encoding="utf-8")
        return out

    return manifest, outputs, tmp_path


def _run(monkeypatch, *args):
    monkeypatch.setattr("sys.argv", ["detect_new_ebooks", *args])
    assert detect.main() == 0


def test_ebook_message_is_the_audiobook_message_with_the_noun_changed():
    payload = {"new_count": 2, "total_count": 1113, "ebook_count": 224,
               "books": [{"title": "Unsouled", "author": "Will Wight", "format": "epub",
                          "cover": "https://covers.heygabi.ai/x.jpg"}]}
    audio = create_embed(payload, SITE)
    ebook = create_embed(payload, SITE + "ebooks.html", kind="ebook")

    assert audio[0]["title"] == "📚 New Books Added!"
    assert ebook[0]["title"] == "📚 New Ebooks Added!"
    assert ebook[0]["description"].startswith("**2** new ebooks added to the library!")
    assert ebook[0]["fields"][0] == audio[0]["fields"][0]   # same counters block
    assert ebook[0]["color"] == audio[0]["color"] and ebook[1]["color"] == audio[1]["color"]
    assert ebook[1]["description"] == "**Author:** Will Wight"
    assert ebook[1]["thumbnail"] == {"url": "https://covers.heygabi.ai/x.jpg"}
    assert ebook[1]["fields"] == [{"name": "Format", "value": "📖 Ebook · EPUB", "inline": True}]


def test_first_run_baselines_and_announces_nothing(ci, monkeypatch):
    manifest, outputs, root = ci
    manifest([_row("Unsouled"), _row("Soulsmith")])

    _run(monkeypatch)

    assert outputs() == {"has_new_ebooks": "false", "new_ebook_count": "0", "baselined": "true"}
    assert len(detect.load_snapshot()) == 2
    assert not (root / "new_ebooks.json").exists()


def test_new_ebook_is_detected_and_snapshot_advances_only_on_request(ci, monkeypatch):
    manifest, outputs, root = ci
    manifest([_row("Unsouled")])
    _run(monkeypatch)
    outputs()
    manifest([_row("Unsouled"), _row("Soulsmith")])

    _run(monkeypatch)
    assert outputs() == {"has_new_ebooks": "true", "new_ebook_count": "1"}
    payload = json.loads((root / "new_ebooks.json").read_text(encoding="utf-8"))
    assert payload["new_count"] == 1
    assert payload["total_count"] == 2       # audiobooks: rows of catalog.csv
    assert payload["ebook_count"] == 2
    assert [b["title"] for b in payload["books"]] == ["Soulsmith"]
    assert len(detect.load_snapshot()) == 1  # not advanced until Discord confirmed

    _run(monkeypatch, "--update-snapshot")
    outputs()
    _run(monkeypatch)
    assert outputs()["has_new_ebooks"] == "false"


def test_a_refiled_ebook_is_not_news_and_a_duplicate_is_one_book(ci, monkeypatch):
    manifest, outputs, root = ci
    manifest([_row("Unsouled", path="Unsouled - Will Wight.epub")])
    _run(monkeypatch)
    outputs()
    manifest([
        _row("Unsouled", path="Will Wight/Unsouled - Will Wight.epub"),
        _row("Reaper", path="Reaper - Will Wight.epub"),
        _row("Reaper", path="Will Wight/Reaper - Will Wight.epub"),
    ])

    _run(monkeypatch)
    payload = json.loads((root / "new_ebooks.json").read_text(encoding="utf-8"))
    assert [b["title"] for b in payload["books"]] == ["Reaper"]


def test_tracked_snapshot_holds_hashes_never_titles(ci, monkeypatch):
    manifest, _, root = ci
    manifest([_row("A Very Private Title", author="Somebody")])
    _run(monkeypatch)

    text = (root / "last_ebook_snapshot.json").read_text(encoding="utf-8")
    assert "Private" not in text and "Somebody" not in text
    assert all(re.fullmatch(r"[0-9a-f]{64}", k) for k in json.loads(text)["ebook_keys"])


def test_log_prints_counts_never_titles(ci, monkeypatch, capsys):
    manifest, _, _ = ci
    manifest([_row("Unsouled")])
    _run(monkeypatch)
    manifest([_row("Unsouled"), _row("A Very Private Title", author="Somebody")])
    _run(monkeypatch)

    out = capsys.readouterr().out
    assert "Private" not in out and "Somebody" not in out and "Unsouled" not in out
    assert "Found 1 new ebooks (total: 2)" in out


def test_no_manifest_is_skipped_never_baselined(ci, monkeypatch):
    _, outputs, root = ci
    _run(monkeypatch)

    assert outputs() == {"has_new_ebooks": "false", "new_ebook_count": "0"}
    assert detect.load_snapshot() is None
    assert not (root / "new_ebooks.json").exists()


def test_deploy_workflow_never_commits_the_manifest_or_payload():
    wf = (REPO / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    added = re.findall(r"git add ([^\n]+)", wf)
    assert added, "deploy.yml stages nothing — the snapshot steps are missing"
    for line in added:
        assert "ebooks_manifest" not in line and "new_ebooks" not in line and "ebooks.json" not in line
    assert "git add last_ebook_snapshot.json" in wf
    assert "rm -f ebooks_manifest.json new_ebooks.json" in wf
    assert "upload-artifact" not in wf
