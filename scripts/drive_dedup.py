#!/usr/bin/env python3
"""
Google Drive Duplicate Cleanup

Reads the audit report data and removes duplicate files from Drive.
For duplicate files across folders, keeps the copy in the "preferred" folder
(priority author folder) and trashes the other.

For duplicate folders with the same base name, merges files into the
longer/more-descriptive folder name and trashes the empty source.

Usage:
    python scripts/drive_dedup.py              # dry run (default)
    python scripts/drive_dedup.py --execute    # actually delete/move
"""

from __future__ import annotations

import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
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

# Priority authors — their folder is always the "keep" folder
PRIORITY_AUTHORS_PATH = SCRIPTS_DIR / "priority_authors.json"
ALIASES_PATH = SCRIPTS_DIR / "author_aliases.json"


def load_priority_authors() -> list[str]:
    if PRIORITY_AUTHORS_PATH.exists():
        with open(PRIORITY_AUTHORS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [a.lower() for a in data.get("priority_authors", [])]
    return []


def load_aliases() -> dict[str, str]:
    if ALIASES_PATH.exists():
        with open(ALIASES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {k.lower(): v for k, v in data.items() if not k.startswith("_")}
    return {}


def normalize(s: str) -> str:
    """Strip to bare alphanumeric lowercase for comparison.
    If the result is empty (e.g. pure CJK/non-latin text), use the original stripped string."""
    norm = re.sub(r'[^a-z0-9]', '', s.lower())
    if not norm:
        return s.strip().lower()
    return norm


def fetch_all_folders(service) -> list[dict]:
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


def fetch_files_in_folder(service, folder_id: str) -> list[dict]:
    files = []
    page_token = None
    while True:
        results = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false and mimeType!='application/vnd.google-apps.folder'",
            pageSize=200,
            fields="nextPageToken, files(id, name, size)",
            pageToken=page_token,
        ).execute()
        files.extend(results.get("files", []))
        page_token = results.get("nextPageToken")
        if not page_token:
            break
    return files


def choose_keep_folder(folders: list[dict], priority_authors: list[str], aliases: dict[str, str]) -> dict:
    """
    Given a list of duplicate folders, pick which one to keep.
    Priority: folder whose base IS a priority author > alias resolves > longest name.
    """
    # Direct match first
    for folder in folders:
        base = folder["name"].split(" - ")[0].strip()
        if base.lower() in priority_authors:
            return folder

    # Alias resolution
    for folder in folders:
        base = folder["name"].split(" - ")[0].strip()
        resolved = aliases.get(base.lower(), base)
        if resolved.lower() in priority_authors:
            return folder

    # Fallback: keep the longest name (most descriptive)
    return max(folders, key=lambda f: len(f["name"]))


def choose_keep_file(locations: list[dict], priority_authors: list[str], aliases: dict[str, str], filename: str = "") -> int:
    """
    Given duplicate file locations, return index of the one to keep.
    Prefer the copy in a folder whose base name IS a priority author directly.
    If multiple match, use priority list order (earlier in list = higher priority).
    Then fall back to alias resolution. Then check if filename matches folder series.
    Then longest folder name.
    """
    # First pass: direct priority author match — pick the HIGHEST priority (lowest index in list)
    best_priority_idx = None
    best_priority_rank = len(priority_authors) + 1
    for i, loc in enumerate(locations):
        folder = loc["folder"]
        base = folder.split(" - ")[0].strip()
        if base.lower() in priority_authors:
            rank = priority_authors.index(base.lower())
            if rank < best_priority_rank:
                best_priority_rank = rank
                best_priority_idx = i
    if best_priority_idx is not None:
        return best_priority_idx

    # Second pass: alias resolves to priority author (same ranking logic)
    best_alias_idx = None
    best_alias_rank = len(priority_authors) + 1
    for i, loc in enumerate(locations):
        folder = loc["folder"]
        base = folder.split(" - ")[0].strip()
        resolved = aliases.get(base.lower(), base)
        if resolved.lower() in priority_authors:
            rank = priority_authors.index(resolved.lower())
            if rank < best_alias_rank:
                best_alias_rank = rank
                best_alias_idx = i
    if best_alias_idx is not None:
        return best_alias_idx

    # Third pass: check if the filename mentions the folder's series suffix
    fn_lower = filename.lower()
    for i, loc in enumerate(locations):
        folder = loc["folder"]
        parts = folder.split(" - ")
        if len(parts) > 1:
            series = parts[1].strip().lower()
            if series and series in fn_lower:
                return i

    # Fallback: keep in the longest folder name (most descriptive)
    longest_idx = 0
    longest_len = 0
    for i, loc in enumerate(locations):
        if len(loc["folder"]) > longest_len:
            longest_len = len(loc["folder"])
            longest_idx = i
    return longest_idx


def trash_file(service, file_id: str, dry_run: bool) -> bool:
    """Move a file to trash."""
    if dry_run:
        return True
    try:
        service.files().update(fileId=file_id, body={"trashed": True}).execute()
        time.sleep(0.1)
        return True
    except Exception as e:
        print(f"    [ERROR] Failed to trash {file_id}: {e}")
        return False


def move_file(service, file_id: str, new_parent_id: str, old_parent_id: str, dry_run: bool) -> bool:
    """Move a file from one folder to another."""
    if dry_run:
        return True
    try:
        service.files().update(
            fileId=file_id,
            addParents=new_parent_id,
            removeParents=old_parent_id,
        ).execute()
        time.sleep(0.1)
        return True
    except Exception as e:
        print(f"    [ERROR] Failed to move {file_id}: {e}")
        return False


def main():
    dry_run = "--execute" not in sys.argv

    print("=" * 60)
    print("  GOOGLE DRIVE DUPLICATE CLEANUP")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Mode: {'DRY RUN' if dry_run else 'EXECUTE'}")
    print("=" * 60)

    if not dry_run:
        print("\n  WARNING: This will trash duplicate files on Google Drive!")
        print("  Files go to Trash (recoverable for 30 days).")
        print()

    service = build_drive_service()
    if not service:
        print("ERROR: Could not connect to Drive.")
        return

    priority_authors = load_priority_authors()
    aliases = load_aliases()

    # Fetch all folders
    print("\nFetching folders...")
    folders = fetch_all_folders(service)
    folder_by_id = {f["id"]: f["name"] for f in folders}
    print(f"  Found {len(folders)} folders")

    # === 1. Handle duplicate folders ===
    print("\n--- DUPLICATE FOLDERS ---")
    base_groups = defaultdict(list)
    for f in folders:
        base = f["name"].split(" - ")[0].strip()
        norm = normalize(base)
        base_groups[norm].append(f)

    dupe_folder_groups = {k: v for k, v in base_groups.items() if len(v) > 1}
    folders_merged = 0
    files_moved = 0

    for norm, group in sorted(dupe_folder_groups.items()):
        # Skip the Japanese-character group (these are different authors)
        names = [f["name"] for f in group]
        # If all normalized names are different, these are false positives (different authors)
        unique_norms = set(normalize(n.split(" - ")[0].strip()) for n in names)
        if len(unique_norms) > 1:
            print(f"\n  [SKIP] Different authors: {names}")
            continue

        keep = choose_keep_folder(group, priority_authors, aliases)
        remove = [f for f in group if f["id"] != keep["id"]]

        print(f"\n  Group: {names}")
        print(f"    Keep:   '{keep['name']}' ({keep['id']})")

        for src in remove:
            print(f"    Merge:  '{src['name']}' -> '{keep['name']}'")
            # Move all files from src to keep
            src_files = fetch_files_in_folder(service, src["id"])
            keep_files = fetch_files_in_folder(service, keep["id"])
            keep_names = {f["name"] for f in keep_files}

            for f in src_files:
                if f["name"] in keep_names:
                    # Already exists in keep folder — trash the duplicate
                    print(f"      [TRASH] {f['name']} (already in target)")
                    trash_file(service, f["id"], dry_run)
                else:
                    # Move to keep folder
                    print(f"      [MOVE]  {f['name']}")
                    move_file(service, f["id"], keep["id"], src["id"], dry_run)
                    files_moved += 1

            # Trash empty source folder
            remaining = fetch_files_in_folder(service, src["id"]) if not dry_run else []
            if dry_run or len(remaining) == 0:
                print(f"      [TRASH FOLDER] '{src['name']}'")
                trash_file(service, src["id"], dry_run)
                folders_merged += 1

    # === 2. Handle duplicate files ===
    print("\n\n--- DUPLICATE FILES ---")
    # Re-scan to get current state (after folder merges)
    print("  Re-scanning for duplicate files...")
    all_files: dict[str, list[dict]] = {}
    for i, folder in enumerate(folders):
        if folder_by_id.get(folder["id"]) is None:
            continue  # folder was trashed
        files = fetch_files_in_folder(service, folder["id"])
        for f in files:
            name = f.get("name", "")
            if name not in all_files:
                all_files[name] = []
            all_files[name].append({
                "folder": folder["name"],
                "folder_id": folder["id"],
                "file_id": f["id"],
                "size": int(f.get("size", 0)),
            })
        if (i + 1) % 50 == 0:
            print(f"    Scanned {i+1}/{len(folders)} folders...")
        time.sleep(0.02)

    dupe_files = {k: v for k, v in all_files.items() if len(v) > 1}
    files_trashed = 0

    for filename, locations in sorted(dupe_files.items()):
        keep_idx = choose_keep_file(locations, priority_authors, aliases, filename=filename)
        size_mb = locations[0]["size"] / (1024 * 1024) if locations[0]["size"] else 0

        print(f"\n  {filename} ({size_mb:.0f} MB, {len(locations)} copies)")
        print(f"    [KEEP]  in '{locations[keep_idx]['folder']}'")

        for i, loc in enumerate(locations):
            if i == keep_idx:
                continue
            print(f"    [TRASH] in '{loc['folder']}'")
            if trash_file(service, loc["file_id"], dry_run):
                files_trashed += 1

    # Summary
    print("\n" + "=" * 60)
    print(f"  SUMMARY {'(DRY RUN)' if dry_run else ''}")
    print(f"  Folders merged: {folders_merged}")
    print(f"  Files moved: {files_moved}")
    print(f"  Duplicate files trashed: {files_trashed}")
    if dry_run:
        print("\n  Run with --execute to apply these changes.")
    print("=" * 60)


if __name__ == "__main__":
    main()
