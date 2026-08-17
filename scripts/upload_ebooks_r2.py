#!/usr/bin/env python3
"""Ingest the ebook FILES themselves into the private `estate-ebooks` R2 bucket.

VIEWER PHASE 0a. Modelled line-for-line on `scripts/upload_covers_r2.py`, which
has already paid for every lesson this script would otherwise learn again.

WHY THIS EXISTS
---------------
`scripts/publish_ebooks_manifest.py` puts the *catalogue* of ebooks behind a
gate (`ebooks-gated` bucket -> audiobook-worker `GET /api/ebooks/manifest`).
That publishes what books exist; it publishes no bytes. The viewer
(library_catalog `docs/info/ebook-viewer-design.md`) needs the bytes reachable
from a Worker binding and from nowhere else, so this script uploads the 168
epub/pdf files from the library disk root into a SECOND private bucket.

    <ebooks.json:root>/<row.path>   ->   estate-ebooks/<row.path>

⚠️ `estate-ebooks` has NO public r2.dev URL and NO custom domain, verified
2026-08-17 with `wrangler r2 bucket dev-url get estate-ebooks` ("Public access
via the r2.dev URL is disabled"). It is reachable only through a Worker binding
(`[[r2_buckets]] binding = "EBOOKS"`, phase 1a). ⚠️ **Never enable a dev URL on
it.** Every object in it is a whole book; a dev URL turns the bucket into the
scraping surface the gate was built to close.

THE KEY SCHEME  (design doc §2.1 — do not change without a migration)
--------------------------------------------------------------------
**The object key is the manifest row's `path`, verbatim.** No prefix, no
re-encoding, no hash:

    row["path"] == "Brandon Sanderson/Defiant.pdf"
    R2 key      == "Brandon Sanderson/Defiant.pdf"

Why not key on the `anchor` (`"b-" + sha256(path)[:12]`), given the future
endpoint is `GET /api/ebook/:anchor/file`?

  * The anchor is a hash OF the path, so it adds no uniqueness — measured on
    today's manifest: 168 unique paths, 168 unique anchors, a bijection.
  * The Worker must load `ebooks.json` anyway in order to authorise (§3.4), so
    it already has the `anchor -> path` map in hand. Resolving the anchor to a
    path and calling `env.EBOOKS.get(path)` is a dictionary lookup, i.e. the
    `:anchor -> object` mapping is 1:1 either way.
  * A human debugging a missing book in the Cloudflare dashboard needs to see
    `Brandon Sanderson/Defiant.pdf`, not `b-a49cd096d824`.
  * It matches the covers bucket's own no-prefix choice, which that work was
    glad of when the r2.dev URL was swapped for a custom domain.

⚠️ The anchor is derived from the FILE PATH, so re-filing a book changes both
its anchor and its key. That is fine here — this is content addressing by
location, and a moved book simply uploads under its new key (the old object is
never deleted; see CONTRACT). It is NOT fine for a stored reading position,
which is why §7 of the design doc keys those on `workKey` instead.

`tests/test_upload_ebooks_r2.py` pins this scheme with golden fixtures: any
mutation of `object_key()` fails the suite.

THE 300 MiB WALL  (measured 2026-08-17 — the finding that shaped this script)
----------------------------------------------------------------------------
⚠️ **`wrangler r2 object put` refuses files over 300 MiB**, measured, not
assumed:

    X [ERROR] Error: Wrangler only supports uploading files up to 300 MiB in size
      ...White Sand Omnibus... is 393 MiB in size
    Assertion failed: !(handle->flags & UV_HANDLE_CLOSING), src\\win\\async.c:76

(the libuv assertion is the same Windows crash `upload_covers_r2.wrangler_key`
documents for `%` — wrangler dies noisily rather than cleanly). `--pipe` does
not help: the 300 MiB ceiling is the Cloudflare REST object endpoint's, which
wrangler merely wraps.

Exactly ONE of the 168 files is over that line — the 393 MiB
`Brandon Sanderson/White Sand Omnibus ... - Rik Hoskin.epub`. The other 167
(next largest 181 MiB) go through wrangler untouched.

So this script has TWO backends, and picks per file by size:

  * **wrangler** (default, and what shipped): auth comes from wrangler's own
    OAuth token, nothing here reads or stores a credential. Used for every
    file <= 300 MiB.
  * **S3-compatible multipart** (`boto3`): the only way past the wall. It needs
    an R2 API token, which is an OWNER ACTION — Cloudflare dashboard -> R2 ->
    *Manage R2 API Tokens* -> create a token with Object Read & Write on
    `estate-ebooks`, then:

        set R2_ACCOUNT_ID=<32-hex account id>
        set R2_ACCESS_KEY_ID=<from the token>
        set R2_SECRET_ACCESS_KEY=<from the token>
        pip install boto3

    and re-run. Idempotence means the re-run touches only that one file.

⚠️ A file over the wall with no S3 credentials configured is reported as a
NAMED FAILURE and exits 1. It is never silently skipped — a book that is in
the manifest but not in the bucket is a 404 in the reader, and the whole point
of this bookkeeping is that such a gap is visible here rather than there.

CONTRACT
--------
* **`--dry-run` is the DEFAULT.** Nothing is uploaded without `--commit`.
* **Idempotent.** A file is uploaded only when its size+mtime differ from the
  record AND its sha256 differs too (the hash is the authority; mtime is only
  a cheap pre-filter, so a touched-but-unchanged file costs a hash, not
  1.8 GB of uplink). Re-running with no library changes uploads nothing.
* **Resumable.** The record is checkpointed every few objects, and a failed
  upload names its file and does not stop the run. A re-run retries exactly
  the failures. ⚠️ A 10-minute task cap killed the covers backfill at
  1,425/1,827 before checkpointing existed, and this payload is 7x larger by
  bytes.
* **Never deletes.** Orphans (a record with no local file) are kept and
  reported by `--report-orphans`. wrangler v4.123 still has no
  `r2 object list`, so no live bucket listing is available from the CLI.
* **The record is `site/ebook_files_manifest.json`, and it is GITIGNORED.**
  ⚠️ This deviates from design doc §2.1, which called for it to be committed,
  and the deviation is deliberate: the record is keyed on FILE PATHS, i.e. it
  is a list of the household's books by name — exactly the scraping surface
  closed on 2026-08-17 when `site/ebooks.json` was gitignored (this repo is
  PUBLIC and must stay public). `site/covers_manifest.json` stays committed
  because it is a list of sha256 hashes, not a list of books. Nothing outside
  this machine needs the file: the Worker resolves `anchor -> path` from
  `ebooks.json` in the gated bucket, never from this record.

USAGE
-----
    python -m scripts.upload_ebooks_r2                  # dry run (the default)
    python -m scripts.upload_ebooks_r2 --commit         # actually upload
    python -m scripts.upload_ebooks_r2 --commit --force # re-upload everything
    python -m scripts.upload_ebooks_r2 --only "White Sand"   # one book
    python -m scripts.upload_ebooks_r2 --report-orphans

Exit 0 = every file that needed uploading is up. Exit 1 = at least one is not
(the record still holds this run's successes, so a re-run retries only those).

PIPELINE WIRING — NOT DONE HERE, ON PURPOSE
-------------------------------------------
This becomes sync step **5.8**, directly after covers' 5.7 and before the
auto-commit, so a published manifest can never name an object that is not in
the bucket. The one-line wiring is a conductor/owner step because
`scripts/sync_to_drive.py` auto-runs 3x/day and may be contested; see
`docs/info/ebooks-r2-ingest.md` for the exact line to add.
"""

from __future__ import annotations

import argparse
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
from typing import Callable, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EBOOKS_JSON = PROJECT_ROOT / "site" / "ebooks.json"
RECORD_PATH = PROJECT_ROOT / "site" / "ebook_files_manifest.json"

BUCKET = os.getenv("EBOOKS_R2_BUCKET", "estate-ebooks")

# ⚠️ MEASURED 2026-08-17, not assumed. See the module docstring.
WRANGLER_MAX_BYTES = 300 * 1024 * 1024

# ⚠️ Never a shared cache. These bodies are served per-person behind a bearer
# token; an edge-cached copy of a gated book is the one thing that could hand
# it to the wrong reader. The Worker sets its own response headers too — this
# is belt and braces on the stored object.
CACHE_CONTROL = "private, max-age=0, no-store"
# A viewer, not a download button (design §6). Stored so the Worker can pass
# `object.httpMetadata` through rather than reinvent it.
CONTENT_DISPOSITION = "inline"

CONTENT_TYPES = {
    ".epub": "application/epub+zip",
    ".pdf": "application/pdf",
}
DEFAULT_CONTENT_TYPE = "application/octet-stream"

DEFAULT_WORKERS = 3  # these average 10.7 MB; the uplink is the limit, not Node
UPLOAD_RETRIES = 3
CHECKPOINT_EVERY = 5  # objects; small, because each one is expensive to redo
S3_MULTIPART_CHUNK = 64 * 1024 * 1024


# ---------------------------------------------------------------------------
# pure helpers — pinned by tests/test_upload_ebooks_r2.py
# ---------------------------------------------------------------------------
def object_key(row: dict) -> str:
    """The R2 object key for one `ebooks.json` row: its `path`, verbatim.

    ⚠️ THE KEY SCHEME. Changing this is a migration, not an edit: every object
    already in `estate-ebooks` is stored under the old scheme, and the phase-1a
    Worker resolves `anchor -> path -> key` on the assumption that the last
    arrow is the identity function. See the module docstring for why `path`
    beats `anchor` here.

    Normalised only for separators (a Windows-built manifest could carry
    backslashes) and a leading slash. No case folding, no prefix, no encoding —
    the key IS the path.
    """
    path = str(row.get("path") or "").replace("\\", "/").lstrip("/")
    if not path:
        raise ValueError(f"manifest row has no usable 'path': {row!r}")
    return path


def content_type_for(key: str) -> str:
    """MIME type from the extension. Unknown extensions get octet-stream."""
    return CONTENT_TYPES.get(Path(key).suffix.lower(), DEFAULT_CONTENT_TYPE)


def wrangler_key(rel_key: str) -> str:
    """The key as `wrangler r2 object put` must be given it.

    Carried verbatim from `upload_covers_r2.wrangler_key`. ⚠️ wrangler splices
    the key STRAIGHT into the REST URL path without encoding it: `#` truncates
    the key while REPORTING SUCCESS (8 covers were silently lost to this), and
    `%` produces an invalid escape that crashes Node.

    ⚠️ Measured: zero of today's 168 ebook paths contain `#`, `%` or `?` — they
    contain 9 apostrophes, 2 ampersands and one non-ASCII character
    (`Brené Brown/`), all three verified safe literally. The encoding is
    carried anyway, because "today's filenames are tame" is not a property
    anyone maintains.
    """
    return rel_key.replace("%", "%25").replace("#", "%23").replace("?", "%3F")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def decide(
    key: str,
    meta: dict,
    recorded: Optional[dict],
    force: bool,
    hasher: Callable[[str], str],
) -> Tuple[str, Optional[str]]:
    """Upload this file, or not? Returns (verdict, sha256-or-None).

    Verdicts: "upload" | "skip".

    Two tiers, and the cheap one exists purely so the 3x/day pipeline step does
    not read 1.8 GB off disk to learn that nothing changed:

      1. size + mtime_ns both match the record -> skip, without hashing.
      2. otherwise hash. sha256 matches the record -> skip anyway (the file was
         touched, re-exported byte-identical, or copied), and the caller
         refreshes the record's mtime so tier 1 catches it next time.

    ⚠️ The HASH is the authority; mtime is only ever allowed to say "skip"
    faster, never to say "upload" on its own. `--force` (and `--rehash`, which
    the caller implements by disabling tier 1) bypass tiers in that order.
    """
    if force or not recorded:
        return "upload", None
    if recorded.get("size") == meta["size"] and recorded.get("mtime_ns") == meta.get("mtime_ns"):
        return "skip", recorded.get("sha256")
    digest = hasher(key)
    if recorded.get("sha256") == digest:
        return "skip", digest
    return "upload", digest


def upload_timeout_for(size_bytes: int) -> int:
    """Seconds to allow one upload. A 393 MiB file on a household uplink is
    minutes, not the 180 s `upload_covers_r2` allows for a 100 KB jpeg."""
    return max(300, int((size_bytes / 1e6) * 6))


# ---------------------------------------------------------------------------
# wrangler backend
# ---------------------------------------------------------------------------
def _wrangler_cmd() -> List[str]:
    """The command prefix that runs wrangler, preferring a local install."""
    local = PROJECT_ROOT / "node_modules" / ".bin" / ("wrangler.cmd" if os.name == "nt" else "wrangler")
    if local.exists():
        return [str(local)]
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        raise SystemExit("npx not found on PATH. Install Node.js, or `npm i -D wrangler` in this repo.")
    return [npx, "--yes", "wrangler"]


def _run(cmd: List[str], timeout: int) -> Tuple[int, str]:
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(PROJECT_ROOT), timeout=timeout,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def upload_via_wrangler(key: str, src: Path, size: int) -> Tuple[bool, str]:
    cmd = _wrangler_cmd() + [
        "r2", "object", "put", f"{BUCKET}/{wrangler_key(key)}",
        "--file", str(src),
        "--content-type", content_type_for(key),
        "--content-disposition", CONTENT_DISPOSITION,
        "--cache-control", CACHE_CONTROL,
        "--remote",
    ]
    timeout = upload_timeout_for(size)
    last = ""
    for attempt in range(1, UPLOAD_RETRIES + 1):
        try:
            code, out = _run(cmd, timeout)
        except subprocess.TimeoutExpired:
            code, out = 1, f"timeout after {timeout}s"
        # ⚠️ wrangler on Windows has been observed printing success and then
        # exiting non-zero, so the OUTPUT is the source of truth, not the code.
        if "Upload complete" in out or (code == 0 and "ERROR" not in out):
            return True, ""
        last = out.strip().splitlines()[-1] if out.strip() else f"exit {code}"
        if attempt < UPLOAD_RETRIES:
            time.sleep(2 * attempt)
    return False, last


# ---------------------------------------------------------------------------
# S3 backend — the only way past the 300 MiB wall
# ---------------------------------------------------------------------------
S3_ENV = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")


def s3_unavailable_reason() -> Optional[str]:
    """None if the S3 backend can run; otherwise the exact thing to fix."""
    missing = [n for n in S3_ENV if not os.getenv(n)]
    if missing:
        return (
            "over wrangler's 300 MiB limit and the S3 fallback is not configured "
            f"(missing env: {', '.join(missing)}). Mint an R2 API token with Object "
            f"Read & Write on {BUCKET} (Cloudflare dashboard -> R2 -> Manage R2 API "
            "Tokens), export those three vars, `pip install boto3`, and re-run."
        )
    try:
        import boto3  # noqa: F401
    except ImportError:
        return "over wrangler's 300 MiB limit and boto3 is not installed (`pip install boto3`)"
    return None


def upload_via_s3(key: str, src: Path, size: int) -> Tuple[bool, str]:
    """Multipart PUT through R2's S3-compatible endpoint.

    boto3 rather than a hand-rolled SigV4 signer on purpose: multipart plus
    request signing is exactly the kind of code that is silently wrong until
    the day it matters, and boto3's `upload_file` already does both correctly
    (it chunks, retries per part, and aborts the upload on failure so no
    orphaned parts are billed).
    """
    reason = s3_unavailable_reason()
    if reason:
        return False, reason
    import boto3
    from boto3.s3.transfer import TransferConfig
    from botocore.config import Config as BotoConfig

    account = os.environ["R2_ACCOUNT_ID"]
    client = boto3.client(
        "s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=BotoConfig(retries={"max_attempts": 3, "mode": "standard"}),
    )
    cfg = TransferConfig(
        multipart_threshold=S3_MULTIPART_CHUNK,
        multipart_chunksize=S3_MULTIPART_CHUNK,
        max_concurrency=2,
    )
    try:
        client.upload_file(
            str(src), BUCKET, key,
            ExtraArgs={
                "ContentType": content_type_for(key),
                "ContentDisposition": CONTENT_DISPOSITION,
                "CacheControl": CACHE_CONTROL,
            },
            Config=cfg,
        )
    except Exception as exc:  # noqa: BLE001 — every failure is the same answer
        return False, f"S3 multipart failed: {type(exc).__name__}: {exc}"
    return True, ""


def upload_one(key: str, src: Path, size: int) -> Tuple[str, bool, str]:
    """Route one file to the backend its size demands."""
    if size > WRANGLER_MAX_BYTES:
        ok, detail = upload_via_s3(key, src, size)
    else:
        ok, detail = upload_via_wrangler(key, src, size)
    return key, ok, detail


# ---------------------------------------------------------------------------
# manifest / record IO
# ---------------------------------------------------------------------------
def load_rows() -> Tuple[List[dict], Path]:
    """The `ebooks.json` rows and the library disk root they hang off."""
    if not EBOOKS_JSON.exists():
        raise SystemExit(
            f"{EBOOKS_JSON} not present — build it first:\n"
            "  python -m scripts.build_ebook_manifest"
        )
    data = json.loads(EBOOKS_JSON.read_text(encoding="utf-8"))
    rows = data.get("ebooks")
    if not isinstance(rows, list) or not rows:
        raise SystemExit(f"{EBOOKS_JSON} has no 'ebooks' list — it is broken, not merely empty")
    root = os.getenv("EBOOKS_ROOT") or data.get("root")
    if not root:
        raise SystemExit(f"{EBOOKS_JSON} has no 'root' — cannot locate the files on disk")
    return rows, Path(str(root))


def scan_local(rows: List[dict], root: Path) -> Tuple[Dict[str, dict], List[str]]:
    """Everything the bucket must hold, keyed by R2 object key.

    Returns (found, missing_keys). A manifest row whose file is not on disk is
    NOT an upload failure and not a skip — it is reported separately, because
    the two have different fixes (re-run step 1b vs retry the uplink).
    """
    found: Dict[str, dict] = {}
    missing: List[str] = []
    for row in rows:
        key = object_key(row)
        src = root / key
        try:
            st = src.stat()
        except OSError:
            missing.append(key)
            continue
        found[key] = {
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
            "anchor": row.get("anchor"),
            "format": row.get("format"),
            "src": src,
        }
    return found, missing


def load_record() -> Dict[str, dict]:
    if not RECORD_PATH.exists():
        return {}
    try:
        return json.loads(RECORD_PATH.read_text(encoding="utf-8")).get("files", {}) or {}
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[WARN] {RECORD_PATH} unreadable ({exc}) — treating every file as new")
        return {}


def write_record(files: Dict[str, dict]) -> None:
    payload = {
        "_comment": (
            "Ebook FILES in the private Cloudflare R2 bucket 'estate-ebooks'. Object key "
            "= the ebooks.json row's `path`, verbatim. Generated by "
            "scripts/upload_ebooks_r2.py — do not hand-edit. ⚠️ GITIGNORED on purpose: "
            "it lists books by filename, which is the surface site/ebooks.json was "
            "gitignored to close (this repo is public)."
        ),
        "bucket": BUCKET,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(files),
        "total_bytes": sum(int(v.get("size") or 0) for v in files.values()),
        "files": {k: files[k] for k in sorted(files)},
    }
    RECORD_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RECORD_PATH.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=1, ensure_ascii=False, sort_keys=False)
        fh.write("\n")


def record_entry(meta: dict, digest: Optional[str]) -> dict:
    return {
        "size": meta["size"],
        "mtime_ns": meta.get("mtime_ns"),
        "sha256": digest,
        "anchor": meta.get("anchor"),
        "format": meta.get("format"),
        "uploaded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def orphan_keys(local: Dict[str, dict], record: Dict[str, dict]) -> List[str]:
    """Record entries with no local file — objects the bucket still holds for a
    book that has left the library or been re-filed under a new path.

    ⚠️ Compared against the RECORD, not a live bucket listing: wrangler v4.123
    still has no `r2 object list`. Nothing is deleted; a re-filed book's old
    object is harmless (nothing links to it — the anchor moved with the path)
    and R2 storage is charged against a 10 GB-month free tier.
    """
    return sorted(set(record) - set(local))


# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commit", action="store_true", help="actually upload (default is a dry run)")
    ap.add_argument("--dry-run", action="store_true", help="explicit no-op — the default already")
    ap.add_argument("--force", action="store_true", help="re-upload every file")
    ap.add_argument("--rehash", action="store_true", help="ignore mtime; hash every file")
    ap.add_argument("--only", default="", help="substring filter on the key (spot checks)")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--limit", type=int, default=0, help="upload at most N files (testing)")
    ap.add_argument("--report-orphans", action="store_true",
                    help="list recorded objects with no local file (never deletes)")
    args = ap.parse_args(argv)

    rows, root = load_rows()
    print(f"Manifest : {EBOOKS_JSON}  ({len(rows)} rows)")
    print(f"Root     : {root}")
    print(f"Bucket   : {BUCKET}")

    local, missing = scan_local(rows, root)
    if args.only:
        local = {k: v for k, v in local.items() if args.only.lower() in k.lower()}
        print(f"  --only {args.only!r}: {len(local)} of {len(rows)} rows match")

    record = {} if args.force else load_record()

    def hasher(key: str) -> str:
        return sha256_of(local[key]["src"])

    pending: List[str] = []
    fresh_digests: Dict[str, Optional[str]] = {}
    touched: List[str] = []  # unchanged bytes, changed mtime — refresh the record
    for key in sorted(local):
        rec = record.get(key)
        if args.rehash and rec:
            rec = {k: v for k, v in rec.items() if k != "mtime_ns"}
        verdict, digest = decide(key, local[key], rec, args.force, hasher)
        fresh_digests[key] = digest
        if verdict == "upload":
            pending.append(key)
        elif digest and record.get(key, {}).get("mtime_ns") != local[key].get("mtime_ns"):
            touched.append(key)

    total_bytes = sum(local[k]["size"] for k in pending)
    oversize = [k for k in pending if local[k]["size"] > WRANGLER_MAX_BYTES]

    print(f"  files on disk : {len(local)}")
    print(f"  in record     : {len(record)}")
    print(f"  to upload     : {len(pending)} ({total_bytes / 1e9:.3f} GB)")
    if touched:
        print(f"  touched only  : {len(touched)} (bytes unchanged; record refreshed)")
    if missing:
        print(f"  ⚠️ MISSING on disk: {len(missing)}")
        for k in missing[:20]:
            print(f"      missing: {k}")
    if oversize:
        reason = s3_unavailable_reason()
        print(f"  ⚠️ over wrangler's 300 MiB limit: {len(oversize)} "
              f"({'S3 fallback ready' if not reason else 'S3 fallback NOT configured'})")
        for k in oversize:
            print(f"      oversize: {k} ({local[k]['size'] / 1e6:.0f} MB)")

    if args.report_orphans:
        orphans = orphan_keys(local, load_record())
        print(f"  orphans in record with no local file (kept): {len(orphans)}")
        for o in orphans[:50]:
            print(f"    orphan: {o}")

    if args.limit:
        pending = pending[: args.limit]
        print(f"  --limit: uploading only the first {len(pending)}")

    if not args.commit:
        print("\nDRY RUN (default) — nothing uploaded. Re-run with --commit.")
        for k in pending[:20]:
            print(f"    would upload: {k} ({local[k]['size'] / 1e6:.1f} MB)")
        if len(pending) > 20:
            print(f"    ... and {len(pending) - 20} more")
        return 0

    # Everything already recorded, plus this run's successes. A failure stays
    # out, so the next run retries exactly it. ⚠️ Entries whose local file has
    # gone are KEPT — the object is still in R2 (see orphan_keys).
    recorded = dict(record)
    for key in touched:
        recorded[key] = record_entry(local[key], fresh_digests[key] or record.get(key, {}).get("sha256"))

    if not pending:
        write_record(recorded)
        print("Nothing to upload. Record refreshed.")
        return 0

    # Smallest first: the cheap ones land and checkpoint before the whales, so
    # an interruption leaves the largest possible count done.
    pending.sort(key=lambda k: local[k]["size"])

    uploaded: List[str] = []
    failed: List[Tuple[str, str]] = []
    started = time.time()
    since_checkpoint = 0
    done_bytes = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(upload_one, k, local[k]["src"], local[k]["size"]): k for k in pending}
        for i, fut in enumerate(as_completed(futures), 1):
            key, ok, detail = fut.result()
            if ok:
                uploaded.append(key)
                done_bytes += local[key]["size"]
                digest = fresh_digests.get(key) or sha256_of(local[key]["src"])
                fresh_digests[key] = digest
                recorded[key] = record_entry(local[key], digest)
                since_checkpoint += 1
                if since_checkpoint >= CHECKPOINT_EVERY:
                    write_record(recorded)
                    since_checkpoint = 0
                mins = (time.time() - started) / 60
                rate = done_bytes / 1e6 / max(1e-6, mins * 60)
                print(f"  [{i}/{len(pending)}] {key}  "
                      f"({local[key]['size'] / 1e6:.1f} MB, {done_bytes / 1e9:.2f} GB done, "
                      f"{rate:.1f} MB/s, {mins:.1f} min)", flush=True)
            else:
                failed.append((key, detail))
                print(f"  [FAIL] {key}: {detail}", flush=True)

    write_record(recorded)

    mins = (time.time() - started) / 60
    print(f"\nUploaded {len(uploaded)} / {len(pending)} "
          f"({done_bytes / 1e9:.3f} GB) in {mins:.1f} min; {len(failed)} failed.")
    print(f"Record: {len(recorded)} objects, "
          f"{sum(int(v.get('size') or 0) for v in recorded.values()) / 1e9:.3f} GB.")
    if failed:
        print("\n::error::some ebooks failed to upload — re-run to retry just those:")
        for key, detail in failed:
            print(f"  - {key}: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
