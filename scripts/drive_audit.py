#!/usr/bin/env python3
"""
Google Drive Audit — Weekly duplicate detection report.

Scans all audiobook folders on Drive for:
1. Duplicate folders (same base name before " - ")
2. Duplicate files (same filename in different folders)
3. Suspected duplicates (similar names via normalized comparison)

Outputs a markdown report to docs/DRIVE_AUDIT_REPORT.md

Usage:
    python scripts/drive_audit.py
"""

from __future__ import annotations

import json
import os
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


def normalize(s: str) -> str:
    """Strip to bare alphanumeric lowercase for comparison."""
    return re.sub(r'[^a-z0-9]', '', s.lower())


def fetch_all_folders(service) -> list[dict]:
    """Fetch all author folders with metadata."""
    folders = []
    page_token = None
    while True:
        results = service.files().list(
            q=f"'{DRIVE_PARENT_FOLDER_ID}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
            pageSize=1000,
            fields="nextPageToken, files(id, name, owners)",
            pageToken=page_token,
        ).execute()
        folders.extend(results.get("files", []))
        page_token = results.get("nextPageToken")
        if not page_token:
            break
    return folders


def fetch_files_in_folder(service, folder_id: str) -> list[dict]:
    """Fetch all files in a folder."""
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


def main():
    print("=" * 60)
    print("  GOOGLE DRIVE AUDIT")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    service = build_drive_service()
    if not service:
        print("ERROR: Could not connect to Drive.")
        return

    # Fetch all folders
    print("\nFetching folders...")
    folders = fetch_all_folders(service)
    print(f"  Found {len(folders)} folders")

    # === 1. Duplicate folder detection ===
    print("\nChecking for duplicate folders...")
    # Group by normalized base name (before " - ")
    base_groups = defaultdict(list)
    for f in folders:
        base = f["name"].split(" - ")[0].strip()
        norm = normalize(base)
        base_groups[norm].append(f)

    dupe_folders = {k: v for k, v in base_groups.items() if len(v) > 1}

    # === 2. Duplicate files across folders ===
    print("Scanning for duplicate files across folders...")
    all_files = {}  # filename -> list of (folder_name, file_id, size)
    for i, folder in enumerate(folders):
        files = fetch_files_in_folder(service, folder["id"])
        for f in files:
            name = f.get("name", "")
            if name not in all_files:
                all_files[name] = []
            all_files[name].append({
                "folder": folder["name"],
                "file_id": f["id"],
                "size": int(f.get("size", 0)),
            })
        if (i + 1) % 50 == 0:
            print(f"  Scanned {i+1}/{len(folders)} folders...")
        time.sleep(0.02)

    dupe_files = {k: v for k, v in all_files.items() if len(v) > 1}

    # === 3. Generate report ===
    print("\nGenerating report...")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# Google Drive Audit Report",
        "",
        f"Generated: {now}",
        f"Total folders: {len(folders)}",
        f"Total unique files: {len(all_files)}",
        "",
        "## Summary",
        "",
        f"| Issue | Count |",
        f"|-------|-------|",
        f"| Duplicate folder groups | {len(dupe_folders)} |",
        f"| Files in multiple folders | {len(dupe_files)} |",
        "",
    ]

    if dupe_folders:
        lines.append("## Duplicate Folders")
        lines.append("")
        lines.append("These folders have the same base author name (before ' - '):")
        lines.append("")
        for norm, group in sorted(dupe_folders.items()):
            lines.append(f"### {group[0]['name'].split(' - ')[0]}")
            lines.append("")
            for f in group:
                lines.append(f"- `{f['name']}` (ID: {f['id']})")
            lines.append("")

    if dupe_files:
        lines.append("## Duplicate Files")
        lines.append("")
        lines.append("These files exist in multiple folders:")
        lines.append("")
        # Sort by count descending
        sorted_dupes = sorted(dupe_files.items(), key=lambda x: -len(x[1]))
        for filename, locations in sorted_dupes[:50]:
            size_mb = locations[0]["size"] / (1024 * 1024) if locations[0]["size"] else 0
            lines.append(f"### {filename} ({size_mb:.0f} MB, {len(locations)} copies)")
            lines.append("")
            for loc in locations:
                lines.append(f"- `{loc['folder']}`")
            lines.append("")
        if len(sorted_dupes) > 50:
            lines.append(f"*... and {len(sorted_dupes) - 50} more*")
            lines.append("")

    if not dupe_folders and not dupe_files:
        lines.append("## All Clear")
        lines.append("")
        lines.append("No duplicates found on Google Drive.")
        lines.append("")

    # Write report
    report_path = PROJECT_ROOT / "docs" / "DRIVE_AUDIT_REPORT.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"\n  Report: {report_path}")
    print(f"  Duplicate folders: {len(dupe_folders)}")
    print(f"  Duplicate files: {len(dupe_files)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
