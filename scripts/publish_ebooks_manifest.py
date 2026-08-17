#!/usr/bin/env python3
"""Publish `site/ebooks.json` to the PRIVATE R2 bucket the gated shelf reads.

WHY THIS EXISTS
---------------
Owner directive, 2026-08-17: *"ebooks should be like the other site where we
grant permission to view it. I don't want people scraping my books."*

Before the gate, the manifest was published in TWO public places at once and
both had to close, because closing only one moves a door in front of an open
window:

  1. **The Pages deployment.** `deploy.yml` assembles `_site/` from each
     branch's committed `site/`, so a tracked `site/ebooks.json` shipped to
     `audiobooks.heygabi.ai/ebooks.json` — fetchable by anyone.
  2. **GitHub.** ⚠️ `skymitch9/audiobook_catalog` is a PUBLIC repo (and must
     stay public — it is the only repo with workflows, and two crons would
     exhaust metered Actions minutes on a private one). A tracked file there
     is world-readable at a stable raw URL regardless of what the site does.

So `site/ebooks.json` is now GITIGNORED, is still written on this machine by
sync step 1b (every local check still reads it), and reaches the world only
through this script -> the `ebooks-gated` bucket -> apps/audiobook-worker's
`GET /api/ebooks/manifest`, which requires a verified Firebase token AND the
estate's `ebooks` visibility grant.

⚠️ A SEPARATE BUCKET FROM THE COVERS, and the separation is the whole point.
`audiobook-covers` has a public r2.dev URL enabled, so every object in it is
fetchable by anyone who guesses the key — putting the manifest there under a
"secret" prefix would be obscurity, not a gate. `ebooks-gated` was created
2026-08-17 with public access DISABLED and no custom domain; verified with
`wrangler r2 bucket dev-url get ebooks-gated`.

THE COVERAGE GATE, AND WHY IT MOVED HERE
----------------------------------------
⚠️ The owner's EVERY-EPUB-HAS-A-COVER rule ("this is so so important to me")
was enforced in two CI places that both read the COMMITTED manifest:
`tests/test_ebook_covers.py` and `app.tools.audit_site` check 5. Both already
`pytest.skip` / warn when the file is absent — which is correct behaviour for
a pre-ebooks checkout, and which means that with the file deliberately out of
git they now SKIP in CI rather than gate it. That is an honest consequence of
the gate, not a silent one, and it is why the same rule is re-asserted HERE,
at the one moment that still matters: publishing. A manifest that violates it
is not uploaded.

Emergency escape hatch, identical to the one those checks honour:
`ALLOW_COVERLESS_EPUBS=1`. It lets a known-broken shelf reach the readers —
use it to unblock something urgent, never to live with missing covers.

CONTRACT
--------
* **Idempotent by content.** The object is re-PUT only when the local file's
  sha256 differs from what was last published (recorded in
  `scripts/.ebooks_published.json`, gitignored). `--force` re-uploads anyway.
* **Never partial.** The gate runs before the upload; a refused manifest
  leaves the previous one serving, which is the safe direction — readers see
  a slightly stale shelf rather than none.
* **Auth comes from wrangler**, which owns its own OAuth token. Nothing here
  reads or stores a credential.

USAGE
-----
    python -m scripts.publish_ebooks_manifest             # publish if changed
    python -m scripts.publish_ebooks_manifest --dry-run   # say what would happen
    python -m scripts.publish_ebooks_manifest --force     # re-upload regardless

Exit 0 = the bucket holds the current manifest. Exit 1 = it does not (refused
by the coverage gate, or the upload failed) — the previous object still
stands.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = PROJECT_ROOT / "site" / "ebooks.json"
STATE_PATH = PROJECT_ROOT / "scripts" / ".ebooks_published.json"
# The one ebook file that stays PUBLIC — freshness only, never book data.
HEARTBEAT_PATH = PROJECT_ROOT / "site" / "ebooks_status.json"

# The bucket and key apps/audiobook-worker reads (its wrangler.toml binds
# EBOOKS_GATED -> ebooks-gated; src/ebooks.ts MANIFEST_KEY is 'ebooks.json').
# ⚠️ Both halves are one contract — changing either without the other gives
# readers a 503 `manifest_absent` that looks exactly like a pipeline stall.
BUCKET = os.getenv("EBOOKS_GATED_BUCKET", "ebooks-gated")
KEY = "ebooks.json"

ALLOW_COVERLESS_ENV = "ALLOW_COVERLESS_EPUBS"
NEEDS_HUMAN_COVER_KEY = "needs_human_cover"


# ---------------------------------------------------------------------------
# wrangler (the upload_covers_r2.py idiom, verbatim — one way to call it)
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
# the coverage gate
# ---------------------------------------------------------------------------
def coverage_problems(manifest: dict) -> List[str]:
    """The owner's cover rule, restated at publish time. Empty list = clean.

    Two halves, matching `app.tools.audit_site` check 5 exactly:
      * EVERY published EPUB carries a `cover_url`.
      * EVERY published PDF either carries one OR is named in
        `needs_human_cover` — a text-first PDF cannot break the publish, but a
        SILENT cover gap cannot exist either.
    """
    entries = manifest.get("ebooks")
    if not isinstance(entries, list):
        return ["the manifest has no 'ebooks' list — it is broken, not merely coverless"]

    named = set()
    gaps = manifest.get(NEEDS_HUMAN_COVER_KEY)
    if isinstance(gaps, list):
        for g in gaps:
            if isinstance(g, dict) and isinstance(g.get("path"), str):
                named.add(g["path"])

    problems: List[str] = []
    epubs = 0
    for e in entries:
        if not isinstance(e, dict):
            continue
        fmt = str(e.get("format") or "").lower()
        has_cover = bool(e.get("cover_url"))
        title = e.get("title") or e.get("path") or "(untitled)"
        if fmt == "epub":
            epubs += 1
            if not has_cover:
                problems.append(f"EPUB without a cover: {title}")
        elif fmt == "pdf":
            if not has_cover and e.get("path") not in named:
                problems.append(f"PDF with neither a cover nor a needs_human_cover entry: {title}")

    if epubs == 0:
        problems.append("the manifest lists no EPUBs at all — that is a broken build, not a clean shelf")
    return problems


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(digest: str) -> None:
    STATE_PATH.write_text(
        json.dumps(
            {
                "bucket": BUCKET,
                "key": KEY,
                "sha256": digest,
                "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# the public heartbeat
# ---------------------------------------------------------------------------
def _write_public_heartbeat(manifest: dict) -> None:
    """Write `site/ebooks_status.json` — FRESHNESS ONLY, no book data.

    ⚠️ WHY THIS EXISTS, and why it is deliberately not the manifest. The apex
    status page (`catalog-platform/sites/heygabi-home/public/status/status.js`)
    and the estate probe suite both read `ebooks.json:generated_at` to answer
    one operational question: *did sync step 1b actually run?* Gating the
    manifest broke that read, and the honest fix is not to hand an admin page
    the whole shelf to read one timestamp — it is to publish the timestamp.

    ⚠️ WHAT MAY GO IN HERE: counts and times. **No titles, no authors, no
    paths, no `needs_human_cover` entries** — that array names files, which is
    exactly why it rides inside the gate. Adding a field that identifies a book
    would quietly reopen the surface this whole build closed, in the one file
    that is still public on purpose.
    """
    payload = {
        "generated_at": manifest.get("generated_at"),
        "count": len(manifest.get("ebooks") or []),
        "needs_human_cover_count": len(manifest.get(NEEDS_HUMAN_COVER_KEY) or []),
        "note": "freshness only — the shelf itself is gated (see catalog-platform/docs/access/ebooks-gate.md)",
    }
    HEARTBEAT_PATH.write_text(json.dumps(payload, indent=2) + chr(10), encoding="utf-8")


# ---------------------------------------------------------------------------
def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="say what would happen; upload nothing")
    ap.add_argument("--force", action="store_true", help="re-upload even when the content is unchanged")
    args = ap.parse_args(argv)

    if not MANIFEST_PATH.exists():
        print(f"[ebooks-publish] {MANIFEST_PATH} not present — nothing to publish.")
        print("  Build it first: python -m scripts.build_ebook_manifest")
        return 1

    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — any parse failure is the same answer
        print(f"[ebooks-publish] {MANIFEST_PATH} is not readable JSON: {exc}")
        return 1

    problems = coverage_problems(manifest)
    if problems:
        if os.getenv(ALLOW_COVERLESS_ENV) == "1":
            print(f"[ebooks-publish] ⚠️  {ALLOW_COVERLESS_ENV}=1 — EMERGENCY ESCAPE HATCH, publishing anyway:")
            for p in problems[:20]:
                print(f"    - {p}")
        else:
            print("[ebooks-publish] REFUSED — the cover rule is not satisfied:")
            for p in problems[:20]:
                print(f"    - {p}")
            if len(problems) > 20:
                print(f"    … and {len(problems) - 20} more")
            print("  Fix with: python -m scripts.build_ebook_manifest")
            print(f"  (Emergency only: {ALLOW_COVERLESS_ENV}=1 publishes a known-broken shelf.)")
            print("  The previously published manifest still stands — readers see a stale shelf, not none.")
            return 1

    digest = _sha256(MANIFEST_PATH)
    state = _read_state()
    unchanged = (
        not args.force
        and state.get("sha256") == digest
        and state.get("bucket") == BUCKET
        and state.get("key") == KEY
    )
    count = len(manifest.get("ebooks") or [])
    gaps = len(manifest.get(NEEDS_HUMAN_COVER_KEY) or [])

    if unchanged:
        print(f"[ebooks-publish] unchanged ({count} books, {gaps} needing a human cover) — nothing to upload.")
        return 0

    if args.dry_run:
        print(f"[ebooks-publish] would PUT {MANIFEST_PATH.name} -> {BUCKET}/{KEY}")
        print(f"  {count} books, {gaps} needing a human cover, sha256 {digest[:12]}")
        return 0

    cmd = _wrangler_cmd() + [
        "r2", "object", "put", f"{BUCKET}/{KEY}",
        "--file", str(MANIFEST_PATH),
        "--content-type", "application/json",
        # ⚠️ Never a shared cache. The Worker serves this per-person behind a
        # bearer; an edge-cached copy of a gated body is the one thing that
        # could hand it to the wrong reader.
        "--cache-control", "private, no-store",
        "--remote",
    ]
    rc, out = _run(cmd)
    if rc != 0:
        print(f"[ebooks-publish] upload FAILED (exit {rc}):")
        print("  " + out.strip().replace("\n", "\n  ")[:2000])
        print("  The previously published manifest still stands.")
        return 1

    _write_state(digest)
    _write_public_heartbeat(manifest)
    print(f"[ebooks-publish] published {count} books ({gaps} needing a human cover) -> {BUCKET}/{KEY}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
