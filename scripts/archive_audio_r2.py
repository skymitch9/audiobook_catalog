#!/usr/bin/env python3
"""DISASTER-RECOVERY ARCHIVE of the whole audiobook library into Cloudflare R2.

Owner's order, 2026-08-18, verbatim: *"do it, setup blob storage for all author
folders. we lose this data we lose it all and the server isnt ready yet."*

This is an OFF-SITE COPY OF THE MASTER, and that is a different job from every
other R2 uploader in this repo. `scripts/upload_audio_r2.py` puts a handful of
*requested* books in R2 as a streaming CACHE that can be evicted and re-uploaded
from disk at will. This script assumes the disk may be GONE. It mirrors, it does
not curate: every file under the library root goes up as-is, including the two
installer `.exe`s, the two `.epub.bak`s and a Windows `.lnk` shortcut, because
"we lose this data we lose it all" is an instruction to copy, not to judge.

⚠️⚠️ THE `archive/` PREFIX IS NOT A CACHE — NOTHING MAY EVER EVICT IT ⚠️⚠️
--------------------------------------------------------------------------
Everything this script writes lands under the key prefix ``archive/`` in the
``estate-audio`` bucket, and the *streaming* copies written by
`scripts/upload_audio_r2.py` land at the bucket root with no prefix. They share
a bucket and they are opposites:

    archive/<Author>/<file>     THE BACKUP. Deleting it can lose the only copy.
    <Author>/<file>             the streaming cache. Deleting it costs a re-upload.

`app/tools/fulfill_audio_requests.py` already holds the eviction machinery
(`evict_candidates` / `run_eviction`, 30-day idle, currently refusing for want
of access data). It iterates `site/audio_manifest.json`, which this script never
writes to, so it cannot reach these objects today — and it now also carries an
explicit belt-and-braces guard that refuses any key under ``archive/``.

⚠️ **If you are writing anything that DELETES from `estate-audio`, the rule is:
skip every key starting with `archive/`, unconditionally, with no flag to
override it.** A "clean up old objects" pass that does not know this rule is how
638 GB of irreplaceable library becomes a Cloudflare billing saving.

WHAT IS ARCHIVED
----------------
Everything under ``ROOT_DIR`` (``C:\\Users\\nbasl\\OpenAudible\\books``) except
``zzzz_Books_to_be_Converted\\``, which is excluded by standing rule — it is a
staging pile of part-files awaiting m4b assembly, not library content, and every
library sweep in this repo excludes it.

MEASURED 2026-08-18 (`--status` re-measures; this is the seed's starting point).
⚠️ Two unit conventions collide here and it is worth being explicit, because a
7% discrepancy between two honest measurements reads like a bug: this script
prints **decimal GB** (10^9, what Cloudflare bills in), Windows Explorer and
PowerShell's ``/1GB`` print **GiB** (2^30).

    included   1,260 files   684.98 GB  =  637.93 GiB
    of which   1,073 .m4b    675.9 GB   (largest single file 4.11 GB)
               138 .epub, 30 .pdf, 7 .zip, 6 .mp4, 2 .exe, 2 .bak, 1 .kfx-zip, 1 .lnk
    excluded   117 files      69.3 GB   (zzzz_Books_to_be_Converted)

At R2's $0.015 per GB-month that is **~$10.3/month** to hold, and R2 charges no
egress, so a full restore costs nothing but time.

THE KEY SCHEME — ⚠️ CHANGING IT IS A MIGRATION, NOT AN EDIT
------------------------------------------------------------
``"archive/" + <path relative to the library root>``, separators folded to
``/``, nothing else touched:

    C:\\Users\\nbasl\\OpenAudible\\books\\Brandon Sanderson\\Skyward.m4b
      ->  archive/Brandon Sanderson/Skyward.m4b

No hashing, no re-encoding, no case folding. A human staring at the Cloudflare
dashboard during a restore needs to read an author and a title, and a restore
run with rclone needs the tree to come back in the shape it left. The 15 files
that sit at the library root with no author folder key as ``archive/<file>``.
`tests/test_archive_audio_r2.py` pins this with golden fixtures.

WHY boto3 MULTIPART AND NOT wrangler
------------------------------------
``wrangler r2 object put`` refuses files over 300 MiB (measured 2026-08-17, see
`scripts/upload_ebooks_r2.py`). **852 of these 1,260 files are over that line**
and the largest is 4.11 GB, so wrangler is not a candidate transport for this
job at all. boto3's ``upload_file`` chunks, signs, retries per part, and aborts
the multipart upload on failure so no orphaned parts accrue storage charges.
Parts are 100 MiB: R2 caps a multipart upload at 10,000 parts, which ceilings
this at ~976 GB per object against a 4.11 GB largest file.

Credentials come from ``.env`` via ``app.config``'s dotenv load — the same three
an R2 API token gives (``R2_ACCOUNT_ID`` / ``R2_ACCESS_KEY_ID`` /
``R2_SECRET_ACCESS_KEY``), MEASURED writing to ``estate-audio`` on 2026-08-18
(docs/info/book-ingestion.md §5), and re-probed under the ``archive/`` prefix
the same day (PUT -> GET byte-exact -> DELETE). ⚠️ They are never printed, never
passed in argv, and never logged.

CONTRACT
--------
* **``--dry-run`` is the DEFAULT.** Nothing uploads without ``--commit``.
* **Idempotent, by manifest.** ``output_files/audio_archive_manifest.json``
  records ``{path, size, mtime_ns, sha256, key, uploaded_at}`` per file. A file
  whose size AND mtime match the record is skipped without being read; anything
  else is hashed (the hash is the authority — a touched-but-identical file costs
  one sha256, not 600 MB of uplink) and uploaded if the digest moved.
* **Checkpointed per file.** The manifest is rewritten after every success, so
  a kill at any moment loses at most the file in flight.
* **Failures log and continue**, are recorded with their reason, and make the
  run exit non-zero. The next hourly run retries exactly them.
* **Never deletes anything, locally or in R2.** A file removed from disk keeps
  its object (an archive that forgets is not an archive); ``--status`` counts
  those separately as "orphans".
* **Single-flight**, on its own lock ``output_files/audio_archive.lock`` — NOT
  the pipeline's lock, so an archive run and a catalog run never block each
  other. Stale detection is PID-liveness first (reusing
  `app.core.pipeline_lock.pid_alive`, which knows why ``os.kill`` is unsafe on
  Windows) and an age ceiling second.
* **Bandwidth only.** No GPU, no ffmpeg, no Whisper — it can run beside the
  transcription chain and the nightly ingest window.

THE SEED IS A SCHEDULED TASK, NOT A COMMAND YOU BABYSIT
-------------------------------------------------------
``AudiobookArchiveR2`` fires HOURLY via
``scripts/archive_audio_r2_hidden.vbs`` -> ``scripts/archive_audio_r2.bat``.
Hourly + idempotent + single-flight means the ~638 GB seed survives reboots,
network drops and killed processes without anyone watching: a run that dies
picks up where it left off within the hour, and once the seed is done the same
task becomes the ongoing sync that carries newly-purchased books off-site.

USAGE
-----
    python -m scripts.archive_audio_r2                 # dry run (the default)
    python -m scripts.archive_audio_r2 --status        # progress, ETA, failures
    python -m scripts.archive_audio_r2 --commit        # upload (what the task runs)
    python -m scripts.archive_audio_r2 --commit --limit 3      # testing
    python -m scripts.archive_audio_r2 --commit --only "Skyward"
    python -m scripts.archive_audio_r2 --abort-multipart       # hygiene sweep

Exit 0 = nothing is outstanding or the run finished clean. Exit 1 = at least one
file failed (or the lock was held by a live run, which is normal for the hourly
task and reported as such with exit 0).
"""

from __future__ import annotations

import argparse
import ctypes  # noqa: F401  (imported by pipeline_lock's Windows probe path)
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from app.config import OUTPUT_DIR, ROOT_DIR

# ⚠️ Reused, not re-derived. `pid_alive` is the Windows-safe process-liveness
# probe the pipeline lock already paid to get right (`os.kill(pid, 0)` on
# Windows can TERMINATE an unrelated process that reused the pid). The LOCK
# FILE is separate on purpose; only the decision is shared.
from app.core.pipeline_lock import pid_alive

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------
BUCKET = os.getenv("AUDIO_R2_BUCKET", "estate-audio")

# ⚠️ THE ARCHIVE PREFIX. See the module docstring: nothing may ever evict from
# under it. Every key this script writes starts here.
ARCHIVE_PREFIX = "archive/"

MANIFEST_PATH: Path = OUTPUT_DIR / "audio_archive_manifest.json"
LOCK_PATH: Path = OUTPUT_DIR / "audio_archive.lock"

# ⚠️ Standing rule (MEMORY: "Ignore zzzz_Books_to_be_Converted"): a staging pile
# of part-files awaiting m4b assembly. Excluded from every library sweep, never
# tidied, and NOT archived — the assembled m4b is what matters and it lands in
# an author folder. Compared case-folded against every path segment.
EXCLUDED_DIR_NAMES = {"zzzz_books_to_be_converted"}

# 100 MiB parts. R2 caps a multipart upload at 10,000 parts -> ~976 GB ceiling
# per object, against a 4.11 GB largest file. Big enough that a 600 MB mean book
# is 6 parts rather than hundreds of round trips.
PART_SIZE = 100 * 1024 * 1024
MAX_CONCURRENCY = 4

# A lock older than this with a LIVE holder is assumed wedged. Generous, because
# a single 4 GB file on a household uplink is legitimately an hour or more and a
# whole run is legitimately days.
STALE_LOCK_HOURS = 12.0

# Orphaned multipart uploads (a process killed mid-file) are billed as storage
# until aborted. Anything older than this cannot belong to a live run.
STALE_MULTIPART_HOURS = 6.0

CONTENT_TYPES = {
    ".m4b": "audio/mp4",
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
    ".mp3": "audio/mpeg",
    ".epub": "application/epub+zip",
    ".pdf": "application/pdf",
    ".zip": "application/zip",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}
DEFAULT_CONTENT_TYPE = "application/octet-stream"

S3_ENV = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# pure helpers — pinned by tests/test_archive_audio_r2.py
# ---------------------------------------------------------------------------
def normalise_rel(rel_path: str) -> str:
    """A library-relative path in canonical form: ``/`` separators, no leading
    slash, no ``./``. The manifest is keyed on this."""
    rel = str(rel_path or "").replace("\\", "/").lstrip("/")
    while rel.startswith("./"):
        rel = rel[2:]
    if not rel:
        raise ValueError(f"cannot build an archive key from {rel_path!r}")
    return rel


def is_excluded(rel_path: str) -> bool:
    """True for anything under an excluded directory.

    ⚠️ Matches a whole DIRECTORY segment, case-folded — never a substring and
    never the filename. So a real book called
    ``Zzzz Books to be Converted - A Memoir.m4b`` sitting in an author folder is
    archived, while the staging directory is excluded wherever it sits in the
    tree (today it is at the root; that is not a property anyone maintains).
    """
    try:
        parts = normalise_rel(rel_path).split("/")
    except ValueError:
        return False
    return any(p.casefold() in EXCLUDED_DIR_NAMES for p in parts[:-1])


def archive_key(rel_path: str) -> str:
    """The R2 object key for one library-relative path.

    ⚠️ THE KEY SCHEME — see the module docstring. ``archive/`` + the path,
    verbatim. Changing this orphans every object already uploaded and means
    re-sending 638 GB, so it is a migration, not an edit.
    """
    return ARCHIVE_PREFIX + normalise_rel(rel_path)


def content_type_for(rel_path: str) -> str:
    return CONTENT_TYPES.get(Path(rel_path).suffix.lower(), DEFAULT_CONTENT_TYPE)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def decide(meta: dict, recorded: Optional[dict], force: bool, hasher) -> Tuple[str, Optional[str]]:
    """Upload this file, or not? Returns ``(verdict, sha256-or-None)``.

    Verdicts: ``"upload"`` | ``"skip"``. Two tiers, and the cheap one exists so
    an hourly task does not read 638 GB off disk to learn that nothing changed:

      1. size AND mtime_ns both match the record -> skip, without reading.
      2. otherwise hash. The digest matching the record -> skip anyway (the file
         was touched, re-tagged to the same bytes, or copied back), and the
         caller refreshes mtime so tier 1 catches it next time.

    ⚠️ The HASH is the authority; mtime may only ever say "skip" faster, never
    "upload" on its own.
    """
    if force or not recorded:
        return "upload", None
    if recorded.get("size") == meta["size"] and recorded.get("mtime_ns") == meta.get("mtime_ns"):
        return "skip", recorded.get("sha256")
    digest = hasher()
    if recorded.get("sha256") == digest:
        return "skip", digest
    return "upload", digest


def manifest_entry(rel: str, meta: dict, digest: Optional[str]) -> dict:
    return {
        "path": rel,
        "size": meta["size"],
        "mtime_ns": meta.get("mtime_ns"),
        "sha256": digest,
        "key": archive_key(rel),
        "uploaded_at": now_iso(),
    }


def orphan_paths(local: Dict[str, dict], record: Dict[str, dict]) -> List[str]:
    """Recorded files that are no longer on disk.

    ⚠️ Their objects are KEPT. This is an archive: a file vanishing locally is
    the exact event the archive exists for, so its copy is the last one and
    deleting it would be the failure mode, not the cleanup.
    """
    return sorted(set(record) - set(local))


def observed_rate_bps(record: Dict[str, dict], sample: int = 60) -> Optional[float]:
    """Bytes/sec across the most recent uploads, from their own timestamps.

    Measured rather than assumed, and honest about not knowing: fewer than two
    recorded uploads, or a zero elapsed span, returns None and the caller says
    "unknown" instead of printing a fabricated ETA.
    """
    stamped = []
    for entry in record.values():
        try:
            ts = datetime.strptime(str(entry.get("uploaded_at")), "%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, TypeError):
            continue
        stamped.append((ts.replace(tzinfo=timezone.utc).timestamp(), int(entry.get("size") or 0)))
    if len(stamped) < 2:
        return None
    stamped.sort()
    window = stamped[-sample:]
    span = window[-1][0] - window[0][0]
    if span <= 0:
        return None
    # The first sample's bytes landed BEFORE the window opened, so they are not
    # part of what the window measures.
    return sum(size for _ts, size in window[1:]) / span


def human_eta(seconds: float) -> str:
    if seconds < 3600:
        return f"{seconds / 60:.0f} min"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} days"


# ---------------------------------------------------------------------------
# the local scan
# ---------------------------------------------------------------------------
def iter_library(root: Path) -> Iterator[Tuple[str, os.stat_result]]:
    """Every file under ``root`` that is not excluded, as ``(rel, stat)``.

    ⚠️ EVERY file — not just ``app.config.EXTS``. This is a mirror of the disk,
    so the epubs, pdfs, zips, the two installer exes and the stray .lnk all go
    up. Curating an archive is how you discover, on the day you need it, that
    the one thing you skipped mattered.
    """
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune excluded directories in place so os.walk never descends into
        # them — cheaper than filtering 117 files out afterwards, and it means
        # a huge staging pile costs nothing to skip.
        dirnames[:] = [d for d in dirnames if d.casefold() not in EXCLUDED_DIR_NAMES]
        for name in filenames:
            full = Path(dirpath) / name
            try:
                rel = normalise_rel(str(full.relative_to(root)))
            except (ValueError, OSError):
                continue
            if is_excluded(rel):
                continue
            try:
                st = full.stat()
            except OSError:
                continue
            yield rel, st


def scan_local(root: Path) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for rel, st in iter_library(root):
        out[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "src": Path(root) / rel}
    return out


# ---------------------------------------------------------------------------
# manifest IO
# ---------------------------------------------------------------------------
def load_manifest() -> Tuple[Dict[str, dict], Dict[str, dict]]:
    """``(files, failures)``. A corrupt manifest is a WARNING, never a crash —
    the worst it costs is re-uploading, and a run that refuses to start because
    a JSON file was truncated mid-write is a run that stops archiving."""
    if not MANIFEST_PATH.exists():
        return {}, {}
    try:
        raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        return raw.get("files", {}) or {}, raw.get("failures", {}) or {}
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[WARN] {MANIFEST_PATH} unreadable ({exc}) — treating every file as new")
        return {}, {}


def write_manifest(files: Dict[str, dict], failures: Dict[str, dict]) -> None:
    payload = {
        "_comment": (
            "DISASTER-RECOVERY ARCHIVE of the audiobook library in Cloudflare R2 bucket "
            f"'{BUCKET}' under the '{ARCHIVE_PREFIX}' prefix. Object key = "
            f"'{ARCHIVE_PREFIX}' + the library-relative path, verbatim. Generated by "
            "scripts/archive_audio_r2.py -- do not hand-edit. "
            "\u26a0\ufe0f NOTHING MAY EVER EVICT FROM THE archive/ PREFIX: these objects are "
            "the off-site copy of the master, not a streaming cache. "
            "\u26a0\ufe0f output_files/ is gitignored, so this record is LOCAL ONLY -- which is "
            "also correct, since it lists the household's books by filename and this repo "
            "is PUBLIC. \u26a0\ufe0f Losing this file does NOT lose the archive, but it does cost "
            "a full re-upload: the skip check is manifest-driven, so with no record every "
            "file looks new. It is the one output_files/ artefact worth copying somewhere "
            "safe."
        ),
        "bucket": BUCKET,
        "prefix": ARCHIVE_PREFIX,
        "library_root": str(ROOT_DIR),
        "generated": now_iso(),
        "count": len(files),
        "total_bytes": sum(int(v.get("size") or 0) for v in files.values()),
        "failure_count": len(failures),
        "files": {k: files[k] for k in sorted(files)},
        "failures": {k: failures[k] for k in sorted(failures)},
    }
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=1, ensure_ascii=False, sort_keys=False)
        fh.write("\n")
    # Atomic-ish replace: a kill mid-write leaves the previous good manifest
    # rather than a truncated one, and this file is rewritten after every
    # single upload.
    tmp.replace(MANIFEST_PATH)


# ---------------------------------------------------------------------------
# the lock — its own file, PID-aware
# ---------------------------------------------------------------------------
class ArchiveLockHeld(Exception):
    def __init__(self, holder: dict):
        self.holder = holder
        super().__init__(
            f"archive lock held by pid {holder.get('pid')} on {holder.get('host')} "
            f"since {holder.get('started_at')}"
        )


def _read_lock() -> Optional[dict]:
    try:
        return json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _lock_age_hours(holder: Optional[dict]) -> float:
    started = (holder or {}).get("started_at")
    if started:
        try:
            ts = datetime.strptime(str(started), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
        except (ValueError, TypeError):
            pass
    try:
        return (time.time() - LOCK_PATH.stat().st_mtime) / 3600.0
    except OSError:
        return float("inf")


def lock_is_stale(holder: Optional[dict]) -> bool:
    """⚠️ PID liveness FIRST, age only as a backstop.

    An mtime-only stale check is wrong in both directions here: a run uploading
    one 4 GB file touches nothing for an hour and would be declared dead, while
    a crashed run's lock stays "fresh" for the whole ceiling. The pid check
    reclaims a crashed run's lock in seconds and never reclaims a live one.
    """
    if holder is None:
        return _lock_age_hours(None) >= STALE_LOCK_HOURS
    pid = holder.get("pid")
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return True
    if not pid_alive(pid):
        return True
    return _lock_age_hours(holder) >= STALE_LOCK_HOURS


class ArchiveLock:
    """Single-flight lock. Also the live status board: the in-flight file is
    written into it, so ``--status`` from another shell can say what is
    uploading right now without touching the uploader."""

    def __init__(self) -> None:
        self._acquired = False
        self._payload: dict = {}

    def acquire(self) -> "ArchiveLock":
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(10):
            try:
                fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                holder = _read_lock()
                if not lock_is_stale(holder):
                    raise ArchiveLockHeld(holder or {})
                try:
                    LOCK_PATH.unlink()
                except FileNotFoundError:
                    pass
                continue
            else:
                self._payload = {
                    "pid": os.getpid(),
                    "host": os.getenv("COMPUTERNAME") or "unknown",
                    "started_at": now_iso(),
                    "current_file": None,
                    "done_this_run": 0,
                    "bytes_this_run": 0,
                }
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self._payload, fh)
                self._acquired = True
                return self
        raise RuntimeError(f"gave up acquiring {LOCK_PATH} — investigate by hand")

    def heartbeat(self, **fields) -> None:
        """Best-effort. A failed status write must never stop an upload."""
        if not self._acquired:
            return
        self._payload.update(fields)
        self._payload["heartbeat_at"] = now_iso()
        try:
            LOCK_PATH.write_text(json.dumps(self._payload), encoding="utf-8")
        except OSError:
            pass

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            LOCK_PATH.unlink()
        except FileNotFoundError:
            pass
        self._acquired = False

    def __enter__(self) -> "ArchiveLock":
        return self.acquire()

    def __exit__(self, *_exc) -> None:
        self.release()


# ---------------------------------------------------------------------------
# R2
# ---------------------------------------------------------------------------
def s3_unavailable_reason() -> Optional[str]:
    """None if uploads can run; otherwise the exact thing to fix. There is no
    second backend — wrangler cannot carry files this size."""
    missing = [n for n in S3_ENV if not os.getenv(n)]
    if missing:
        return (
            f"R2 S3 credentials are not configured (missing env: {', '.join(missing)}). "
            f"They belong in this repo's .env — an R2 API token with Object Read & Write "
            f"on {BUCKET}."
        )
    try:
        import boto3  # noqa: F401
    except ImportError:
        return "boto3 is not installed (`pip install boto3`) and it is the only upload path"
    return None


def s3_client():
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
        config=BotoConfig(
            retries={"max_attempts": 5, "mode": "standard"},
            # A stalled socket on a household uplink must fail and be retried,
            # not hang an hourly task forever.
            connect_timeout=30,
            read_timeout=300,
        ),
    )


def abort_stale_multipart(client, older_than_hours: float = STALE_MULTIPART_HOURS) -> int:
    """Abort orphaned multipart uploads under ``archive/``.

    boto3 aborts its own multipart upload when ``upload_file`` raises, so this
    only ever finds the case boto3 cannot handle: the PROCESS was killed (task
    timeout, reboot, Ctrl-C at the wrong moment) and never got to clean up.
    Those parts are billed as storage until aborted, silently and forever.

    ⚠️ Scoped to the ``archive/`` prefix and to uploads older than the cutoff:
    aborting a *live* multipart upload — this run's own, or the streaming
    uploader's at the bucket root — would fail an upload in flight.
    """
    cutoff = datetime.now(timezone.utc).timestamp() - older_than_hours * 3600
    aborted = 0
    try:
        paginator = client.get_paginator("list_multipart_uploads")
        for page in paginator.paginate(Bucket=BUCKET, Prefix=ARCHIVE_PREFIX):
            for up in page.get("Uploads", []) or []:
                initiated = up.get("Initiated")
                if initiated and initiated.timestamp() > cutoff:
                    continue
                try:
                    client.abort_multipart_upload(
                        Bucket=BUCKET, Key=up["Key"], UploadId=up["UploadId"]
                    )
                    aborted += 1
                    print(f"  [abort] orphaned multipart: {up['Key']}")
                except Exception as exc:  # noqa: BLE001
                    print(f"  [WARN] could not abort {up.get('Key')}: {exc}")
    except Exception as exc:  # noqa: BLE001
        # Hygiene, not the job. A token without ListMultipartUploads must not
        # stop the archive from archiving.
        print(f"  [WARN] multipart sweep skipped: {type(exc).__name__}: {exc}")
    return aborted


def upload_one(client, rel: str, src: Path, size: int) -> Tuple[bool, str]:
    """Multipart PUT, then VERIFY the stored length. Returns ``(ok, detail)``.

    ⚠️ The HEAD is not ceremony. A silently short object is the one failure this
    archive could not survive discovering during a restore, and it costs one
    round trip per file to rule out. Byte-exactness beyond length is covered by
    the sha256 in the manifest plus R2's own per-part MD5 checks.
    """
    from boto3.s3.transfer import TransferConfig

    key = archive_key(rel)
    cfg = TransferConfig(
        multipart_threshold=PART_SIZE,
        multipart_chunksize=PART_SIZE,
        max_concurrency=MAX_CONCURRENCY,
    )
    try:
        client.upload_file(
            str(src),
            BUCKET,
            key,
            ExtraArgs={"ContentType": content_type_for(rel)},
            Config=cfg,
        )
    except Exception as exc:  # noqa: BLE001 — every failure has the same answer
        return False, f"upload failed: {type(exc).__name__}: {exc}"
    try:
        stored = client.head_object(Bucket=BUCKET, Key=key)["ContentLength"]
    except Exception as exc:  # noqa: BLE001
        return False, f"uploaded but HEAD failed: {type(exc).__name__}: {exc}"
    if int(stored) != int(size):
        return False, f"SIZE MISMATCH: local {size} != stored {stored}"
    return True, ""


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
def print_status() -> int:
    files, failures = load_manifest()
    local = scan_local(ROOT_DIR)
    done = {k: v for k, v in files.items() if k in local}
    remaining = [k for k in local if k not in files]
    total_bytes = sum(v["size"] for v in local.values())
    done_bytes = sum(int(files[k].get("size") or 0) for k in done)
    remaining_bytes = sum(local[k]["size"] for k in remaining)
    orphans = orphan_paths(local, files)

    print(f"Bucket     : {BUCKET}  (prefix {ARCHIVE_PREFIX!r} — NEVER evictable)")
    print(f"Library    : {ROOT_DIR}")
    print(f"Manifest   : {MANIFEST_PATH}")
    print(f"On disk    : {len(local)} files, {total_bytes / 1e9:.2f} GB")
    pct = (done_bytes / total_bytes * 100) if total_bytes else 0.0
    print(f"Archived   : {len(done)} files, {done_bytes / 1e9:.2f} GB  ({pct:.1f}% by bytes)")
    print(f"Remaining  : {len(remaining)} files, {remaining_bytes / 1e9:.2f} GB")
    if orphans:
        print(f"Orphans    : {len(orphans)} archived file(s) no longer on disk (objects KEPT)")
        for o in orphans[:10]:
            print(f"    {o}")

    rate = observed_rate_bps(files)
    if rate and remaining_bytes:
        print(f"Rate       : {rate / 1e6:.1f} MB/s observed over the last uploads")
        print(f"ETA        : ~{human_eta(remaining_bytes / rate)} at that rate")
    elif remaining_bytes:
        print("Rate       : unknown (fewer than two recorded uploads) — no ETA")

    holder = _read_lock()
    if holder:
        alive = pid_alive(int(holder.get("pid") or -1))
        state = "RUNNING" if alive else "stale (holder pid is dead; next run reclaims it)"
        print(f"Lock       : {state} — pid {holder.get('pid')}, started {holder.get('started_at')}")
        if holder.get("current_file"):
            print(f"  uploading now : {holder['current_file']}")
        if holder.get("heartbeat_at"):
            print(f"  last beat     : {holder['heartbeat_at']}  "
                  f"({holder.get('done_this_run', 0)} files / "
                  f"{int(holder.get('bytes_this_run') or 0) / 1e9:.2f} GB this run)")
    else:
        print("Lock       : free (no run in flight)")

    if failures:
        print(f"Failures   : {len(failures)} file(s) — retried on the next run")
        for k, v in list(sorted(failures.items()))[:10]:
            print(f"    {k}: {v.get('error')} (attempts {v.get('attempts')}, last {v.get('last_try')})")
    else:
        print("Failures   : none recorded")
    return 0


# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--commit", action="store_true", help="actually upload (default is a dry run)")
    ap.add_argument("--dry-run", action="store_true", help="explicit no-op — already the default")
    ap.add_argument("--status", action="store_true", help="progress, rate, ETA, failures; uploads nothing")
    ap.add_argument("--limit", type=int, default=0, help="upload at most N files (testing)")
    ap.add_argument("--only", default="", help="substring filter on the relative path (spot checks)")
    ap.add_argument("--force", action="store_true", help="re-upload even what the manifest records")
    ap.add_argument("--largest-first", action="store_true",
                    help="whales first (default is smallest-first: more files safe per interruption)")
    ap.add_argument("--max-hours", type=float, default=0.0,
                    help="stop starting new files after this long (0 = no limit; the hourly task uses 0)")
    ap.add_argument("--abort-multipart", action="store_true",
                    help="abort orphaned multipart uploads under archive/ and exit")
    args = ap.parse_args(argv)

    if args.status:
        return print_status()

    if args.abort_multipart:
        n = abort_stale_multipart(s3_client())
        print(f"Aborted {n} orphaned multipart upload(s) under {ARCHIVE_PREFIX!r}.")
        return 0

    local = scan_local(ROOT_DIR)
    # ⚠️ The library total is measured BEFORE --only narrows the run, so the
    # "% of the library" line at the end means the same thing in a spot check as
    # it does in a full run. It said "100.0% of the library" after a one-file
    # --only run before this was split out, which is exactly the kind of
    # comforting-and-false number a disaster-recovery report must not print.
    library_bytes = sum(v["size"] for v in local.values())
    if args.only:
        local = {k: v for k, v in local.items() if args.only.lower() in k.lower()}
    files, failures = load_manifest()
    record = {} if args.force else files

    total_bytes = sum(v["size"] for v in local.values())
    print(f"Library  : {ROOT_DIR}")
    print(f"Bucket   : {BUCKET}  prefix {ARCHIVE_PREFIX!r}")
    print(f"On disk  : {len(local)} files, {total_bytes / 1e9:.2f} GB "
          f"(zzzz_Books_to_be_Converted excluded)")
    print(f"Recorded : {len(files)} files, "
          f"{sum(int(v.get('size') or 0) for v in files.values()) / 1e9:.2f} GB")

    pending: List[str] = []
    digests: Dict[str, Optional[str]] = {}
    refreshed: List[str] = []
    for rel in sorted(local):
        meta = local[rel]
        verdict, digest = decide(meta, record.get(rel), args.force, lambda p=meta["src"]: sha256_of(p))
        digests[rel] = digest
        if verdict == "upload":
            pending.append(rel)
        elif digest and record.get(rel, {}).get("mtime_ns") != meta.get("mtime_ns"):
            refreshed.append(rel)

    pending.sort(key=lambda r: local[r]["size"], reverse=args.largest_first)
    pending_bytes = sum(local[r]["size"] for r in pending)
    print(f"To upload: {len(pending)} files, {pending_bytes / 1e9:.2f} GB")
    if refreshed:
        print(f"Touched  : {len(refreshed)} (bytes unchanged; manifest mtime refreshed)")

    if args.limit:
        pending = pending[: args.limit]
        print(f"--limit  : this run will do only the first {len(pending)}")

    if not args.commit:
        print("\nDRY RUN (default) — nothing uploaded. Re-run with --commit.")
        for rel in pending[:20]:
            print(f"    would upload: {archive_key(rel)}  ({local[rel]['size'] / 1e6:.0f} MB)")
        if len(pending) > 20:
            print(f"    ... and {len(pending) - 20} more")
        return 0

    try:
        lock = ArchiveLock().acquire()
    except ArchiveLockHeld as held:
        # ⚠️ EXIT 0, deliberately. The hourly task firing on top of a running
        # multi-day seed is the DESIGN, not a fault, and a non-zero
        # LastTaskResult here would train everyone to ignore this task's status.
        print(f"Another archive run is in flight — {held}. Nothing to do.")
        return 0

    started = time.time()
    uploaded: List[str] = []
    failed: List[Tuple[str, str]] = []
    done_bytes = 0
    try:
        client = s3_client()
        abort_stale_multipart(client)

        for rel in refreshed:
            files[rel] = manifest_entry(rel, local[rel], digests[rel] or record.get(rel, {}).get("sha256"))
        if refreshed and not pending:
            write_manifest(files, failures)

        for i, rel in enumerate(pending, 1):
            if args.max_hours and (time.time() - started) / 3600.0 >= args.max_hours:
                print(f"--max-hours {args.max_hours} reached; stopping cleanly with "
                      f"{len(pending) - i + 1} file(s) left for the next run.")
                break
            meta = local[rel]
            size = meta["size"]
            lock.heartbeat(current_file=rel, done_this_run=len(uploaded), bytes_this_run=done_bytes)
            print(f"  [{i}/{len(pending)}] {rel} ({size / 1e6:.0f} MB) …", flush=True)
            t0 = time.time()
            ok, detail = upload_one(client, rel, meta["src"], size)
            if not ok:
                failed.append((rel, detail))
                entry = failures.get(rel) or {"attempts": 0}
                entry.update({"error": detail, "attempts": int(entry.get("attempts", 0)) + 1,
                              "last_try": now_iso(), "size": size})
                failures[rel] = entry
                write_manifest(files, failures)
                print(f"  [FAIL] {rel}: {detail}", flush=True)
                continue
            digest = digests.get(rel) or sha256_of(meta["src"])
            files[rel] = manifest_entry(rel, meta, digest)
            failures.pop(rel, None)
            write_manifest(files, failures)  # checkpoint per file — each is expensive to redo
            uploaded.append(rel)
            done_bytes += size
            elapsed = max(1e-6, time.time() - t0)
            run_min = (time.time() - started) / 60
            print(f"  [ok] {archive_key(rel)}  ({size / 1e6:.0f} MB in {elapsed:.0f}s = "
                  f"{size / 1e6 / elapsed:.1f} MB/s; run total {done_bytes / 1e9:.2f} GB "
                  f"in {run_min:.1f} min)", flush=True)
    finally:
        write_manifest(files, failures)
        lock.release()

    mins = (time.time() - started) / 60
    rate = done_bytes / 1e6 / max(1e-6, mins * 60)
    print(f"\nUploaded {len(uploaded)} / {len(pending)} ({done_bytes / 1e9:.2f} GB) "
          f"in {mins:.1f} min at {rate:.1f} MB/s; {len(failed)} failed.")
    archived_bytes = sum(int(v.get("size") or 0) for v in files.values())
    print(f"Archive now holds {len(files)} objects, {archived_bytes / 1e9:.2f} GB "
          f"({archived_bytes / max(1, library_bytes) * 100:.1f}% of the library).")
    if failed:
        print("\n::error::files failed this run — the next hourly run retries exactly these:")
        for rel, detail in failed[:20]:
            print(f"  - {rel}: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
