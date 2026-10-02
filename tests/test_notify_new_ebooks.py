"""The ebook Discord post (2026-10-02) — app/tools/notify_new_ebooks.py.

Owner: "can we have discord also create messages for ebooks like it does
audiobooks? use the exact same message and format and just specify ebook".

It is sent from the pipeline box, not CI, because the ebook list is gated and
CI cannot see it. What these tests pin:
  * the message IS the audiobook message with the noun changed (one builder);
  * the first run baselines and announces nothing;
  * a moved (re-filed) ebook is not a new ebook — identity is title|author;
  * the snapshot advances only on confirmed delivery, so a missing webhook or
    a refused post leaves the books pending rather than lost;
  * without --commit nothing is sent and nothing is written.

No network: send_notification is stubbed.
"""

from __future__ import annotations

import json

import pytest

from app.tools import notify_new_ebooks as notify
from app.tools import send_discord_notification as discord
from app.tools.send_discord_notification import create_embed

SITE = "https://audiobooks.heygabi.ai/"


def _row(title, author="Will Wight", path=None, fmt="epub"):
    return {
        "title": title, "author": author, "format": fmt,
        "path": path or f"{author}/{title}.epub",
        "anchor": f"b-{abs(hash(path or title)) % 10**12:012d}",
        "cover_url": f"https://covers.heygabi.ai/ebooks/{title}.jpg",
    }


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A tmp pipeline box: manifest, catalog, snapshot path, no real webhook."""
    monkeypatch.setattr(notify, "EBOOKS_JSON_PATH", tmp_path / "ebooks.json")
    monkeypatch.setattr(notify, "CATALOG_CSV_PATH", tmp_path / "catalog.csv")
    monkeypatch.setattr(notify, "SNAPSHOT_PATH", tmp_path / "out" / "snap.json")
    (tmp_path / "catalog.csv").write_text("title,author\nA,B\nC,D\n", encoding="utf-8")
    monkeypatch.setenv("DISCORD_WEBHOOK", "https://discord.invalid/hook")
    monkeypatch.setenv("SITE_URL", SITE)

    sent = []
    monkeypatch.setattr(discord, "send_notification", lambda url, embeds: sent.append(embeds) or True)

    def manifest(rows):
        (tmp_path / "ebooks.json").write_text(json.dumps({"ebooks": rows}), encoding="utf-8")

    return manifest, sent


def test_ebook_message_is_the_audiobook_message_with_the_noun_changed():
    payload = {"new_count": 2, "total_count": 1113, "ebook_count": 244,
               "books": [{"title": "Unsouled", "author": "Will Wight", "format": "epub",
                          "cover": "https://covers.heygabi.ai/x.jpg"}]}
    audio = create_embed(payload, SITE)
    ebook = create_embed(payload, SITE, kind="ebook")

    assert audio[0]["title"] == "📚 New Books Added!"
    assert ebook[0]["title"] == "📚 New Ebooks Added!"
    assert ebook[0]["description"].startswith("**2** new ebooks added to the library!")
    assert "*Showing first 1 ebooks*" in ebook[0]["description"]
    # Same stats block, same colours, same card shape.
    assert ebook[0]["fields"][0] == audio[0]["fields"][0]
    assert ebook[0]["color"] == audio[0]["color"] and ebook[1]["color"] == audio[1]["color"]
    assert ebook[1]["title"] == "Unsouled"
    assert ebook[1]["description"] == "**Author:** Will Wight"
    assert ebook[1]["thumbnail"] == {"url": "https://covers.heygabi.ai/x.jpg"}
    assert ebook[1]["fields"] == [{"name": "Format", "value": "📖 Ebook · EPUB", "inline": True}]
    assert "fields" not in audio[1]


def test_first_run_baselines_and_announces_nothing(box):
    manifest, sent = box
    manifest([_row("Unsouled"), _row("Soulsmith")])

    out = notify.run(commit=True)

    assert out.startswith("baseline saved — 2 ebook(s)")
    assert sent == []
    assert notify.load_snapshot() == {"Unsouled|Will Wight", "Soulsmith|Will Wight"}


def test_new_ebook_is_announced_once_and_the_snapshot_advances(box):
    manifest, sent = box
    manifest([_row("Unsouled")])
    notify.run(commit=True)
    manifest([_row("Unsouled"), _row("Soulsmith")])

    assert notify.run(commit=True) == "1 new ebook(s) announced"
    (embeds,) = sent
    assert embeds[0]["title"] == "📚 New Ebooks Added!"
    assert "**2** audiobooks\n**2** ebooks\n**1** new additions" == embeds[0]["fields"][0]["value"]
    assert embeds[0]["fields"][1]["value"] == f"[Browse Library]({SITE}ebooks.html)"
    assert [e["title"] for e in embeds[1:]] == ["Soulsmith"]

    assert notify.run(commit=True) == "nothing new"
    assert len(sent) == 1


def test_a_refiled_ebook_is_not_a_new_ebook(box):
    """The anchor and the path both move when a book is filed into an author
    folder (STEP 1c does exactly that). Identity is title|author."""
    manifest, sent = box
    manifest([_row("Unsouled", path="Unsouled - Will Wight.epub")])
    notify.run(commit=True)
    manifest([_row("Unsouled", path="Will Wight/Unsouled - Will Wight.epub")])

    assert notify.run(commit=True) == "nothing new"
    assert sent == []


def test_a_duplicate_row_is_announced_once(box):
    manifest, sent = box
    manifest([_row("Unsouled")])
    notify.run(commit=True)
    manifest([_row("Unsouled"), _row("Reaper", path="Reaper.epub"), _row("Reaper", path="Will Wight/Reaper.epub")])

    assert notify.run(commit=True) == "1 new ebook(s) announced"
    assert [e["title"] for e in sent[0][1:]] == ["Reaper"]


def test_no_webhook_leaves_the_books_pending(box, monkeypatch):
    manifest, sent = box
    manifest([_row("Unsouled")])
    notify.run(commit=True)
    manifest([_row("Unsouled"), _row("Soulsmith")])
    monkeypatch.delenv("DISCORD_WEBHOOK")

    assert "PENDING" in notify.run(commit=True)
    assert sent == []
    assert notify.load_snapshot() == {"Unsouled|Will Wight"}

    monkeypatch.setenv("DISCORD_WEBHOOK", "https://discord.invalid/hook")
    assert notify.run(commit=True) == "1 new ebook(s) announced"


def test_a_refused_post_does_not_advance_the_snapshot(box, monkeypatch):
    manifest, _ = box
    manifest([_row("Unsouled")])
    notify.run(commit=True)
    manifest([_row("Unsouled"), _row("Soulsmith")])
    monkeypatch.setattr(discord, "send_notification", lambda url, embeds: False)

    assert "NOT announced" in notify.run(commit=True)
    assert notify.load_snapshot() == {"Unsouled|Will Wight"}


def test_report_only_sends_nothing_and_writes_nothing(box):
    manifest, sent = box
    manifest([_row("Unsouled")])

    assert notify.run(commit=False).startswith("baseline saved")
    assert notify.load_snapshot() is None

    notify.run(commit=True)
    manifest([_row("Unsouled"), _row("Soulsmith")])
    assert "would be announced" in notify.run(commit=False)
    assert sent == []
    assert notify.load_snapshot() == {"Unsouled|Will Wight"}


def test_unreadable_manifest_is_skipped_never_baselined(box):
    _, sent = box
    notify.EBOOKS_JSON_PATH.write_text("{not json", encoding="utf-8")

    assert notify.run(commit=True).startswith("skipped")
    assert notify.load_snapshot() is None
    assert sent == []


def test_a_corrupt_snapshot_is_an_error_not_a_silent_rebaseline(box):
    manifest, _ = box
    manifest([_row("Unsouled")])
    notify.SNAPSHOT_PATH.parent.mkdir(parents=True)
    notify.SNAPSHOT_PATH.write_text("{not json", encoding="utf-8")

    with pytest.raises(RuntimeError):
        notify.run(commit=True)
