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
  3. reports exactly which books need downloading; with --notify it pings
     the Discord webhook so a human (or Claude driving the container UI)
     downloads those specific books — a few clicks, a few times a month
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
from pathlib import Path

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


def notify_discord(missing):
    webhook = os.getenv("DISCORD_WEBHOOK")
    if not webhook:
        print("[notify] DISCORD_WEBHOOK not set — skipping")
        return
    import urllib.request
    lines = "\n".join(f"- {d} | {t}" for d, t in missing)
    body = {"content": f"📚 **{len(missing)} new Audible purchase(s) need downloading** "
                       f"(container: http://127.0.0.1:3000)\n{lines}"}
    req = urllib.request.Request(webhook, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20):
            print("[notify] Discord pinged")
    except Exception as e:
        print(f"[notify] failed: {e}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--top", type=int, default=50, help="recent purchases to audit")
    parser.add_argument("--stop-after", action="store_true", help="stop the container when done")
    parser.add_argument("--notify", action="store_true", help="Discord ping when downloads are needed")
    parser.add_argument("--no-sync", action="store_true", help="skip the library refresh (audit only)")
    args = parser.parse_args()

    started_here = False
    if not container_running():
        print("[1/3] starting openaudible container...")
        r = compose("up", "-d", "openaudible")
        if r.returncode != 0:
            print(r.stderr.strip() or "docker compose up failed")
            return 2
        started_here = True
    else:
        print("[1/3] container already running")

    if not wait_connected():
        print("container never reached Connected — check http://127.0.0.1:3000")
        return 2

    if not args.no_sync:
        print("[2/3] refreshing library list (Sync_Quick, all connected accounts)...")
        queue_command("Sync_Quick")
        time.sleep(10)
        if not wait_idle():
            print("library sync did not settle — continuing with last known list")
    else:
        print("[2/3] skipped library refresh (--no-sync)")

    print("[3/3] auditing purchases vs catalog...")
    missing = run_audit(args.top)

    if args.stop_after and (started_here or True):
        compose("stop", "openaudible")
        print("container stopped (auth persists in runtime/)")

    if missing is None:
        return 2
    if not missing:
        print("\nRESULT: library is current — nothing to download.")
        return 0
    print(f"\nRESULT: {len(missing)} book(s) to download — open http://127.0.0.1:3000,")
    print("search each title, select it, Actions > Download (autoConvert makes the")
    print("m4b). The next sync run ingests them into the library automatically.")
    if args.notify:
        notify_discord(missing)
    return 1


if __name__ == "__main__":
    sys.exit(main())
