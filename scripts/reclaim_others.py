#!/usr/bin/env python3
"""
Reclaim books from Google Drive that are owned by other users.

Scans all audiobook folders on Drive for files NOT owned by you.
Downloads them to the local library so they appear in the catalog.
Does NOT re-upload them or create new Drive folders.

Usage:
    python scripts/reclaim_others.py              # Full run (download + rebuild)
    python scripts/reclaim_others.py --dry-run    # Preview only (no downloads)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))

# Fix Windows console encoding
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from drive_auth import build_drive_service

DRIVE_PARENT_FOLDER_ID = "1yZHU_UryCZkuhg9zFzu5uOadx3NI0FJv"
MY_EMAIL = os.getenv("Email", "nbaslamking@gmail.com").lower()
LIBRARY_ROOT = Path(os.getenv("ROOT_DIR", r"C:\Users\nbasl\OpenAudible\books"))
AUDIOBOOK_EXTS = {".m4b", ".m4a", ".mp4"}
MANIFEST_PATH = SCRIPTS_DIR / "reclaim_manifest.json"


def load_manifest() -> set:
    """Load set of already-reclaimed file IDs."""
    if MANIFEST_PATH.exists():
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_manifest(reclaimed: set) -> None:
    """Save reclaimed file IDs."""
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(reclaimed), f, indent=2)


def scan_drive_for_others(service) -> list[dict]:
    """
    Scan all audiobook folders for files owned by someone else.
    Returns list of {id, name, folder_name, owner_email, size}.
    """
    print("Scanning Drive folders for files owned by others...")

    # Get all author folders
    folders = []
    page_token = None
    while True:
        results = service.files().list(
            q=f"'{DRIVE_PARENT_FOLDER_ID}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
            pageSize=1000,
            fields="nextPageToken, files(id, name)",
            pageToken=page_token,
        ).execute()
        folders.extend(results.get("files", []))
        page_token = results.get("nextPageToken")
        if not page_token:
            break

    print(f"  Found {len(folders)} author folders")

    # Scan each folder for files not owned by me
    others_files = []
    for i, folder in enumerate(folders):
        # Get files in this folder with owner info
        file_token = None
        while True:
            results = service.files().list(
                q=f"'{folder['id']}' in parents and trashed=false",
                pageSize=100,
                fields="nextPageToken, files(id, name, owners, size, mimeType)",
                pageToken=file_token,
            ).execute()

            for f in results.get("files", []):
                # Skip folders
                if f.get("mimeType") == "application/vnd.google-apps.folder":
                    continue
                # Check if file extension is audiobook
                name = f.get("name", "")
                ext = Path(name).suffix.lower()
                if ext not in AUDIOBOOK_EXTS:
                    continue
                # Check owner
                owners = f.get("owners", [])
                owner_email = owners[0].get("emailAddress", "").lower() if owners else ""
                if owner_email and owner_email != MY_EMAIL:
                    others_files.append({
                        "id": f["id"],
                        "name": name,
                        "folder_name": folder["name"],
                        "owner_email": owner_email,
                        "size": int(f.get("size", 0)),
                    })

            file_token = results.get("nextPageToken")
            if not file_token:
                break

        # Progress every 20 folders
        if (i + 1) % 20 == 0:
            print(f"  Scanned {i+1}/{len(folders)} folders, found {len(others_files)} files by others")

    print(f"  Total files owned by others: {len(others_files)}")
    return others_files


def download_file(service, file_info: dict, dry_run: bool = False) -> Path | None:
    """Download a file to the local library under its author folder."""
    from googleapiclient.http import MediaIoBaseDownload

    folder_name = file_info["folder_name"]
    file_name = file_info["name"]

    # Determine local path — use the Drive folder name as author folder
    # Strip subfolder paths from folder name (e.g., "Author - Series/SubFolder")
    author_base = folder_name.split("/")[0].strip()
    local_dir = LIBRARY_ROOT / author_base
    local_path = local_dir / file_name

    if local_path.exists():
        return local_path  # Already have it

    if dry_run:
        size_mb = file_info["size"] / (1024 * 1024)
        print(f"    [DRY-RUN] Would download: {author_base}/{file_name} ({size_mb:.1f} MB)")
        return None

    local_dir.mkdir(parents=True, exist_ok=True)
    size_mb = file_info["size"] / (1024 * 1024)
    print(f"    [DOWNLOAD] {author_base}/{file_name} ({size_mb:.1f} MB)...", end="", flush=True)

    try:
        request = service.files().get_media(fileId=file_info["id"])
        with open(local_path, "wb") as f:
            downloader = MediaIoBaseDownload(f, request, chunksize=10 * 1024 * 1024)
            done = False
            while not done:
                status, done = downloader.next_chunk()
        print(" done")
        return local_path
    except Exception as e:
        print(f" FAILED: {e}")
        if local_path.exists():
            local_path.unlink()
        return None


def main():
    parser = argparse.ArgumentParser(description="Reclaim audiobooks owned by others on Drive")
    parser.add_argument("--dry-run", action="store_true", help="Preview without downloading")
    args = parser.parse_args()

    print("=" * 60)
    print("  RECLAIM: Download books owned by others from Drive")
    if args.dry_run:
        print("  MODE: DRY RUN")
    print("=" * 60)

    service = build_drive_service()
    if not service:
        print("ERROR: Could not connect to Drive.")
        return

    # Load manifest of already-reclaimed files
    reclaimed = load_manifest()
    print(f"  Already reclaimed: {len(reclaimed)} files")

    # Scan for files owned by others
    others = scan_drive_for_others(service)

    # Filter out already-reclaimed, already-local, "Copy of" files, and duplicate numbered folders
    new_files = []
    already_local = 0
    for f in others:
        if f["id"] in reclaimed:
            continue
        # Skip "Copy of" files
        if f["name"].startswith("Copy of "):
            reclaimed.add(f["id"])
            continue
        # Skip files in numbered duplicate folders (e.g. "Author 2" when "Author" exists)
        author_base = f["folder_name"].split("/")[0].strip()
        import re
        numbered_match = re.match(r'^(.+?)\s+\d+$', author_base)
        if numbered_match:
            base_without_number = numbered_match.group(1)
            # Check if the base folder (without number) exists locally
            if (LIBRARY_ROOT / base_without_number).exists():
                reclaimed.add(f["id"])
                continue
        # Check if we already have it locally
        local_path = LIBRARY_ROOT / author_base / f["name"]
        if local_path.exists():
            reclaimed.add(f["id"])
            already_local += 1
            continue
        new_files.append(f)

    print(f"\n  New files to reclaim: {len(new_files)}")
    print(f"  Already local (skipped): {already_local}")

    if not new_files:
        print("\n  Nothing to reclaim!")
        save_manifest(reclaimed)
        return

    # Group by owner for reporting
    by_owner = {}
    for f in new_files:
        by_owner.setdefault(f["owner_email"], []).append(f)

    print(f"\n  Files by owner:")
    for owner, files in sorted(by_owner.items()):
        total_size = sum(f["size"] for f in files) / (1024 * 1024 * 1024)
        print(f"    {owner}: {len(files)} files ({total_size:.2f} GB)")

    # Download
    print(f"\n  {'[DRY-RUN] Would download' if args.dry_run else 'Downloading'} {len(new_files)} files...")
    downloaded = 0
    for i, f in enumerate(new_files, 1):
        print(f"  [{i}/{len(new_files)}] {f['folder_name']}/{f['name']}")
        result = download_file(service, f, dry_run=args.dry_run)
        if result or args.dry_run:
            reclaimed.add(f["id"])
            downloaded += 1

    # Save manifest
    if not args.dry_run:
        save_manifest(reclaimed)

    # Rebuild catalog if we downloaded anything
    if downloaded > 0 and not args.dry_run:
        print(f"\n  Rebuilding catalog...")
        try:
            from app.main import main as catalog_main
            catalog_main()
            print("  Catalog rebuilt.")

            # Auto-commit and push
            import subprocess
            status = subprocess.run(
                ["git", "status", "--porcelain", "site/"],
                capture_output=True, text=True, cwd=str(PROJECT_ROOT),
            )
            if status.stdout.strip():
                subprocess.run(["git", "add", "site/catalog.csv", "site/index.html", "site/covers/", "site/stats.html"],
                               cwd=str(PROJECT_ROOT), capture_output=True)
                msg = f"feat(catalog): Reclaimed {downloaded} books from other owners"
                subprocess.run(["git", "commit", "-m", msg], cwd=str(PROJECT_ROOT), capture_output=True)
                subprocess.run(["git", "pull", "--rebase"], cwd=str(PROJECT_ROOT), capture_output=True)
                subprocess.run(["git", "push"], cwd=str(PROJECT_ROOT), capture_output=True)
                print("  Pushed to origin.")
        except Exception as e:
            print(f"  [WARN] Catalog rebuild failed: {e}")

    # Summary
    print(f"\n{'=' * 60}")
    print(f"  COMPLETE: {downloaded} files {'would be downloaded' if args.dry_run else 'downloaded'}")
    print(f"  Total reclaimed (all time): {len(reclaimed)}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
