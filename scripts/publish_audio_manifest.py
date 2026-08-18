#!/usr/bin/env python3
"""Publish the audio manifest to the PRIVATE R2 bucket the player reads.

AUDIO PLAYER PHASE 1 (2026-08-18) — the half that lets the Worker answer
"is this book streamable?" at all. Sibling of `publish_ebooks_manifest.py`,
whose wrangler idiom and idempotence contract this copies.

WHY IT EXISTS
-------------
Phase 0b uploads audiobooks into `estate-audio` and records what it uploaded in
`site/audio_manifest.json`. That file is ⚠️ **GITIGNORED**, because it lists the
household's 630 GB library file by file and this repo is PUBLIC — the same
surface `site/ebooks.json` was pulled out of git to close. So it can never
reach the Worker through the repo or through the Pages deployment. This script
is the only way out: one PUT into a private bucket, read by

    catalog-platform apps/audiobook-worker
      GET  /api/audio/status          (a five-field projection — never `path`)
      GET|HEAD /api/audio/:anchor/file (the byte stream; anchor -> path -> key)

both behind a verified Firebase token AND the estate's `vis_ebooks` grant.

⚠️ THE BUCKET IS `ebooks-gated`, AND THAT IS A DECISION, NOT A TYPO
--------------------------------------------------------------------
The audio manifest sits beside the ebook one in the SAME private bucket, under
a second key. It reads like a mistake, so:

  * Owner decision 1 (audio-player-design.md §12, 2026-08-17) FUSED the two
    grants — *"MIRROR EBOOK if they can read an ebook they can listen to an
    audio."* One grant means "may consume the estate's book files". So this is
    not the ebook bucket holding an audio file; it is **the one gated-manifest
    bucket for the one book-files grant**.
  * `ebooks-gated` already has a verified-private posture (no r2.dev URL, no
    custom domain) and an existing Worker binding. A fourth bucket would be a
    fourth thing whose privacy somebody has to keep re-verifying, buying a
    separation nothing in this design needs.
  * ⚠️ The BYTES keep their own bucket (`estate-audio`). That is the separation
    that carries a security property, and it is untouched.

⚠️ BOTH HALVES OF THE CONTRACT ARE HERE AND THERE
--------------------------------------------------
`BUCKET` + `KEY` below must match apps/audiobook-worker's `EBOOKS_GATED`
binding and `src/audio-manifest.ts`'s `AUDIO_MANIFEST_KEY`. Changing either
without the other gives listeners a 503 `manifest_absent` that looks exactly
like a stalled pipeline.

AN ABSENT RECORD PUBLISHES AN EMPTY MANIFEST, ON PURPOSE
---------------------------------------------------------
Before anybody presses "request it" there is no `site/audio_manifest.json` at
all. Publishing nothing would leave the site's audio row on a 503 error path
forever; publishing an EMPTY manifest gets it a clean 200 with zero books, and
the site correctly offers "not streamable yet — request it" on everything. An
empty answer is the design working, not a fault, and this makes the wire say so.

CONTRACT
--------
* **Idempotent by CONTENT.** The digest is taken over the `files` map only, not
  over the serialised document — the document carries a `generated` timestamp
  that changes on every call, so hashing the whole thing would re-upload
  forever. State lives in `scripts/.audio_published.json` (gitignored).
* **Never partial.** A payload that is not an audio manifest is refused and the
  previously published one still stands — listeners see a slightly stale list
  rather than none.
* **Auth comes from wrangler**, which owns its own OAuth token. ⚠️ Note this is
  a DIFFERENT credential from the R2 API token `upload_audio_r2.py` needs: this
  script works today even where that one is blocked.
* Soft by construction when called from the pipeline: `publish_if_changed()`
  returns a bool and never raises.

USAGE
-----
    python -m scripts.publish_audio_manifest             # publish if changed
    python -m scripts.publish_audio_manifest --dry-run   # say what would happen
    python -m scripts.publish_audio_manifest --force     # re-upload regardless

Exit 0 = the bucket holds the current manifest. Exit 1 = it does not.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from scripts import upload_audio_r2 as up

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = PROJECT_ROOT / "scripts" / ".audio_published.json"

# ⚠️ One contract, two repos — see the module docstring. The Worker's binding is
# EBOOKS_GATED -> ebooks-gated; its key constant is AUDIO_MANIFEST_KEY.
BUCKET = os.getenv("EBOOKS_GATED_BUCKET", "ebooks-gated")
KEY = "audio_manifest.json"


# ---------------------------------------------------------------------------
# wrangler (the publish_ebooks_manifest.py idiom, verbatim — one way to call it)
# ---------------------------------------------------------------------------
def _wrangler_cmd() -> List[str]:
    local = PROJECT_ROOT / "node_modules" / ".bin" / ("wrangler.cmd" if os.name == "nt" else "wrangler")
    if local.exists():
        return [str(local)]
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        raise SystemExit("npx not found on PATH. Install Node.js, or `npm i -D wrangler` in this repo.")
    return [npx, "--yes", "wrangler"]


def _run(cmd: List[str], timeout: int = 180) -> Tuple[int, str]:
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(PROJECT_ROOT), timeout=timeout,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# ---------------------------------------------------------------------------
# pure helpers — pinned by tests/test_publish_audio_manifest.py
# ---------------------------------------------------------------------------
def files_digest(files: dict) -> str:
    """sha256 over the `files` map alone — ⚠️ NOT over the whole document.

    The document carries a `generated` timestamp that moves on every call, so
    a digest over the serialised payload would differ every time and this
    script would re-PUT on every pipeline run forever. What actually decides
    whether the Worker's answer changes is the file map, so that is what is
    hashed. `sort_keys` makes the digest independent of dict ordering.
    """
    blob = json.dumps(files or {}, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def manifest_problems(payload: dict) -> List[str]:
    """Empty list = publishable. ⚠️ An EMPTY `files` map is FINE — see the header.

    The only real refusal is "this is not an audio manifest at all", which
    protects against publishing a half-written or wrong-shaped document over a
    good one. Every row is otherwise the ingest script's business; the Worker
    skips a row with no anchor rather than serving a bucket-root object.
    """
    files = payload.get("files")
    if not isinstance(files, dict):
        return ["the payload has no 'files' object — it is broken, not merely empty"]
    problems: List[str] = []
    for key, row in files.items():
        if not isinstance(row, dict):
            problems.append(f"row {key!r} is not an object")
        elif row.get("streamable") and not row.get("anchor"):
            # A streamable row with no anchor is unreachable: the Worker's index
            # is keyed on the anchor, so this book would be invisible forever
            # while the record claims it is up. Worth failing loudly.
            problems.append(f"row {key!r} is streamable but carries no anchor")
    return problems


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------
def _read_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a missing/corrupt receipt means "publish"
        return {}


def _write_state(digest: str, count: int) -> None:
    STATE_PATH.write_text(
        json.dumps(
            {
                "bucket": BUCKET,
                "key": KEY,
                "files_sha256": digest,
                "streamable": count,
                "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
def publish_if_changed(force: bool = False, dry_run: bool = False,
                       quiet: bool = False) -> bool:
    """Publish the current record. Returns True if the bucket holds it.

    ⚠️ NEVER RAISES. The pipeline calls this from STEP 5.9, which is soft: a
    dead uplink or a missing wrangler must warn and let the run continue, not
    strand a library sync. A failure here costs a stale streamable list until
    the next run, which is hours — never a lost book, because the record on
    disk is the source of truth and it is untouched by a failed publish.
    """
    def say(msg: str) -> None:
        if not quiet:
            print(msg)

    try:
        files = up.load_record()
        payload = up.record_payload(files)
        problems = manifest_problems(payload)
        if problems:
            say("[audio-publish] REFUSED — the record is not a publishable manifest:")
            for p in problems[:20]:
                say(f"    - {p}")
            say("  The previously published manifest still stands.")
            return False

        digest = files_digest(files)
        state = _read_state()
        streamable = payload.get("streamable", 0)
        unchanged = (
            not force
            and state.get("files_sha256") == digest
            and state.get("bucket") == BUCKET
            and state.get("key") == KEY
        )
        if unchanged:
            say(f"[audio-publish] unchanged ({streamable} streamable) — nothing to upload.")
            return True

        if dry_run:
            say(f"[audio-publish] would PUT {len(files)} row(s) / {streamable} streamable "
                f"-> {BUCKET}/{KEY} (files sha256 {digest[:12]})")
            return True

        # ⚠️ A TEMP FILE, never the record itself. `wrangler r2 object put`
        # takes a path, and pointing it at site/audio_manifest.json would race
        # a concurrent upload checkpointing that same file mid-PUT (upload_keys
        # rewrites it after every object, because each one is expensive to
        # redo). Serialising here means what is published is exactly what was
        # validated a few lines above.
        tmp = Path(tempfile.gettempdir()) / f"audio_manifest.publish.{os.getpid()}.json"
        try:
            tmp.write_text(
                json.dumps(payload, indent=1, ensure_ascii=False, sort_keys=False) + "\n",
                encoding="utf-8",
            )
            cmd = _wrangler_cmd() + [
                "r2", "object", "put", f"{BUCKET}/{KEY}",
                "--file", str(tmp),
                "--content-type", "application/json",
                # ⚠️ Never a shared cache. The Worker serves this per-person
                # behind a bearer; an edge-cached copy of a gated body is the
                # one thing that could hand it to the wrong listener.
                "--cache-control", "private, no-store",
                "--remote",
            ]
            rc, out = _run(cmd)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

        if rc != 0:
            say(f"[audio-publish] upload FAILED (exit {rc}):")
            say("  " + out.strip().replace("\n", "\n  ")[:2000])
            say("  The previously published manifest still stands.")
            return False

        _write_state(digest, streamable)
        say(f"[audio-publish] published {len(files)} row(s) / {streamable} streamable "
            f"-> {BUCKET}/{KEY}")
        return True
    except Exception as exc:  # noqa: BLE001 — soft by construction, see the docstring
        say(f"[audio-publish] WARN: could not publish ({type(exc).__name__}: {exc}); "
            "the previously published manifest still stands")
        return False


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="say what would happen; upload nothing")
    ap.add_argument("--force", action="store_true", help="re-upload even when the content is unchanged")
    args = ap.parse_args(argv)
    return 0 if publish_if_changed(force=args.force, dry_run=args.dry_run) else 1


if __name__ == "__main__":
    sys.exit(main())
