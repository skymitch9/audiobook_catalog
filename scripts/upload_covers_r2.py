"""
Upload site/covers/** to the Cloudflare R2 bucket that serves the catalog's
cover art, and maintain site/covers_manifest.json as the record of what is
up there.

WHY THIS EXISTS
---------------
Covers used to be committed to git and served from the site itself. That put
243 MB / 1,843 files in the repo and copied them into BOTH deploy lanes on
every deploy (~514 MB per publish). They now live in R2 and the generated site
points at `COVERS_BASE_URL` (app/config.py).

    site/covers/<author>/<title>.jpg   ->   <COVERS_BASE_URL><author>/<title>.jpg

The R2 object key is the path RELATIVE TO site/covers — there is no `covers/`
prefix in the bucket, so a future swap from the r2.dev URL to a custom domain
such as https://covers.heygabi.ai/ is a one-line change and nothing else.

CONTRACT
--------
* **Idempotent.** A file is uploaded only when its sha256 differs from (or is
  absent from) site/covers_manifest.json. Re-running with no local changes
  uploads nothing.
* **Never deletes.** Orphans are reported by `--report-orphans` and left
  alone; the prod branch may still reference a cover that main dropped.
  (`--report-orphans` compares the manifest to the local tree — wrangler v4
  has no `r2 object list`, so a live bucket listing is not available here.)
* **Auth comes from wrangler**, which owns its own OAuth token and refreshes
  it. Nothing here reads or stores a credential. Run `npx wrangler login`
  once (or set CLOUDFLARE_API_TOKEN, which wrangler picks up itself).

USAGE
-----
    python -m scripts.upload_covers_r2                 # upload changed, write manifest
    python -m scripts.upload_covers_r2 --dry-run       # show what would change
    python -m scripts.upload_covers_r2 --force         # re-upload everything
    python -m scripts.upload_covers_r2 --report-orphans

Exit code 0 = everything that needed uploading is up; 1 = at least one upload
failed (the manifest still records the ones that succeeded, so a re-run
retries only the failures).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COVERS_DIR = PROJECT_ROOT / "site" / "covers"
CATALOG_PATH = PROJECT_ROOT / "site" / "catalog.csv"
MANIFEST_PATH = PROJECT_ROOT / "site" / "covers_manifest.json"

BUCKET = os.getenv("COVERS_R2_BUCKET", "audiobook-covers")

# Matches site/_headers' `/covers/*` rule. Cover filenames are stable while
# their bytes can change (re-extraction), so this is a bounded TTL, never
# `immutable`. See docs/info/caching.md.
CACHE_CONTROL = "public, max-age=604800"

CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

DEFAULT_WORKERS = 8
UPLOAD_RETRIES = 3
CHECKPOINT_EVERY = 200  # rewrite the manifest this often so a kill is resumable


# ---------------------------------------------------------------------------
# wrangler
# ---------------------------------------------------------------------------
def _wrangler_cmd() -> List[str]:
    """The command prefix that runs wrangler, preferring a local install."""
    local = PROJECT_ROOT / "node_modules" / ".bin" / ("wrangler.cmd" if os.name == "nt" else "wrangler")
    if local.exists():
        return [str(local)]
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        raise SystemExit(
            "npx not found on PATH. Install Node.js, or `npm i -D wrangler` in this repo."
        )
    return [npx, "--yes", "wrangler"]


def _run(cmd: List[str], timeout: int = 180) -> Tuple[int, str]:
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(PROJECT_ROOT), timeout=timeout,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def wrangler_key(rel_key: str) -> str:
    """The key as `wrangler r2 object put` must be given it.

    ⚠️ wrangler splices the key STRAIGHT into the REST URL path without
    encoding it, so any character that means something in a URL is silently
    mishandled. Both failure modes were measured against this catalog:

      `#`  "The Dark Healer (Book #9).jpg"  -> treated as a fragment, the key
           is TRUNCATED at the #, wrangler reports "Upload complete", and the
           object is unreachable. This is the dangerous one: it looks like a
           success. 8 covers hit it.
      `%`  "1% Lifesteal.jpg"               -> "% L" is an invalid escape, the
           edge returns 400 and Node then dies with a libuv assertion. Noisy,
           but at least it fails. 1 cover hit it.

    Pre-encoding these makes the server decode them back to the literal
    character, so the stored key is the real filename and the site's own
    `quote(safe="/")` URL resolves to it. Spaces, `&`, `+`, `(` and non-ASCII
    all go through literally and were verified working — do not "helpfully"
    encode more than this.
    """
    return rel_key.replace("%", "%25").replace("#", "%23").replace("?", "%3F")


def upload_one(rel_key: str, path: Path) -> Tuple[str, bool, str]:
    """PUT one cover into the bucket. Returns (rel_key, ok, detail)."""
    ctype = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
    cmd = _wrangler_cmd() + [
        "r2", "object", "put", f"{BUCKET}/{wrangler_key(rel_key)}",
        "--file", str(path),
        "--content-type", ctype,
        "--cache-control", CACHE_CONTROL,
        "--remote",
    ]
    last = ""
    for attempt in range(1, UPLOAD_RETRIES + 1):
        try:
            code, out = _run(cmd)
        except subprocess.TimeoutExpired:
            code, out = 1, "timeout"
        # ⚠️ wrangler on Windows has been observed printing success and then
        # exiting non-zero, so the OUTPUT is the source of truth, not the code.
        if "Upload complete" in out or code == 0:
            return rel_key, True, ""
        last = out.strip().splitlines()[-1] if out.strip() else f"exit {code}"
        if attempt < UPLOAD_RETRIES:
            time.sleep(2 * attempt)
    return rel_key, False, last


# ---------------------------------------------------------------------------
# local scan / manifest
# ---------------------------------------------------------------------------
def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def catalog_cover_keys() -> List[str]:
    """Every `cover_href` in site/catalog.csv, as an R2 object key."""
    if not CATALOG_PATH.exists():
        return []
    with CATALOG_PATH.open(encoding="utf-8", newline="") as f:
        rows = csv.DictReader(f)
        out = []
        for r in rows:
            href = (r.get("cover_href") or "").strip().replace("\\", "/").lstrip("/")
            if href.startswith("covers/"):
                href = href[len("covers/"):]
            if href:
                out.append(href)
    return out


def scan_local() -> Dict[str, dict]:
    """Every cover that must exist in the bucket, keyed by its R2 object key.

    Two sources, and the second one is not redundant:

    1. Everything `rglob` finds under site/covers.
    2. Every `cover_href` in site/catalog.csv that is READABLE at that path.

    ⚠️ (2) exists because the covers tree contains case-colliding directories
    that git tracks separately and Windows cannot: `V.A. Lewis/`,
    `V.a. Lewis/` and `V.A. Lewis - Amber the Cursed Berserker/` are three
    paths in the index and one directory on disk. `rglob` reports only the
    name the filesystem actually has, so 7 catalog rows pointed at keys the
    directory walk never yielded — they would have been silent 404s on the
    live site while every local check passed, because `Path.exists()` on
    Windows resolves them fine.

    The catalog is what the page links to, so the catalog is what has to be
    in the bucket. This is a pre-existing repo defect (duplicate author-folder
    spellings, see docs/info/author-folder-audit.md); making the upload
    catalog-driven routes around it rather than pretending it is fixed.
    """
    out: Dict[str, dict] = {}
    if COVERS_DIR.exists():
        for p in sorted(COVERS_DIR.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(COVERS_DIR).as_posix()
            out[rel] = {"size": p.stat().st_size, "sha256": sha256_of(p)}
    for key in catalog_cover_keys():
        if key in out:
            continue
        p = COVERS_DIR / key
        try:
            if p.is_file():
                out[key] = {"size": p.stat().st_size, "sha256": sha256_of(p)}
        except OSError:
            continue
    return out


def load_manifest() -> Dict[str, dict]:
    if not MANIFEST_PATH.exists():
        return {}
    try:
        with MANIFEST_PATH.open(encoding="utf-8") as f:
            return json.load(f).get("files", {}) or {}
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[WARN] {MANIFEST_PATH} unreadable ({exc}) — treating every cover as new")
        return {}


def write_manifest(files: Dict[str, dict]) -> None:
    payload = {
        "_comment": (
            "Covers live in Cloudflare R2, not git. Object key = path relative to "
            "site/covers. Generated by scripts/upload_covers_r2.py — do not hand-edit."
        ),
        "bucket": BUCKET,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(files),
        "files": {k: files[k] for k in sorted(files)},
    }
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MANIFEST_PATH.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, indent=1, ensure_ascii=False, sort_keys=False)
        f.write("\n")


def orphan_keys(local: Dict[str, dict], manifest: Dict[str, dict]) -> List[str]:
    """Manifest entries with no local file — objects the bucket is still
    holding for a book that has left the catalog.

    ⚠️ This compares against the MANIFEST, not against a live bucket listing:
    wrangler v4 has no `r2 object list`, so there is no CLI way to enumerate
    the bucket. The manifest is this repo's record of what was uploaded, and
    anything uploaded outside this script is invisible here.

    Nothing is deleted. The prod branch can still be serving a cover that main
    has dropped, and R2 storage is charged against a 10 GB free tier.
    """
    return sorted(set(manifest) - set(local))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="show the diff, upload nothing")
    ap.add_argument("--force", action="store_true", help="re-upload every cover")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--limit", type=int, default=0, help="upload at most N files (testing)")
    ap.add_argument("--report-orphans", action="store_true",
                    help="list bucket objects with no local file (never deletes)")
    args = ap.parse_args(argv)

    print(f"Scanning {COVERS_DIR} ...")
    local = scan_local()
    if not local:
        print(f"[WARN] no covers found under {COVERS_DIR} — nothing to do")
        return 0
    manifest = {} if args.force else load_manifest()

    pending = [
        k for k, meta in local.items()
        if manifest.get(k, {}).get("sha256") != meta["sha256"]
    ]
    total_bytes = sum(local[k]["size"] for k in pending)
    print(f"  local covers : {len(local)}")
    print(f"  in manifest  : {len(manifest)}")
    print(f"  to upload    : {len(pending)} ({total_bytes / 1e6:.1f} MB)")

    if args.report_orphans:
        orphans = orphan_keys(local, load_manifest())
        print(f"  orphans in manifest with no local file (kept): {len(orphans)}")
        for o in orphans[:50]:
            print(f"    orphan: {o}")
        if len(orphans) > 50:
            print(f"    ... and {len(orphans) - 50} more")

    if args.limit:
        pending = pending[: args.limit]
        print(f"  --limit: uploading only the first {len(pending)}")

    if args.dry_run:
        for k in pending[:20]:
            print(f"    would upload: {k}")
        if len(pending) > 20:
            print(f"    ... and {len(pending) - 20} more")
        return 0

    if not pending:
        # Still rewrite the manifest: it may be missing or stale in shape.
        write_manifest(local)
        print("Nothing to upload. Manifest refreshed.")
        return 0

    # Record what is genuinely in the bucket: everything already in the
    # manifest plus this run's successes. A failure stays out, so the next run
    # retries exactly it.
    #
    # ⚠️ Entries whose local file has gone are KEPT, deliberately. The object
    # is still in R2 and `prod` may still be serving it, and site/covers is a
    # rebuildable local cache that genuinely does empty out — an ff-merge past
    # the commit that untracked it wipes the directory, and app.main refills it
    # from output_files/covers on the next build. Pruning on "not on disk"
    # would silently shrink the manifest, and the manifest is what the promote
    # audit checks, so the next promote would fail on books whose covers are
    # perfectly fine. Use --report-orphans to see them.
    recorded = dict(manifest)

    uploaded, failed = [], []
    started = time.time()
    # Checkpoint as we go. The first full backfill is ~1,800 objects at ~3/s,
    # i.e. ten minutes, and anything that long WILL eventually be interrupted
    # (it was, by a task timeout, the first time). Writing the manifest
    # periodically makes a re-run resume instead of starting over.
    since_checkpoint = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(upload_one, k, COVERS_DIR / k): k for k in pending}
        for i, fut in enumerate(as_completed(futures), 1):
            key, ok, detail = fut.result()
            (uploaded if ok else failed).append(key)
            if ok:
                recorded[key] = local[key]
                since_checkpoint += 1
                if since_checkpoint >= CHECKPOINT_EVERY:
                    write_manifest(recorded)
                    since_checkpoint = 0
                if i % 25 == 0 or i == len(pending):
                    rate = i / max(1e-6, time.time() - started)
                    eta = (len(pending) - i) / max(1e-6, rate)
                    print(f"  [{i}/{len(pending)}] {rate:.1f}/s  eta {eta / 60:.1f} min",
                          flush=True)
            else:
                print(f"  [FAIL] {key}: {detail}", flush=True)

    write_manifest(recorded)

    mins = (time.time() - started) / 60
    print(f"\nUploaded {len(uploaded)} / {len(pending)} in {mins:.1f} min; "
          f"{len(failed)} failed. Manifest: {len(recorded)} objects.")
    if failed:
        print("::error::some covers failed to upload — re-run to retry just those")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
