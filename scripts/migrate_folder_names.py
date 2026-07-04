#!/usr/bin/env python3
"""
Migration: Strip " - Series/Description" suffixes from author folders.

Renames folders on both local disk and Google Drive from:
    "Author Name - Series/Description"
to:
    "Author Name"

Also updates:
    - author_drive_map.json (folder name -> URL mapping)
    - scripts/author_aliases.json (alias entries that reference old names)
    - scripts/upload_manifest.json (tracked folder names)
    - scripts/drive_folders_cache.json (cached folder list)

Usage:
    python scripts/migrate_folder_names.py              # dry run
    python scripts/migrate_folder_names.py --execute    # apply changes
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from collections import defaultdict
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from drive_auth import build_drive_service

# Config
LIBRARY_ROOT = Path(os.getenv("ROOT_DIR", r"C:\Users\nbasl\OpenAudible\books"))
DRIVE_PARENT_FOLDER_ID = "1yZHU_UryCZkuhg9zFzu5uOadx3NI0FJv"
AUTHOR_MAP_PATH = PROJECT_ROOT / "author_drive_map.json"
ALIASES_PATH = SCRIPTS_DIR / "author_aliases.json"
# SYNC_DATA_DIR must match sync_to_drive.py, which owns these files
SYNC_DATA_DIR = Path(os.getenv("SYNC_DATA_DIR", str(SCRIPTS_DIR)))
MANIFEST_PATH = SYNC_DATA_DIR / "upload_manifest.json"
CACHE_PATH = SYNC_DATA_DIR / "drive_folders_cache.json"


def get_base_name(folder_name: str) -> str:
    """Extract the author base name (before first ' - ')."""
    return folder_name.split(" - ")[0].strip()


def needs_rename(folder_name: str) -> bool:
    """Return True if the folder has a ' - ' suffix to strip."""
    return " - " in folder_name


def check_conflicts(names: list[str]) -> dict[str, list[str]]:
    """
    Check if stripping suffixes would create naming conflicts.
    Returns a dict of base_name -> [original_names] where conflicts exist.
    """
    base_groups: dict[str, list[str]] = defaultdict(list)
    for name in names:
        base = get_base_name(name)
        base_groups[base].append(name)
    return {k: v for k, v in base_groups.items() if len(v) > 1 and any(needs_rename(n) for n in v)}


def main():
    dry_run = "--execute" not in sys.argv

    print("=" * 70)
    print("  FOLDER NAME MIGRATION: Strip ' - Series/Description' suffixes")
    print(f"  Mode: {'DRY RUN' if dry_run else 'EXECUTE'}")
    print("=" * 70)

    # =========================================================================
    # PHASE 1: Scan local folders
    # =========================================================================
    print("\n[PHASE 1] Scanning local folders...")
    local_folders = [d for d in sorted(LIBRARY_ROOT.iterdir()) if d.is_dir()]
    local_to_rename = [(d, LIBRARY_ROOT / get_base_name(d.name))
                       for d in local_folders if needs_rename(d.name)]
    print(f"  Total local folders: {len(local_folders)}")
    print(f"  Need renaming: {len(local_to_rename)}")

    # Check local conflicts
    local_names = [d.name for d in local_folders]
    local_conflicts = check_conflicts(local_names)
    if local_conflicts:
        print(f"\n  [CONFLICTS] {len(local_conflicts)} base names would collide:")
        for base, originals in sorted(local_conflicts.items()):
            print(f"    '{base}' <- {originals}")

    # =========================================================================
    # PHASE 2: Scan Drive folders
    # =========================================================================
    print("\n[PHASE 2] Scanning Google Drive folders...")
    service = build_drive_service()
    if not service:
        print("  ERROR: Could not connect to Drive.")
        return

    # Fetch all Drive folders
    drive_folders = []
    page_token = None
    while True:
        results = service.files().list(
            q=f"'{DRIVE_PARENT_FOLDER_ID}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
            pageSize=1000,
            fields="nextPageToken, files(id, name)",
            pageToken=page_token,
        ).execute()
        drive_folders.extend(results.get("files", []))
        page_token = results.get("nextPageToken")
        if not page_token:
            break

    drive_to_rename = [(f, get_base_name(f["name"]))
                       for f in drive_folders if needs_rename(f["name"])]
    print(f"  Total Drive folders: {len(drive_folders)}")
    print(f"  Need renaming: {len(drive_to_rename)}")

    # Check Drive conflicts
    drive_names = [f["name"] for f in drive_folders]
    drive_conflicts = check_conflicts(drive_names)
    if drive_conflicts:
        print(f"\n  [CONFLICTS] {len(drive_conflicts)} base names would collide on Drive:")
        for base, originals in sorted(drive_conflicts.items()):
            print(f"    '{base}' <- {originals}")

    # =========================================================================
    # PHASE 3: Preview renames
    # =========================================================================
    print("\n[PHASE 3] Rename preview (first 30):")
    for old, new in drive_to_rename[:30]:
        print(f"  '{old['name']}' -> '{new}'")
    if len(drive_to_rename) > 30:
        print(f"  ... and {len(drive_to_rename) - 30} more")

    # =========================================================================
    # PHASE 4: Check for conflicts that would merge folders
    # =========================================================================
    # If "Author" already exists AND "Author - Series" exists, we'd need to merge
    print("\n[PHASE 4] Checking for merge situations...")
    existing_bases_local = {get_base_name(d.name): d for d in local_folders if not needs_rename(d.name)}
    merges_needed_local = []
    for old_path, new_path in local_to_rename:
        base = get_base_name(old_path.name)
        if base in existing_bases_local or new_path.exists():
            merges_needed_local.append((old_path, new_path))

    existing_bases_drive = {}
    for f in drive_folders:
        if not needs_rename(f["name"]):
            existing_bases_drive[f["name"]] = f

    merges_needed_drive = []
    for old_folder, new_name in drive_to_rename:
        if new_name in existing_bases_drive:
            merges_needed_drive.append((old_folder, existing_bases_drive[new_name]))

    if merges_needed_local:
        print(f"  Local merges needed: {len(merges_needed_local)}")
        for old, new in merges_needed_local[:10]:
            print(f"    '{old.name}' -> merge into '{new.name}'")
    else:
        print("  Local merges needed: 0 (clean renames only)")

    if merges_needed_drive:
        print(f"  Drive merges needed: {len(merges_needed_drive)}")
        for old, existing in merges_needed_drive[:10]:
            print(f"    '{old['name']}' -> merge into '{existing['name']}'")
    else:
        print("  Drive merges needed: 0 (clean renames only)")

    # =========================================================================
    # PHASE 5: Execute renames
    # =========================================================================
    if dry_run:
        print("\n" + "=" * 70)
        print("  DRY RUN COMPLETE")
        print(f"  Local renames: {len(local_to_rename)}")
        print(f"  Drive renames: {len(drive_to_rename)}")
        print(f"  Local merges: {len(merges_needed_local)}")
        print(f"  Drive merges: {len(merges_needed_drive)}")
        print(f"  Local conflicts: {len(local_conflicts)}")
        print(f"  Drive conflicts: {len(drive_conflicts)}")
        print("\n  Run with --execute to apply.")
        print("=" * 70)
        return

    # --- Execute local renames ---
    print("\n[PHASE 5a] Renaming local folders...")
    local_renamed = 0
    local_merged = 0
    for old_path, new_path in local_to_rename:
        if new_path.exists():
            # Merge: move files from old to new, delete old
            files_in_old = list(old_path.iterdir())
            existing_in_new = {f.name for f in new_path.iterdir()}
            for f in files_in_old:
                if f.name not in existing_in_new:
                    shutil.move(str(f), str(new_path / f.name))
                else:
                    # Duplicate — delete the old copy
                    f.unlink() if f.is_file() else shutil.rmtree(str(f))
            # Remove empty old folder
            try:
                old_path.rmdir()
            except OSError:
                shutil.rmtree(str(old_path))
            local_merged += 1
        else:
            old_path.rename(new_path)
            local_renamed += 1
    print(f"  Renamed: {local_renamed}, Merged: {local_merged}")

    # --- Execute Drive renames ---
    print("\n[PHASE 5b] Renaming Drive folders...")
    drive_renamed = 0
    drive_merged = 0
    for old_folder, new_name in drive_to_rename:
        if new_name in existing_bases_drive:
            # Merge: move files from old folder to existing, trash old
            target = existing_bases_drive[new_name]
            # Get files in old folder
            results = service.files().list(
                q=f"'{old_folder['id']}' in parents and trashed=false",
                fields="files(id, name)"
            ).execute()
            old_files = results.get("files", [])

            # Get files in target
            results = service.files().list(
                q=f"'{target['id']}' in parents and trashed=false",
                fields="files(id, name)"
            ).execute()
            target_file_names = {f["name"] for f in results.get("files", [])}

            for f in old_files:
                if f["name"] not in target_file_names:
                    service.files().update(
                        fileId=f["id"],
                        addParents=target["id"],
                        removeParents=old_folder["id"],
                    ).execute()
                else:
                    # Duplicate — trash it
                    service.files().update(fileId=f["id"], body={"trashed": True}).execute()
                time.sleep(0.05)

            # Trash empty old folder
            service.files().update(fileId=old_folder["id"], body={"trashed": True}).execute()
            drive_merged += 1
        else:
            # Simple rename
            service.files().update(
                fileId=old_folder["id"],
                body={"name": new_name}
            ).execute()
            # Track new name for future conflict checks
            existing_bases_drive[new_name] = {"id": old_folder["id"], "name": new_name}
            drive_renamed += 1
        time.sleep(0.05)
    print(f"  Renamed: {drive_renamed}, Merged: {drive_merged}")

    # =========================================================================
    # PHASE 6: Update author_drive_map.json
    # =========================================================================
    print("\n[PHASE 6] Updating author_drive_map.json...")
    if AUTHOR_MAP_PATH.exists():
        with open(AUTHOR_MAP_PATH, "r", encoding="utf-8") as f:
            author_map = json.load(f)

        new_map = {}
        for old_key, url in author_map.items():
            new_key = get_base_name(old_key) if needs_rename(old_key) else old_key
            # If base name already exists, keep the one with the URL (don't lose data)
            if new_key in new_map:
                continue  # already mapped
            new_map[new_key] = url

        sorted_map = dict(sorted(new_map.items(), key=lambda x: x[0].lower()))
        with open(AUTHOR_MAP_PATH, "w", encoding="utf-8") as f:
            json.dump(sorted_map, f, indent=2, ensure_ascii=False)
        print(f"  Entries: {len(author_map)} -> {len(sorted_map)}")
    else:
        print("  [SKIP] File not found")

    # =========================================================================
    # PHASE 7: Update author_aliases.json
    # =========================================================================
    print("\n[PHASE 7] Updating author_aliases.json...")
    if ALIASES_PATH.exists():
        with open(ALIASES_PATH, "r", encoding="utf-8") as f:
            aliases = json.load(f)

        new_aliases = {}
        for key, val in aliases.items():
            if key.startswith("_"):
                new_aliases[key] = val
                continue
            # Strip suffix from values that reference old folder names
            new_val = get_base_name(val) if needs_rename(val) and not val.startswith("__FOLDER_ID__") else val
            new_aliases[key] = new_val

        with open(ALIASES_PATH, "w", encoding="utf-8") as f:
            json.dump(new_aliases, f, indent=2, ensure_ascii=False)
        print(f"  Updated {len(new_aliases)} entries")
    else:
        print("  [SKIP] File not found")

    # =========================================================================
    # PHASE 8: Clear stale caches
    # =========================================================================
    print("\n[PHASE 8] Clearing stale caches...")
    if CACHE_PATH.exists():
        CACHE_PATH.unlink()
        print("  Deleted drive_folders_cache.json")
    if MANIFEST_PATH.exists():
        MANIFEST_PATH.unlink()
        print("  Deleted upload_manifest.json (will regenerate on next sync)")

    print("\n" + "=" * 70)
    print("  MIGRATION COMPLETE")
    print(f"  Local: {local_renamed} renamed, {local_merged} merged")
    print(f"  Drive: {drive_renamed} renamed, {drive_merged} merged")
    print("  Next: rebuild catalog with `python -m app.main`")
    print("=" * 70)


if __name__ == "__main__":
    main()
