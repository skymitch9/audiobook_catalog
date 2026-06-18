"""
Reclaim Drive Files: Download files uploaded by other users, delete from Drive, re-upload under your account.

Also downloads any extra files from Drive that don't exist locally (e.g., books contributed by others).

Workflow:
1. Get current user's email from Drive API
2. Scan all files in all author folders on Drive
3. For files NOT owned by you:
   a. Download to local library (into the correct author folder)
   b. Delete from Drive
   c. The regular sync_to_drive.py will re-upload them under your account next run
4. For files that exist on Drive but NOT locally (extra content):
   a. Download to local library

Usage:
    python scripts/reclaim_drive_files.py              # Full reclaim
    python scripts/reclaim_drive_files.py --dry-run    # Preview without changes
    python scripts/reclaim_drive_files.py --download-only  # Only download missing files, don't delete
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

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

# Library root
LIBRARY_ROOT = Path(os.getenv("ROOT_DIR", r"C:\Users\nbasl\OpenAudible\books"))

# Parent folder on Drive
DRIVE_PARENT_FOLDER_ID = "1yZHU_UryCZkuhg9zFzu5uOadx3NI0FJv"

# File extensions we care about
AUDIOBOOK_EXTS = {".m4b", ".m4a", ".mp4"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_my_email(service) -> str:
    """Get the authenticated user's email address."""
    # Try env first
    env_email = os.getenv("Email")
    if env_email:
        return env_email
    # Fallback to API
    about = service.about().get(fields="user").execute()
    email = about["user"]["emailAddress"]
    return email


def list_all_author_folders(service) -> list[dict]:
    """List all author folders under the parent."""
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
    return folders


def list_files_in_folder(service, folder_id: str) -> list[dict]:
    """List all files in a specific folder with owner info."""
    files = []
    page_token = None
    while True:
        results = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false and mimeType!='application/vnd.google-apps.folder'",
            pageSize=1000,
            fields="nextPageToken, files(id, name, owners, size, mimeType)",
            pageToken=page_token,
        ).execute()
        files.extend(results.get("files", []))
        page_token = results.get("nextPageToken")
        if not page_token:
            break
    return files


def download_file(service, file_id: str, dest_path: Path) -> bool:
    """Download a file from Drive to local path."""
    from googleapiclient.http import MediaIoBaseDownload

    try:
        request = service.files().get_media(fileId=file_id)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        with open(dest_path, "wb") as f:
            downloader = MediaIoBaseDownload(f, request, chunksize=10 * 1024 * 1024)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status:
                    pct = int(status.progress() * 100)
                    print(f"\r    Downloading... {pct}%", end="", flush=True)
        print(f"\r    Downloaded: {dest_path.name}")
        return True
    except Exception as e:
        print(f"\n    [ERROR] Download failed: {e}")
        if dest_path.exists():
            dest_path.unlink()
        return False


def delete_file_from_drive(service, file_id: str, file_name: str) -> bool:
    """Delete a file from Drive."""
    try:
        service.files().delete(fileId=file_id).execute()
        print(f"    [DELETED] {file_name} from Drive")
        return True
    except Exception as e:
        print(f"    [ERROR] Could not delete {file_name}: {e}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run_reclaim(dry_run: bool = False, download_only: bool = False):
    """Main reclaim process."""
    print("=" * 60)
    print("  Drive File Reclaim - Download & Re-own Files")
    if dry_run:
        print("  MODE: DRY RUN")
    if download_only:
        print("  MODE: DOWNLOAD ONLY (no deletions)")
    print("=" * 60)

    # Authenticate
    service = build_drive_service()
    if not service:
        print("[ERROR] Failed to authenticate.")
        return

    # Get my email
    my_email = get_my_email(service)
    print(f"\n  Authenticated as: {my_email}")
    print(f"  Library root: {LIBRARY_ROOT}")

    # Get all author folders
    print("\n[STEP 1] Scanning Drive folders...")
    folders = list_all_author_folders(service)
    print(f"  Found {len(folders)} author folders.")

    # Stats
    other_owner_files = []
    missing_locally = []
    total_files_scanned = 0

    # Scan each folder
    print("\n[STEP 2] Scanning files in each folder...")
    for i, folder in enumerate(folders, 1):
        folder_name = folder["name"]
        folder_id = folder["id"]

        files = list_files_in_folder(service, folder_id)
        if not files:
            continue

        for file_info in files:
            total_files_scanned += 1
            file_name = file_info["name"]
            file_id = file_info["id"]
            owners = file_info.get("owners", [])
            size = int(file_info.get("size", 0))

            # Check owner
            owner_email = owners[0]["emailAddress"] if owners else "unknown"
            is_mine = owner_email.lower() == my_email.lower()

            # Check if file exists locally
            local_path = LIBRARY_ROOT / folder_name / file_name
            exists_locally = local_path.exists()

            if not is_mine:
                other_owner_files.append({
                    "file_id": file_id,
                    "file_name": file_name,
                    "folder_name": folder_name,
                    "folder_id": folder_id,
                    "owner": owner_email,
                    "size": size,
                    "local_path": local_path,
                    "exists_locally": exists_locally,
                })

            if not exists_locally and not file_name.startswith("."):
                # File on Drive but not local — download it
                ext = Path(file_name).suffix.lower()
                if ext in AUDIOBOOK_EXTS or not AUDIOBOOK_EXTS:
                    missing_locally.append({
                        "file_id": file_id,
                        "file_name": file_name,
                        "folder_name": folder_name,
                        "size": size,
                        "local_path": local_path,
                        "owner": owner_email,
                        "is_mine": is_mine,
                    })

        # Progress indicator
        if i % 50 == 0:
            print(f"  ... scanned {i}/{len(folders)} folders ({total_files_scanned} files)")

    print(f"\n  Scanned {total_files_scanned} total files across {len(folders)} folders.")
    print(f"  Files owned by others: {len(other_owner_files)}")
    print(f"  Files missing locally: {len(missing_locally)}")

    # ---------------------------------------------------------------------------
    # Step 3: Download files missing locally (extras from Drive)
    # ---------------------------------------------------------------------------
    if missing_locally:
        print(f"\n[STEP 3] Downloading {len(missing_locally)} files missing locally...")
        downloaded = 0
        for item in missing_locally:
            size_mb = item["size"] / (1024 * 1024)
            print(f"\n  [{downloaded+1}/{len(missing_locally)}] {item['folder_name']}/{item['file_name']} ({size_mb:.1f} MB) [owner: {item['owner']}]")

            if dry_run:
                print(f"    [DRY-RUN] Would download to: {item['local_path']}")
            else:
                success = download_file(service, item["file_id"], item["local_path"])
                if success:
                    downloaded += 1

        print(f"\n  Downloaded {downloaded} files.")
    else:
        print("\n[STEP 3] No files missing locally.")

    # ---------------------------------------------------------------------------
    # Step 4: Delete files owned by others from Drive (they'll be re-uploaded by sync)
    # ---------------------------------------------------------------------------
    if other_owner_files and not download_only:
        print(f"\n[STEP 4] Reclaiming {len(other_owner_files)} files owned by others...")
        print("  (These will be re-uploaded under your account on next sync run)")

        reclaimed = 0
        for item in other_owner_files:
            size_mb = item["size"] / (1024 * 1024)
            print(f"\n  [{reclaimed+1}/{len(other_owner_files)}] {item['folder_name']}/{item['file_name']} ({size_mb:.1f} MB)")
            print(f"    Owner: {item['owner']}")

            # Make sure we have it locally first
            if not item["exists_locally"]:
                if dry_run:
                    print(f"    [DRY-RUN] Would download first, then delete from Drive")
                else:
                    print(f"    Downloading before deleting...")
                    success = download_file(service, item["file_id"], item["local_path"])
                    if not success:
                        print(f"    [SKIP] Failed to download, won't delete from Drive")
                        continue

            # Delete from Drive
            if dry_run:
                print(f"    [DRY-RUN] Would delete from Drive")
            else:
                delete_file_from_drive(service, item["file_id"], item["file_name"])
                reclaimed += 1

        print(f"\n  Reclaimed {reclaimed} files.")
        print("  Run 'python scripts/sync_to_drive.py --upload-only' to re-upload under your account.")
    elif download_only:
        print(f"\n[STEP 4] Skipped (--download-only). {len(other_owner_files)} files owned by others.")
        if other_owner_files:
            print("\n  Files owned by others:")
            for item in other_owner_files[:20]:
                size_mb = item["size"] / (1024 * 1024)
                print(f"    - {item['folder_name']}/{item['file_name']} ({size_mb:.1f} MB) [{item['owner']}]")
            if len(other_owner_files) > 20:
                print(f"    ... and {len(other_owner_files) - 20} more")
    else:
        print("\n[STEP 4] No files owned by others found.")

    print("\n" + "=" * 60)
    print("  DONE")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Reclaim Drive files uploaded by other users")
    parser.add_argument("--dry-run", action="store_true", help="Preview without changes")
    parser.add_argument("--download-only", action="store_true", help="Only download missing files, don't delete")
    args = parser.parse_args()

    run_reclaim(dry_run=args.dry_run, download_only=args.download_only)


if __name__ == "__main__":
    main()
