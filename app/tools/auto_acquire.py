"""
Orchestrate acquisition of newly purchased Audible books via the Dockerized
OpenAudible — the safe way.

Why not just Download_All: the sync pipeline sorts books into author folders,
which breaks OpenAudible's file tracking permanently — it always thinks ~900
books need downloading, and its ignore flags don't persist anywhere we can
write (verified 2026-07-06: books.json edits and UI-set ignores are both
lost). So bulk commands can never be dupe-safe. This orchestrator instead:

  1. starts the container (if needed) and refreshes the library list of
     every connected Audible account (Sync_Quick via commands.json)
  2. runs the top-N purchase audit (audit_new_purchases) against the
     merged list — the ONLY trustworthy gap signal
  3. DOWNLOADS the missing books automatically via audible-cli
     (app/tools/audible_download.py, one registered profile per account),
     converting each to a tagged m4b in runtime/openaudible/books where the
     sync's sort step ingests it. --no-download falls back to report-only;
     --notify pings Discord with what was downloaded (or what failed)
  4. anything downloaded lands in runtime/openaudible/books and is
     ingested automatically by the next sync run's sort step (dupes are
     skipped by filename, so a stray download can't corrupt the library)
  5. optionally stops the container after (--stop-after)

Fully hands-off when there's nothing to download — which the audits show
is the normal state.

Usage:
    python -m app.tools.auto_acquire                 # start, sync, audit, report
    python -m app.tools.auto_acquire --stop-after    # also stop the container
    python -m app.tools.auto_acquire --notify        # Discord ping when books are missing
"""

import argparse
import json
import os
import subprocess
import sys
import time

from app.config import SITE_DIR
from app.tools.audit_new_purchases import run_audit

PROJECT_ROOT = SITE_DIR.parent
RUNTIME = PROJECT_ROOT / "runtime" / "openaudible"
COMPOSE = ["docker", "compose", "-f", str(PROJECT_ROOT / "docker-compose.sync.yml")]


def compose(*args, timeout=180):
    return subprocess.run(COMPOSE + list(args), capture_output=True, text=True, timeout=timeout)


def read_status():
    try:
        return json.loads((RUNTIME / "status.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def container_running():
    r = subprocess.run(["docker", "ps", "--filter", "name=openaudible", "--format", "{{.Names}}"],
                       capture_output=True, text=True, timeout=30)
    return "openaudible" in (r.stdout or "")


def queue_command(cmd):
    """OpenAudible consumes and deletes commands.json (documented queue)."""
    p = RUNTIME / "commands.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps([cmd]), encoding="utf-8")
    tmp.replace(p)


def wait_connected(timeout_s=120):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        s = read_status()
        if s and str(s.get("status", {}).get("Connected", "")).startswith("Yes"):
            return True
        time.sleep(5)
    return False


def wait_idle(timeout_s=900, settle_s=15):
    """Wait until the job queues are empty and stay empty briefly."""
    deadline = time.time() + timeout_s
    idle_since = None
    while time.time() < deadline:
        s = read_status()
        busy = bool(s and any((s.get("queues") or {}).values()))
        if busy:
            idle_since = None
        elif idle_since is None:
            idle_since = time.time()
        elif time.time() - idle_since >= settle_s:
            return True
        time.sleep(5)
    return False


def download_missing(missing, books=None):
    """Auto-download each missing purchase with the owning account's
    profile; converted m4bs land in runtime/openaudible/books for the
    sync to ingest. Returns (downloaded_titles, [(title, error)])."""
    from app.tools.audible_download import download_and_convert, profile_for
    lookup = {}
    for b in books or []:  # audible-cli export rows carry asin + profile + narrator
        title = b.get("title_short") or b.get("title") or ""
        lookup[title] = (b.get("asin"), b.get("profile"), b.get("narrator") or "")
    if not lookup:  # container books.json fallback (user_id -> profile)
        try:
            raw = json.loads((RUNTIME / "books.json").read_text(encoding="utf-8"))
            for b in raw:
                title = b.get("title_short") or b.get("title") or ""
                lookup[title] = (b.get("asin"), profile_for(b.get("user_id")),
                                 b.get("narrator") or "")
        except (OSError, json.JSONDecodeError):
            pass

    downloaded, failed = [], []
    for _date, title in missing:
        asin, profile, narrator = lookup.get(title, (None, None, ""))
        if not asin:
            failed.append((title, "no ASIN in library list"))
            continue
        try:
            dest = download_and_convert(asin, title, profile, narrator=narrator)
            downloaded.append(title)
            print(f"DOWNLOADED: {title} -> {dest.name}")
        except Exception as e:
            failed.append((title, str(e)[:200]))
            print(f"FAILED: {title}: {e}")
    return downloaded, failed


def notify_discord_lines(header, lines):
    webhook = os.getenv("DISCORD_WEBHOOK")
    if not webhook:
        print("[notify] DISCORD_WEBHOOK not set — skipping")
        return
    import urllib.request
    body = {"content": f"**{header}**\n" + "\n".join(lines)}
    req = urllib.request.Request(webhook, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20):
            print("[notify] Discord pinged")
    except Exception as e:
        print(f"[notify] failed: {e}")


def notify_discord(missing):
    notify_discord_lines(
        f"📚 {len(missing)} new Audible purchase(s) need downloading",
        [f"- {d} | {t}" for d, t in missing])


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--top", type=int, default=50, help="recent purchases to audit")
    parser.add_argument("--stop-after", action="store_true", help="stop the container when done")
    parser.add_argument("--notify", action="store_true", help="Discord ping when downloads are needed")
    parser.add_argument("--no-sync", action="store_true", help="skip the library refresh (audit only)")
    parser.add_argument("--no-download", action="store_true", help="report only; don't auto-download")
    args = parser.parse_args()

    # Preferred source: fresh audible-cli exports — no container involved.
    from app.tools.audit_new_purchases import audible_cli_books
    print("[1/3] fetching fresh library lists via audible-cli...")
    books = audible_cli_books()
    used_container = False
    if books:
        print(f"  {len(books)} items across profiles — container not needed")
    else:
        # Fallback: the container's books.json (may lag until OpenAudible flushes)
        used_container = True
        if not container_running():
            print("[1/3] audible-cli unavailable — starting openaudible container...")
            r = compose("up", "-d", "openaudible")
            if r.returncode != 0:
                print(r.stderr.strip() or "docker compose up failed")
                return 2
        if not wait_connected():
            print("container never reached Connected — check http://127.0.0.1:3000")
            return 2
        if not args.no_sync:
            print("[2/3] refreshing library list (Sync_Quick, all connected accounts)...")
            queue_command("Sync_Quick")
            time.sleep(10)
            if not wait_idle():
                print("library sync did not settle — continuing with last known list")

    print("[3/3] auditing purchases vs catalog...")
    missing = run_audit(args.top, books=books or None)

    if args.stop_after and used_container:
        compose("stop", "openaudible")
        print("container stopped (auth persists in runtime/)")

    if missing is None:
        return 2
    if not missing:
        print("\nRESULT: library is current — nothing to download.")
        return 0

    if args.no_download:
        print(f"\nRESULT: {len(missing)} book(s) need downloading (auto-download disabled).")
        if args.notify:
            notify_discord(missing)
        return 1

    downloaded, failed = download_missing(missing, books)
    print(f"\nRESULT: {len(downloaded)} downloaded, {len(failed)} failed — "
          "the sync step ingests downloads into the library.")
    if args.notify:
        lines = ([f"- ⬇️ downloaded: {t}" for t in downloaded]
                 + [f"- ⚠️ FAILED {t}: {err}" for t, err in failed])
        notify_discord_lines(f"📚 Auto-acquire: {len(downloaded)} downloaded, {len(failed)} failed", lines)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
