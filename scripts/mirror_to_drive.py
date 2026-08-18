#!/usr/bin/env python3
"""Mirror the local `estate-backups` copy into Google Drive `GABI_backup`.

The second half of the off-Cloudflare backup mirror. The first half is
`catalog-platform/scripts/mirror-estate-backups.mjs`, which pulls the newest
complete generation of every store out of the private `estate-backups` R2
bucket into a local OneDrive-synced folder. This half carries that folder to a
THIRD home that is neither Cloudflare nor Microsoft.

Closes the restore drill's owner step #7 (catalog-platform
`docs/access/RECOVERY.md` §9.7: *"Get a copy of `estate-backups` off
Cloudflare. Everything protected and everything protecting it live in one
account."*). Owner decision 2026-08-18, verbatim: *"Do a and b, don't store in
GABI tho store in a new folder called GABI_backup on drive"*.

⚠️ `GABI_backup` IS A NEW TOP-LEVEL FOLDER, AND THAT IS THE INSTRUCTION
----------------------------------------------------------------------
It is created in **My Drive's root**, deliberately OUTSIDE the audiobook
library tree that `sync_to_drive.py` manages. It must never be parented under
`DRIVE_PARENT_FOLDER_ID`:

  * that tree is shared with real people by role (`drive_role_parity.py`
    reconciles who can see it), and database dumps — `estate_auth` among them —
    have no business inheriting a book folder's sharing;
  * `sync_to_drive.py`'s own book-shaped logic walks that tree, and a folder of
    `.sql` and `.tar.gz` files inside it is a foreign object in a place other
    code makes assumptions about.

`GABI_backup` is created UNSHARED — it inherits nothing, and this script never
calls `permissions()`. Sharing it, if ever wanted, is a deliberate human act.

WHY THIS SCRIPT LIVES HERE AND NOT IN catalog-platform
------------------------------------------------------
Same reason `publish_docs_snapshot.py` does: the estate's Google Drive OAuth
token exists on this machine at `scripts/token.json` and nowhere else, and
`drive_auth.py` is the one helper that knows how to refresh it. A Node script
in the sibling repo would need a second Drive credential for no gain. The
CloudFLARE-side half stays in catalog-platform beside `backup-r2.mjs`, because
that is where the R2 key grammar lives. Two halves, two repos, each where its
credentials already are.

RETENTION — DRIVE FOLLOWS LOCAL, WHICH FOLLOWS THE BUCKET
---------------------------------------------------------
⚠️ **This is a MIRROR, not an archive.** A generation the local mirror has
pruned (because the bucket pruned it) is TRASHED here on the next run. Anything
inside `GABI_backup` is subject to deletion by this script; a copy meant to
outlive the bucket's 8-generation retention belongs somewhere this script does
not manage.

Trashed, never hard-deleted: `files().update(trashed=True)` leaves 30 days of
recovery in Drive's own bin. A retention bug here should be survivable.

INCREMENTAL
-----------
A file is skipped when Drive already holds one of that name in that folder at
the same byte size AND, when Drive reports one, the same MD5. Drive computes
`md5Checksum` server-side, so that comparison is a genuine end-to-end integrity
check of the stored bytes — not a restatement of what we uploaded.

USAGE
-----
    python scripts/mirror_to_drive.py                # mirror
    python scripts/mirror_to_drive.py --dry-run      # plan only
    python scripts/mirror_to_drive.py --mirror-dir D:\\elsewhere

    ESTATE_MIRROR_DIR=...  overrides the source folder (same env var the Node
                           half reads, so the two cannot point at different
                           places by accident).

Exit 0 = every local file is present in Drive at the right size.
Exit non-zero = something did not land; the previous Drive copy still stands.

⚠️ No token, no credential and no file CONTENT is ever printed. Names, sizes
and counts only.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent

# The same default the Node half uses. Kept in both places on purpose: this
# script must be runnable on its own, and a wrong path here fails loudly at the
# first `is_dir()` check rather than silently mirroring an empty tree.
DEFAULT_MIRROR_DIR = Path(r"C:\Users\nbasl\OneDrive\Documents\estate-backups-mirror")

DRIVE_FOLDER_NAME = "GABI_backup"
FOLDER_MIME = "application/vnd.google-apps.folder"
UPLOAD_CHUNK = 10 * 1024 * 1024  # 10 MB — the split parts are ~200 MB
MAX_RETRIES = 3

# The manifest the Node half writes. Not required (the walk is the source of
# truth), but when present its recorded sha256 makes the report far more useful.
MANIFEST_NAME = "mirror-manifest.json"


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _escape(name: str) -> str:
    """Escape a name for a Drive `q` string literal."""
    return name.replace("\\", "\\\\").replace("'", "\\'")


def find_or_create_folder(
    service, name: str, parent: Optional[str], dry_run: bool, parent_label: str = "root"
) -> Optional[str]:
    """Return the id of `name` under `parent` ('root' when None), creating it if absent.

    `parent_label` is for MESSAGES ONLY. In a dry run the parent folder may not
    exist yet, so its id is None and the query necessarily falls back to
    'root' — printing "under root" there would claim this script is about to
    put a `d1` folder at the top of My Drive, which is precisely the thing the
    module header promises it never does.
    """
    parent_id = parent or "root"
    q = (
        f"name='{_escape(name)}' and mimeType='{FOLDER_MIME}' "
        f"and '{parent_id}' in parents and trashed=false"
    )
    res = service.files().list(q=q, fields="files(id,name)", pageSize=10).execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]

    if dry_run:
        print(f"  [DRY-RUN] would create Drive folder: {name} (under {parent_label})")
        return None

    meta = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
    folder = service.files().create(body=meta, fields="id").execute()
    print(f"  [CREATE] Drive folder '{name}' -> {folder['id']}")
    return folder["id"]


def list_folder(service, folder_id: str) -> Dict[str, dict]:
    """Every non-trashed file directly in `folder_id`, by name."""
    out: Dict[str, dict] = {}
    token = None
    while True:
        res = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields="nextPageToken, files(id,name,size,md5Checksum,mimeType)",
                pageSize=200,
                pageToken=token,
            )
            .execute()
        )
        for f in res.get("files", []):
            out[f["name"]] = f
        token = res.get("nextPageToken")
        if not token:
            return out


def upload_file(service, path: Path, folder_id: str) -> Optional[str]:
    """Resumable upload of one file. Returns the Drive file id, or None on failure."""
    from googleapiclient.http import MediaFileUpload

    size_mb = path.stat().st_size / (1024 * 1024)
    for attempt in range(1, MAX_RETRIES + 1):
        media = MediaFileUpload(str(path), resumable=True, chunksize=UPLOAD_CHUNK)
        label = f"  [UPLOAD] {path.name} ({size_mb:.1f} MB)"
        if attempt > 1:
            label += f" (attempt {attempt}/{MAX_RETRIES})"
        try:
            print(f"{label} ...", end="", flush=True)
            request = service.files().create(
                body={"name": path.name, "parents": [folder_id]},
                media_body=media,
                fields="id",
            )
            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    print(f"\r{label} ... {int(status.progress() * 100)}%", end="", flush=True)
            print(f"\r{label} ... done")
            return response.get("id")
        except Exception as e:  # noqa: BLE001 — retried, then reported
            print(f"\n  [ERROR] Upload failed for {path.name}: {e}")
            if attempt < MAX_RETRIES:
                import time

                time.sleep(2 ** (attempt - 1))
    print(f"  [FAILED] All {MAX_RETRIES} attempts exhausted for {path.name}")
    return None


def local_tree(mirror_dir: Path) -> List[Tuple[str, str, Path]]:
    """Every mirrored object as (kind, store, path), sorted.

    Only `<kind>/<store>/<file>` is mirrored — exactly the bucket's key shape.
    `mirror-manifest.json` at the root is bookkeeping and stays local: it names
    every key the mirror holds, and there is no reason to publish an index of
    the backups to a third system.
    """
    out: List[Tuple[str, str, Path]] = []
    for kind_dir in sorted(p for p in mirror_dir.iterdir() if p.is_dir()):
        for store_dir in sorted(p for p in kind_dir.iterdir() if p.is_dir()):
            for f in sorted(p for p in store_dir.iterdir() if p.is_file()):
                out.append((kind_dir.name, store_dir.name, f))
    return out


# ⚠️ NOT AN R2 BUCKET, AND DELIBERATELY OUTSIDE EVERY GIT REPOSITORY.
# Whisper transcripts of the household's own audiobooks. Owner, 2026-08-18:
# *"back up all this training data to Google Drive GABI back ups… this is data
# that could lead to piracy if it were to get out"*. They live at
# `C:\Users\nbasl\estate-training-data` precisely so that no command run inside
# `audiobook_catalog` (a PUBLIC repo) can commit them — the path IS the guard,
# where a .gitignore entry would only be a promise.
#
# They ride this mirror rather than getting their own script because the Drive
# OAuth token, the retry logic and the md5 comparison already live here, and a
# second uploader would be a second thing to keep correct.
DEFAULT_TRAINING_DIR = Path(
    os.getenv("ESTATE_TRAINING_ROOT", r"C:\Users\nbasl\estate-training-data"))

# Which subfolders of the training root are worth carrying off this machine.
# ⚠️ `packs` is NOT here: those objects also live in the `ebooks-gated` bucket
# and are reproducible from the transcripts in seconds. `whisper-venv` is not
# here either — it is 2.3 GB of reinstallable wheels.
TRAINING_SUBDIRS = ("transcripts", "receipts")


def training_tree(training_dir: Path) -> List[Tuple[str, str, Path]]:
    """Training data as (kind, store, path), shaped like `local_tree`'s output."""
    out: List[Tuple[str, str, Path]] = []
    if not training_dir.is_dir():
        return out
    for name in TRAINING_SUBDIRS:
        sub = training_dir / name
        if not sub.is_dir():
            continue
        for f in sorted(p for p in sub.rglob("*") if p.is_file()):
            out.append(("training-data", name, f))
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mirror-dir", default=os.getenv("ESTATE_MIRROR_DIR", str(DEFAULT_MIRROR_DIR)))
    ap.add_argument("--training-dir", default=str(DEFAULT_TRAINING_DIR),
                    help="local training-data root (transcripts, receipts)")
    ap.add_argument("--no-training-data", action="store_true",
                    help="mirror only the R2 backup generations")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    mirror_dir = Path(args.mirror_dir)
    print(f"=== Mirroring {mirror_dir} -> Google Drive /{DRIVE_FOLDER_NAME} ===")

    if not mirror_dir.is_dir():
        # A named failure, never silence. The usual cause is that the Node half
        # has not run yet on this machine.
        print(f"  [ERROR] Local mirror not found at {mirror_dir}. "
              "Run catalog-platform/scripts/mirror-estate-backups.mjs first.")
        return 2

    files = local_tree(mirror_dir)
    if not files:
        print(f"  [ERROR] {mirror_dir} holds no <kind>/<store>/<file> objects. "
              "Treating an empty source as a failure, not as 'nothing to do'.")
        return 2

    if not args.no_training_data:
        training_dir = Path(args.training_dir)
        extra = training_tree(training_dir)
        # ⚠️ Absent training data is NOT an error. This machine may legitimately
        # have none yet (nothing transcribed), and failing the whole backup
        # mirror over that would stop the database dumps reaching Drive — a much
        # worse outcome than a missing transcript. Say so, then carry on.
        if extra:
            print(f"+ {len(extra)} training-data file(s) from {training_dir}")
            files = files + extra
        else:
            print(f"  [note] no training data under {training_dir} "
                  f"({'/'.join(TRAINING_SUBDIRS)}); mirroring backups only.")
    total_bytes = sum(f.stat().st_size for _, _, f in files)
    print(f"{len(files)} local object(s), {total_bytes} bytes, across "
          f"{len({(k, s) for k, s, _ in files})} store(s).")

    from drive_auth import build_drive_service

    service = build_drive_service()
    if not service:
        print("  [ERROR] Google Drive auth failed. Run `python scripts/drive_auth.py` "
              "on this machine to refresh the OAuth token.")
        return 3

    root_id = find_or_create_folder(service, DRIVE_FOLDER_NAME, None, args.dry_run)
    if root_id is None and not args.dry_run:
        print(f"  [ERROR] Could not resolve or create /{DRIVE_FOLDER_NAME}.")
        return 4

    uploaded = uploaded_bytes = skipped = trashed = failed = 0
    # Cache folder ids so a 12-object run does not make 24 lookups.
    folder_ids: Dict[Tuple[str, ...], Optional[str]] = {}

    def folder_for(*parts: str) -> Optional[str]:
        if parts in folder_ids:
            return folder_ids[parts]
        parent = root_id
        label = DRIVE_FOLDER_NAME
        for i, part in enumerate(parts):
            key = tuple(parts[: i + 1])
            if key not in folder_ids:
                folder_ids[key] = find_or_create_folder(service, part, parent, args.dry_run, label)
            parent = folder_ids[key]
            label = f"{label}/{part}"
            if parent is None:
                # Dry run only: the parent does not exist yet, so nothing below
                # it can be looked up. Stop rather than querying 'root' and
                # printing a plan that reads as if these land at Drive's top.
                break
        return folder_ids.get(parts)

    by_store: Dict[Tuple[str, str], List[Path]] = {}
    for kind, store, path in files:
        by_store.setdefault((kind, store), []).append(path)

    for (kind, store), paths in by_store.items():
        store_id = folder_for(kind, store)
        remote = list_folder(service, store_id) if store_id else {}
        wanted = {p.name for p in paths}

        for path in paths:
            size = path.stat().st_size
            existing = remote.get(path.name)
            if existing and str(existing.get("size")) == str(size):
                remote_md5 = existing.get("md5Checksum")
                if not remote_md5 or remote_md5 == _md5(path):
                    print(f"  [SKIP] {kind}/{store}/{path.name} already on Drive "
                          f"({size} bytes{', md5 verified' if remote_md5 else ''})")
                    skipped += 1
                    continue
                print(f"  [REUPLOAD] {kind}/{store}/{path.name}: size matches but MD5 does NOT — "
                      "the Drive copy is corrupt or stale.")
                if not args.dry_run:
                    service.files().update(fileId=existing["id"], body={"trashed": True}).execute()

            if args.dry_run:
                print(f"  [DRY-RUN] would upload {kind}/{store}/{path.name} ({size} bytes)")
                uploaded += 1
                uploaded_bytes += size
                continue

            if upload_file(service, path, store_id):
                uploaded += 1
                uploaded_bytes += size
            else:
                failed += 1

        # Retention — Drive follows local. A generation the bucket pruned and
        # the local mirror deleted is trashed here too.
        for name, f in remote.items():
            if name in wanted:
                continue
            if args.dry_run:
                print(f"  [DRY-RUN] would trash {kind}/{store}/{name} (pruned upstream)")
            else:
                service.files().update(fileId=f["id"], body={"trashed": True}).execute()
                print(f"  [TRASH] {kind}/{store}/{name} (no longer in the mirror)")
            trashed += 1

    print("\n=== Summary ===")
    print(f"uploaded:  {uploaded} object(s), {uploaded_bytes} bytes")
    print(f"skipped:   {skipped} (already on Drive at the same size/MD5)")
    print(f"trashed:   {trashed} (pruned upstream; recoverable from Drive's bin for 30 days)")
    print(f"failed:    {failed}")
    print(f"Drive folder: /{DRIVE_FOLDER_NAME}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
