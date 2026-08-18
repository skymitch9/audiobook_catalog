#!/usr/bin/env python3
"""Ingest AUDIOBOOK FILES into the private `estate-audio` R2 bucket.

AUDIO PLAYER PHASE 0b. Sibling of `scripts/upload_ebooks_r2.py`, and it reuses
that script's `decide()` / `sha256_of()` rather than re-deriving them — one
implementation of "has this file changed since we uploaded it".

⚠️ THERE IS NO BULK MODE, AND THAT IS THE FEATURE
--------------------------------------------------
The owner's ingest decision (audio-player-design.md §12 decision 3) is
**on-demand**: *"upon clicking the download button it adds it to a queue to be
downloaded for everyone. so each book is on request then ready for everyone."*
The library is **630 GB / 1,073 files** (MEASURED 2026-08-17); uploading it
whole would be days of uplink and ~$9.45/mo for books nobody asked for.

So this script only ever uploads files it was NAMED. It takes explicit paths,
or `--anchor` values, or `--title` values — never "everything". If you find
yourself wanting a `--all`, the thing you actually want is
`app/tools/fulfill_audio_requests.py`, which uploads what people asked for.

THE 300 MiB WALL IS THE COMMON CASE HERE, NOT THE EXCEPTION
-----------------------------------------------------------
`upload_ebooks_r2.py` measured `wrangler r2 object put` refusing files over
300 MiB, and routed exactly one of 168 ebooks around it. For audio, **889 of
1,073 files are over that line** (MEASURED 2026-08-17; mean file 601 MB,
largest 3.92 GB). So wrangler is not a backend here at all: **boto3 multipart
through R2's S3-compatible endpoint is THE path**, for every file regardless
of size. One code path, exercised on every upload, rather than a rare branch
that is only discovered to be broken on the day a big book is requested.

Credentials (already in this repo's `.env`, same three the ebook uploader
uses — an R2 API token with Object Read & Write):

    R2_ACCOUNT_ID  R2_ACCESS_KEY_ID  R2_SECRET_ACCESS_KEY

THE KEY SCHEME — ⚠️ CHANGING IT IS A MIGRATION, NOT AN EDIT
------------------------------------------------------------
**The object key is the file's path relative to the library root, verbatim**,
with `\\` folded to `/`. No prefix, no hash, no re-encoding:

    C:\\Users\\...\\books\\Brandon Sanderson\\Skyward.m4b
    ->  key "Brandon Sanderson/Skyward.m4b"

Identical to `estate-ebooks`, and for the same reasons: the anchor is a hash
OF this path so it adds no uniqueness; the Worker must load the manifest to
authorise anyway, so `anchor -> path -> key` is a dictionary lookup either
way; and a human debugging a missing book in the Cloudflare dashboard needs to
read `Brandon Sanderson/Skyward.m4b`, not `b-a49cd096d824`.

⚠️ This key is PERSISTED — objects already in the bucket are stored under it
and the phase-1 Worker will resolve `anchor -> path -> object key` assuming
the last arrow is the identity function. Changing `object_key()` means
re-uploading every object that exists. `tests/test_upload_audio_r2.py` pins it
with golden fixtures so the mutation cannot pass silently.

⚠️ Re-filing a book changes its path, hence its anchor, hence its key. The old
object is never deleted by this script (see `evict_keys` in
`app/tools/fulfill_audio_requests.py` for the only thing that does delete).

THE RECORD
----------
`site/audio_manifest.json` — what is streamable, and since when. ⚠️ GITIGNORED
on purpose: it lists the household's books by filename, which is exactly the
scraping surface `site/ebooks.json` was gitignored to close (this repo is
PUBLIC and must stay public). The Worker will read the manifest from a gated
bucket, never from git.

Per-object shape, and the last two fields are the eviction contract:

    "Brandon Sanderson/Skyward.m4b": {
      "anchor": "b-…", "title": "Skyward", "bookId": "skyward",
      "size": 402653184, "mtime_ns": …, "sha256": null,
      "streamable": true,
      "since": "2026-08-17T21:03:11Z",     # first became streamable
      "uploaded_at": "2026-08-17T21:03:11Z",
      "last_stream_at": null,   # ⚠️ phase 2 writes these. Until then the
      "last_position_at": null  #    eviction pass refuses to delete anything.
    }

USAGE
-----
    python -m scripts.upload_audio_r2 "Brandon Sanderson/Skyward.m4b"
    python -m scripts.upload_audio_r2 --title "Skyward" --commit
    python -m scripts.upload_audio_r2 --anchor b-a49cd096d824 --commit
    python -m scripts.upload_audio_r2 --list-record

`--dry-run` is the DEFAULT: nothing is uploaded without `--commit`.
Exit 0 = every named file is in the bucket. Exit 1 = at least one is not.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.config import EXTS, ROOT_DIR, SITE_DIR
from app.core.review_join import book_id_from_title
from app.metadata import walk_library

# ⚠️ Reused, not re-derived. `decide()` is the two-tier "did this file change"
# rule (size+mtime, then sha256 as the authority) that the ebook ingest already
# paid to get right, and it is pure.
from scripts.upload_ebooks_r2 import decide, sha256_of

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RECORD_PATH = SITE_DIR / "audio_manifest.json"

BUCKET = os.getenv("AUDIO_R2_BUCKET", "estate-audio")

# ⚠️ `audio/mp4`, never `audio/x-m4b` and never `application/octet-stream`.
# An .m4b is structurally an .m4a — AAC in an MPEG-4 container — and browsers
# key playback behaviour off the type. See audio-player-design.md §7.2.
CONTENT_TYPE = "audio/mp4"
# Never a shared cache: these bodies are served per-person behind a bearer.
CACHE_CONTROL = "private, max-age=0, no-store"
# A player, not a download button.
CONTENT_DISPOSITION = "inline"

# 64 MiB parts: R2 caps a multipart upload at 10,000 parts, so this ceilings at
# 640 GB per object against a 3.92 GB largest file — ample, and big enough that
# a 601 MB mean book is ~10 parts rather than hundreds of round trips.
S3_MULTIPART_CHUNK = 64 * 1024 * 1024
S3_MAX_CONCURRENCY = 4

S3_ENV = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")


# ---------------------------------------------------------------------------
# pure helpers — pinned by tests/test_upload_audio_r2.py
# ---------------------------------------------------------------------------
def object_key(rel_path: str) -> str:
    """The R2 object key for one library-relative path: the path, verbatim.

    ⚠️ THE KEY SCHEME. See the module docstring — changing this is a migration.
    Normalised only for separators (this pipeline runs on Windows) and a
    leading slash. No case folding, no prefix, no URL-encoding.
    """
    key = str(rel_path or "").replace("\\", "/").lstrip("/")
    if not key:
        raise ValueError(f"cannot build an object key from {rel_path!r}")
    return key


def audio_anchor(rel_path: str) -> str:
    """`"b-" + sha256(relative path)[:12]` — the SAME fold as `ebook_anchor`.

    ⚠️ Deliberately the same function, imported rather than re-typed, so the
    two shelves can never drift into two answers for "what is this file
    called". The estate has already shipped that class of drift once.
    """
    from scripts.build_ebook_manifest import ebook_anchor
    return ebook_anchor(object_key(rel_path))


def rel_key_for(path: Path, root: Path = None) -> str:
    """Library-relative object key for an absolute file path."""
    root = root or ROOT_DIR
    return object_key(str(Path(path).resolve().relative_to(Path(root).resolve())))


def s3_unavailable_reason() -> Optional[str]:
    """None if the upload can run; otherwise the exact thing to fix.

    ⚠️ There is no second backend to fall back to (see the module docstring),
    so this is a hard stop rather than a routing hint.
    """
    missing = [n for n in S3_ENV if not os.getenv(n)]
    if missing:
        return (
            f"R2 S3 credentials are not configured (missing env: {', '.join(missing)}). "
            f"Mint an R2 API token with Object Read & Write on {BUCKET} (Cloudflare "
            "dashboard -> R2 -> Manage R2 API Tokens), put those three vars in .env, "
            "`pip install boto3`, and re-run."
        )
    try:
        import boto3  # noqa: F401
    except ImportError:
        return "boto3 is not installed (`pip install boto3`) and it is the only upload path"
    return None


def s3_client():
    """A boto3 S3 client pointed at this account's R2 endpoint."""
    reason = s3_unavailable_reason()
    if reason:
        raise SystemExit(reason)
    import boto3
    from botocore.config import Config as BotoConfig

    account = os.environ["R2_ACCOUNT_ID"]
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=BotoConfig(retries={"max_attempts": 3, "mode": "standard"}),
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# the record
# ---------------------------------------------------------------------------
def load_record() -> Dict[str, dict]:
    if not RECORD_PATH.exists():
        return {}
    try:
        return json.loads(RECORD_PATH.read_text(encoding="utf-8")).get("files", {}) or {}
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[WARN] {RECORD_PATH} unreadable ({exc}) — treating every file as absent")
        return {}


def write_record(files: Dict[str, dict]) -> None:
    payload = {
        "_comment": (
            "Audiobook FILES in the private Cloudflare R2 bucket 'estate-audio'. Object "
            "key = the library-relative path, verbatim. Generated by "
            "scripts/upload_audio_r2.py — do not hand-edit. ⚠️ GITIGNORED on purpose: it "
            "lists the household's books by filename, the surface site/ebooks.json was "
            "gitignored to close (this repo is PUBLIC). Ingest is ON-DEMAND — a book "
            "absent here was never requested, not lost."
        ),
        "bucket": BUCKET,
        "generated": now_iso(),
        "count": len(files),
        "streamable": sum(1 for v in files.values() if v.get("streamable")),
        "total_bytes": sum(int(v.get("size") or 0) for v in files.values()),
        "files": {k: files[k] for k in sorted(files)},
    }
    RECORD_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RECORD_PATH.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=1, ensure_ascii=False, sort_keys=False)
        fh.write("\n")


def record_entry(key: str, meta: dict, digest: Optional[str],
                 previous: Optional[dict] = None) -> dict:
    """One manifest row. `since` is FIRST-streamable and never moves on re-upload.

    ⚠️ `last_stream_at` / `last_position_at` are carried forward untouched.
    Nothing writes them yet (phase 2), and the eviction pass reads them — so a
    re-upload must not look like fresh access, and must not erase real access
    data either.
    """
    previous = previous or {}
    stamp = now_iso()
    return {
        "anchor": meta.get("anchor") or audio_anchor(key),
        "title": meta.get("title") or previous.get("title"),
        "bookId": meta.get("bookId") or previous.get("bookId"),
        "size": meta.get("size"),
        "mtime_ns": meta.get("mtime_ns"),
        "sha256": digest if digest is not None else previous.get("sha256"),
        "streamable": True,
        "since": previous.get("since") or stamp,
        "uploaded_at": stamp,
        "last_stream_at": previous.get("last_stream_at"),
        "last_position_at": previous.get("last_position_at"),
    }


# ---------------------------------------------------------------------------
# resolving what to upload — explicit only
# ---------------------------------------------------------------------------
def scan_library(root: Path = None) -> Dict[str, Path]:
    """`{object key: absolute path}` for every audio file on disk.

    A directory walk, not an upload plan. It exists so `--anchor` and
    `--title` can be resolved to a file; nothing here uploads anything.
    """
    root = Path(root or ROOT_DIR)
    return {rel_key_for(p, root): p for p in walk_library(root, EXTS)}


def resolve_targets(paths: List[str], anchors: List[str], titles: List[str],
                    root: Path = None) -> Tuple[Dict[str, Path], List[str]]:
    """Named files -> `({key: path}, unresolved)`. NEVER the whole library.

    Three ways to name a book, all explicit:
      * a path — absolute, or relative to the library root;
      * `--anchor b-…` — the manifest/Worker identity;
      * `--title "…"` — the m4b `©nam` title, folded through
        `book_id_from_title` so a request keyed on the site's `bookId`
        (which is how `audio_requests` docs are keyed) matches.
    """
    root = Path(root or ROOT_DIR)
    library = scan_library(root)
    found: Dict[str, Path] = {}
    unresolved: List[str] = []

    for raw in paths or []:
        candidate = Path(raw)
        key = None
        if candidate.is_absolute() and candidate.exists():
            try:
                key = rel_key_for(candidate, root)
            except ValueError:
                key = None
        if key is None:
            probe = object_key(raw)
            key = probe if probe in library else None
        if key and key in library:
            found[key] = library[key]
        else:
            unresolved.append(raw)

    if anchors:
        by_anchor = {audio_anchor(k): k for k in library}
        for a in anchors:
            key = by_anchor.get(a.strip())
            if key:
                found[key] = library[key]
            else:
                unresolved.append(a)

    if titles:
        # Title -> file needs the m4b tag, which is what chapters.json and the
        # catalog are both keyed on. The tag cache means this costs a stat,
        # not a file open, for everything already seen.
        from app.tools.extract_chapters import (TAG_CACHE_PATH, load_json,
                                                read_tags_cached,
                                                save_json_with_retry)
        cache = load_json(TAG_CACHE_PATH, {})
        by_book_id: Dict[str, str] = {}
        for key, path in library.items():
            tag_title, _author = read_tags_cached(path, cache)
            if tag_title:
                by_book_id.setdefault(book_id_from_title(tag_title), key)
        save_json_with_retry(cache, TAG_CACHE_PATH)
        for t in titles:
            key = by_book_id.get(book_id_from_title(t))
            if key:
                found[key] = library[key]
            else:
                unresolved.append(t)

    return found, unresolved


# ---------------------------------------------------------------------------
# the upload
# ---------------------------------------------------------------------------
def upload_via_s3(key: str, src: Path, client=None) -> Tuple[bool, str]:
    """Multipart PUT through R2's S3-compatible endpoint. THE upload path.

    boto3 rather than a hand-rolled SigV4 signer on purpose: multipart plus
    request signing is exactly the kind of code that is silently wrong until
    the day it matters, and `upload_file` chunks, retries per part, and aborts
    a failed upload so no orphaned parts are billed.
    """
    from boto3.s3.transfer import TransferConfig

    client = client or s3_client()
    cfg = TransferConfig(
        multipart_threshold=S3_MULTIPART_CHUNK,
        multipart_chunksize=S3_MULTIPART_CHUNK,
        max_concurrency=S3_MAX_CONCURRENCY,
    )
    try:
        client.upload_file(
            str(src), BUCKET, key,
            ExtraArgs={
                "ContentType": CONTENT_TYPE,
                "ContentDisposition": CONTENT_DISPOSITION,
                "CacheControl": CACHE_CONTROL,
            },
            Config=cfg,
        )
    except Exception as exc:  # noqa: BLE001 — every failure is the same answer
        return False, f"S3 multipart failed: {type(exc).__name__}: {exc}"
    return True, ""


def upload_keys(targets: Dict[str, Path], force: bool = False,
                titles: Dict[str, str] = None, client=None) -> Tuple[List[str], List[Tuple[str, str]]]:
    """Upload the named files, refresh the record. Returns (uploaded, failed).

    Idempotent: a file whose size+mtime match the record is skipped without
    hashing; a touched-but-identical file costs one sha256 rather than 601 MB
    of uplink. `--force` bypasses both tiers.
    """
    titles = titles or {}
    record = load_record()
    client = client or s3_client()
    uploaded: List[str] = []
    failed: List[Tuple[str, str]] = []

    for key in sorted(targets):
        src = targets[key]
        st = src.stat()
        meta = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
        verdict, digest = decide(key, meta, None if force else record.get(key), force,
                                 lambda _k: sha256_of(src))
        title = titles.get(key)
        meta.update({
            "anchor": audio_anchor(key),
            "title": title,
            "bookId": book_id_from_title(title) if title else None,
        })
        if verdict == "skip":
            print(f"  [have] {key} ({st.st_size / 1e6:.0f} MB) — already streamable")
            record[key] = record_entry(key, meta, digest, record.get(key))
            continue
        print(f"  [up  ] {key} ({st.st_size / 1e6:.0f} MB) — multipart…", flush=True)
        ok, detail = upload_via_s3(key, src, client=client)
        if ok:
            uploaded.append(key)
            record[key] = record_entry(key, meta, digest, record.get(key))
            write_record(record)  # checkpoint per object: each one is expensive to redo
            print(f"  [ok  ] {key}", flush=True)
        else:
            failed.append((key, detail))
            print(f"  [FAIL] {key}: {detail}", flush=True)

    write_record(record)
    return uploaded, failed


# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="file paths (absolute, or relative to the library root)")
    ap.add_argument("--anchor", action="append", default=[], help="upload the book with this anchor (repeatable)")
    ap.add_argument("--title", action="append", default=[], help="upload the book with this m4b title (repeatable)")
    ap.add_argument("--commit", action="store_true", help="actually upload (default is a dry run)")
    ap.add_argument("--force", action="store_true", help="re-upload even if the record says it is there")
    ap.add_argument("--list-record", action="store_true", help="print what the bucket is recorded as holding")
    args = ap.parse_args(argv)

    if args.list_record:
        record = load_record()
        streamable = {k: v for k, v in record.items() if v.get("streamable")}
        print(f"Bucket   : {BUCKET}")
        print(f"Record   : {RECORD_PATH}")
        print(f"Objects  : {len(record)} ({len(streamable)} streamable, "
              f"{sum(int(v.get('size') or 0) for v in record.values()) / 1e9:.3f} GB)")
        for k, v in sorted(streamable.items()):
            print(f"  {k}  ({int(v.get('size') or 0) / 1e6:.0f} MB, since {v.get('since')})")
        return 0

    if not (args.paths or args.anchor or args.title):
        # ⚠️ NOT a usage error to correct with a --all. See the module docstring:
        # ingest is on-demand by owner decision, so "nothing named" is "nothing
        # to do", and the queue is the thing that names books.
        ap.error("name at least one book (a path, --anchor or --title). "
                 "There is no bulk mode — ingest is on-demand by design.")

    print(f"Library  : {ROOT_DIR}")
    print(f"Bucket   : {BUCKET}")
    targets, unresolved = resolve_targets(args.paths, args.anchor, args.title)
    for u in unresolved:
        print(f"  ⚠️ not found in the library: {u}")
    if not targets:
        print("Nothing resolved to a file on disk.")
        return 1

    total = sum(t.stat().st_size for t in targets.values())
    print(f"  named    : {len(targets)} file(s), {total / 1e9:.3f} GB")

    if not args.commit:
        print("\nDRY RUN (default) — nothing uploaded. Re-run with --commit.")
        for key, src in sorted(targets.items()):
            print(f"    would upload: {key} ({src.stat().st_size / 1e6:.0f} MB)")
        return 0 if not unresolved else 1

    uploaded, failed = upload_keys(targets, force=args.force)
    print(f"\nUploaded {len(uploaded)} / {len(targets)}; {len(failed)} failed.")
    if failed or unresolved:
        for key, detail in failed:
            print(f"  - {key}: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
