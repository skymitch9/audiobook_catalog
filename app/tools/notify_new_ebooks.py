#!/usr/bin/env python3
"""
Announce new EBOOKS on Discord — the ebook twin of detect_new_books.py +
send_discord_notification.py, in one module, run on the pipeline box.

Why it does not run in CI like the audiobook post. That post is made by
deploy.yml from site/catalog.csv, which is tracked. The ebook list
(site/ebooks.json) is gitignored on purpose — the shelf is permission-gated
and this repo is public — so CI can see the ebook COUNT and nothing else. The
only place the list exists is this machine, so the post is made here, right
after STEP 5.8 publishes the manifest: the moment the books are actually
readable.

The message is send_discord_notification.create_embed(kind="ebook") — the same
builder as the audiobook post, so the two cannot drift.

State: output_files/ebook_announce_snapshot.json (untracked, like every other
path-or-title record of the ebook shelf). Same rules as the audiobook snapshot:
  * no snapshot yet -> save a baseline and post NOTHING (what is already on
    the shelf is not news);
  * the snapshot advances only after Discord confirms delivery, so a failed or
    unconfigured post is retried next run rather than lost.

DISCORD_WEBHOOK comes from the environment / .env on this machine. Unset is
not an error: the new ebooks stay pending and are announced, together, by the
first run after it is set.

    python -m app.tools.notify_new_ebooks            # report only, sends nothing
    python -m app.tools.notify_new_ebooks --commit   # post + advance the snapshot
"""
import csv
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EBOOKS_JSON_PATH = PROJECT_ROOT / "site" / "ebooks.json"
CATALOG_CSV_PATH = PROJECT_ROOT / "site" / "catalog.csv"
SNAPSHOT_PATH = PROJECT_ROOT / "output_files" / "ebook_announce_snapshot.json"
EBOOKS_PAGE = "ebooks.html"


def ebook_id(row: dict) -> str:
    """Identity of an ebook for "have we announced this?".

    ⚠️ Title|author, NOT the manifest's `anchor` or `path`: both move when a
    book is re-filed into an author folder, and a moved book is not a new one.
    It is the same key detect_new_books.py uses for audiobooks, and it also
    folds a re-downloaded duplicate into the copy already announced."""
    return f"{row.get('title', '')}|{row.get('author', '')}"


def load_ebooks() -> list[dict] | None:
    """The manifest's rows, or None when it cannot be read (NOT the same as an
    empty shelf — the caller must not baseline on it)."""
    try:
        with open(EBOOKS_JSON_PATH, "r", encoding="utf-8") as f:
            rows = json.load(f).get("ebooks")
        return rows if isinstance(rows, list) else None
    except Exception:
        return None


def load_snapshot() -> set | None:
    """Announced ebook ids, or None when no snapshot exists yet."""
    try:
        with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f).get("ebook_ids", []))
    except FileNotFoundError:
        return None
    except Exception as e:
        # A corrupt snapshot must not be read as "first run": that would
        # silently re-baseline and swallow every pending announcement.
        raise RuntimeError(f"unreadable {SNAPSHOT_PATH.name}: {e}") from e


def save_snapshot(ids: set) -> None:
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SNAPSHOT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"ebook_ids": sorted(ids)}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, SNAPSHOT_PATH)


def audiobook_count() -> int:
    try:
        with open(CATALOG_CSV_PATH, "r", encoding="utf-8") as f:
            return sum(1 for _ in csv.DictReader(f))
    except Exception:
        return 0


def build_payload(rows: list[dict], announced: set) -> dict:
    """The new_books.json-shaped dict create_embed() takes, for the ebooks not
    yet announced. One entry per distinct ebook, in manifest order."""
    seen: set[str] = set()
    new: list[dict] = []
    for row in rows:
        key = ebook_id(row)
        if key in announced or key in seen:
            continue
        seen.add(key)
        new.append({
            "title": row.get("title", ""),
            "author": row.get("author", ""),
            "cover": row.get("cover_url") or "",
            "format": row.get("format", ""),
        })
    return {
        "new_count": len(new),
        "total_count": audiobook_count(),
        # len(rows), not distinct ids: it is the same number STEP 1b writes to
        # site/ebooks_status.json, which the audiobook post prints. One count.
        "ebook_count": len(rows),
        "books": new[:10],
    }


def run(commit: bool = False) -> str:
    """One pass. Returns a one-line outcome for the pipeline log; never raises
    for an ordinary condition (no manifest, no webhook, nothing new)."""
    import app.config  # noqa: F401  — loads .env, where DISCORD_WEBHOOK lives

    rows = load_ebooks()
    if rows is None:
        return "skipped — site/ebooks.json could not be read"

    current = {ebook_id(r) for r in rows}
    announced = load_snapshot()
    if announced is None:
        if commit:
            save_snapshot(current)
        return f"baseline saved — {len(current)} ebook(s) already on the shelf, nothing announced"

    payload = build_payload(rows, announced)
    if payload["new_count"] == 0:
        return "nothing new"

    if not commit:
        return f"{payload['new_count']} new ebook(s) would be announced (report only)"

    webhook = os.environ.get("DISCORD_WEBHOOK")
    if not webhook:
        return (f"{payload['new_count']} new ebook(s) PENDING — DISCORD_WEBHOOK is not set "
                "on this machine; they will be announced once it is")

    from app.tools.send_discord_notification import create_embed, send_notification

    site_url = os.environ.get("SITE_URL", "https://audiobooks.heygabi.ai/")
    if not site_url.endswith("/"):
        site_url += "/"
    embeds = create_embed(payload, site_url + EBOOKS_PAGE, kind="ebook")
    if not send_notification(webhook, embeds):
        return f"{payload['new_count']} new ebook(s) NOT announced — Discord refused; will retry next run"

    save_snapshot(announced | current)
    return f"{payload['new_count']} new ebook(s) announced"


def main() -> int:
    print(f"[ebooks-notify] {run(commit='--commit' in sys.argv)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
