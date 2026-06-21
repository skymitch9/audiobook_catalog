"""
Audit: Compare every file on Google Drive vs local library.
Reports:
- Files on Drive but NOT local (need to download)
- Files local but NOT on Drive (need to upload)
- Files that exist in both (synced)

Ignores the "zzzz_Books_to_be_Converted" folder.

Usage:
    python scripts/audit_drive_vs_local.py           # Full report
    python scripts/audit_drive_vs_local.py --fix     # Download missing files from Drive
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from drive_auth import build_drive_service
from googleapiclient.http import MediaIoBaseDownload

DRIVE_PARENT_FOLDER_ID = "1yZHU_UryCZkuhg9zFzu5uOadx3NI0FJv"
LIBRARY_ROOT = Path(os.getenv("ROOT_DIR", r"C:\Users\nbasl\OpenAudible\books"))
AUDIOBOOK_EXTS = {".m4b", ".m4a", ".mp4"}
IGNORE_FOLDERS = {"zzzz_books_to_be_converted", "zzzz_to_be_converted"}


def get_all_drive_files(service) -> dict[str, list[dict]]:
    """Get all files from Drive organized by folder. Returns {folder_name: [{name, id, owner}]}."""
    print("Scanning Drive folders...")
    
    # Get all folders
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

    print(f"  Found {len(folders)} folders")

    # Get files in each folder
    drive_files = {}
    for i, folder in enumerate(folders):
        if folder["name"].lower() in IGNORE_FOLDERS:
            continue

        files = []
        page_token = None
        while True:
            results = service.files().list(
                q=f"'{folder['id']}' in parents and trashed=false and mimeType!='application/vnd.google-apps.folder'",
                pageSize=1000,
                fields="nextPageToken, files(id, name, owners, size)",
                pageToken=page_token,
            ).execute()
            files.extend(results.get("files", []))
            page_token = results.get("nextPageToken")
            if not page_token:
                break

        if files:
            drive_files[folder["name"]] = [{
                "name": f["name"],
                "id": f["id"],
                "owner": f.get("owners", [{}])[0].get("emailAddress", "unknown"),
                "size": int(f.get("size", 0)),
            } for f in files]

        if (i + 1) % 50 == 0:
            print(f"  Scanned {i+1}/{len(folders)} folders...")

    total_files = sum(len(v) for v in drive_files.values())
    print(f"  Total files on Drive: {total_files}")
    return drive_files


def get_all_local_files() -> dict[str, list[str]]:
    """Get all local audiobook files organized by author folder."""
    print("Scanning local library...")
    local_files = {}

    if not LIBRARY_ROOT.exists():
        print(f"  [ERROR] Library root not found: {LIBRARY_ROOT}")
        return {}

    for author_dir in LIBRARY_ROOT.iterdir():
        if not author_dir.is_dir():
            continue
        if author_dir.name.lower() in IGNORE_FOLDERS:
            continue

        files = [f.name for f in author_dir.iterdir()
                 if f.is_file() and f.suffix.lower() in AUDIOBOOK_EXTS]
        if files:
            local_files[author_dir.name] = files

    total_files = sum(len(v) for v in local_files.values())
    print(f"  Total local files: {total_files}")
    return local_files


def download_file(service, file_id: str, dest_path: Path) -> bool:
    """Download a file from Drive."""
    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        request = service.files().get_media(fileId=file_id)
        with open(dest_path, "wb") as f:
            downloader = MediaIoBaseDownload(f, request, chunksize=10 * 1024 * 1024)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status:
                    pct = int(status.progress() * 100)
                    print(f"\r      Downloading... {pct}%", end="", flush=True)
        print(f"\r      Downloaded!")
        return True
    except Exception as e:
        print(f"\r      [ERROR] Download failed: {e}")
        if dest_path.exists():
            dest_path.unlink()
        return False


def main():
    parser = argparse.ArgumentParser(description="Audit Drive vs Local library")
    parser.add_argument("--fix", action="store_true", help="Download files missing locally from Drive")
    args = parser.parse_args()

    service = build_drive_service()
    if not service:
        print("[ERROR] Auth failed")
        return

    print("=" * 60)
    print("  Drive vs Local Audit")
    print("=" * 60)

    drive_files = get_all_drive_files(service)
    local_files = get_all_local_files()

    # --- Compare ---
    on_drive_not_local = []  # (folder_name, file_info)
    on_local_not_drive = []  # (folder_name, filename)
    synced = 0

    # Check Drive -> Local
    for folder_name, files in drive_files.items():
        local_folder_files = set()
        # Try exact folder match and case-insensitive
        for local_folder, local_list in local_files.items():
            if local_folder.lower() == folder_name.lower():
                local_folder_files = set(f.lower() for f in local_list)
                break
            # Check if folder_name contains local_folder (e.g., "Randi Darren/William D. Arand - ...")
            parts = folder_name.replace("/", " - ").split(" - ")
            for part in parts:
                if part.strip().lower() == local_folder.lower():
                    local_folder_files = set(f.lower() for f in local_list)
                    break
            if local_folder_files:
                break

        for file_info in files:
            if file_info["name"].lower() in local_folder_files:
                synced += 1
            else:
                on_drive_not_local.append((folder_name, file_info))

    # Check Local -> Drive
    for local_folder, files in local_files.items():
        drive_folder_files = set()
        for drive_folder, drive_list in drive_files.items():
            if drive_folder.lower() == local_folder.lower():
                drive_folder_files = set(f["name"].lower() for f in drive_list)
                break
            parts = drive_folder.replace("/", " - ").split(" - ")
            for part in parts:
                if part.strip().lower() == local_folder.lower():
                    drive_folder_files = set(f["name"].lower() for f in drive_list)
                    break
            if drive_folder_files:
                break

        for filename in files:
            if filename.lower() not in drive_folder_files:
                on_local_not_drive.append((local_folder, filename))

    # --- Report ---
    print(f"\n{'='*60}")
    print(f"  RESULTS")
    print(f"{'='*60}")
    print(f"  Synced (both Drive & local): {synced}")
    print(f"  On Drive, NOT local: {len(on_drive_not_local)}")
    print(f"  On local, NOT Drive: {len(on_local_not_drive)}")

    if on_drive_not_local:
        print(f"\n  --- ON DRIVE BUT NOT LOCAL ({len(on_drive_not_local)}) ---")
        by_owner = {}
        for folder_name, file_info in on_drive_not_local:
            owner = file_info["owner"]
            if owner not in by_owner:
                by_owner[owner] = []
            by_owner[owner].append((folder_name, file_info))

        for owner, items in sorted(by_owner.items()):
            print(f"\n    Owner: {owner} ({len(items)} files)")
            for folder_name, file_info in items[:10]:
                size_mb = file_info["size"] / (1024 * 1024)
                print(f"      {folder_name}/{file_info['name']} ({size_mb:.0f} MB)")
            if len(items) > 10:
                print(f"      ... and {len(items) - 10} more")

    if on_local_not_drive:
        print(f"\n  --- ON LOCAL BUT NOT DRIVE ({len(on_local_not_drive)}) ---")
        for folder_name, filename in on_local_not_drive[:20]:
            print(f"    {folder_name}/{filename}")
        if len(on_local_not_drive) > 20:
            print(f"    ... and {len(on_local_not_drive) - 20} more")

    # --- Fix: Download missing files ---
    if args.fix and on_drive_not_local:
        print(f"\n{'='*60}")
        print(f"  DOWNLOADING {len(on_drive_not_local)} missing files...")
        print(f"{'='*60}")

        downloaded = 0
        for folder_name, file_info in on_drive_not_local:
            # Determine local folder (use first part of drive folder name)
            local_folder = folder_name.split("/")[0].split(" - ")[0].strip()
            local_path = LIBRARY_ROOT / local_folder / file_info["name"]

            if local_path.exists():
                continue

            size_mb = file_info["size"] / (1024 * 1024)
            print(f"\n    [{downloaded+1}/{len(on_drive_not_local)}] {local_folder}/{file_info['name']} ({size_mb:.0f} MB)")

            if download_file(service, file_info["id"], local_path):
                downloaded += 1

        print(f"\n  Downloaded {downloaded} files.")

    print(f"\n{'='*60}")
    print(f"  DONE")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
