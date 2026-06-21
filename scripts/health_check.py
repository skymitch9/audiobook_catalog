#!/usr/bin/env python3
"""
Health Check / Status Summary for the Audiobook Catalog pipeline.

Reports:
  - Last sync time (from upload manifest)
  - Catalog size (books in CSV / local library)
  - Google Drive connectivity
  - Discord webhook reachability
  - Site deployment status

Usage:
    python scripts/health_check.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
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

# Paths
MANIFEST_PATH = SCRIPTS_DIR / "upload_manifest.json"
CATALOG_CSV = PROJECT_ROOT / "site" / "catalog.csv"
LIBRARY_ROOT = Path(os.getenv("ROOT_DIR", r"C:\Users\nbasl\OpenAudible\books"))
AUDIOBOOK_EXTS = {".m4b", ".m4a", ".mp4"}


def _header(title: str) -> None:
    print(f"\n{'─' * 50}")
    print(f"  {title}")
    print(f"{'─' * 50}")


def check_last_sync() -> None:
    """Report last sync time from the upload manifest."""
    _header("Last Sync")

    if not MANIFEST_PATH.exists():
        print("  [?] No upload manifest found (never synced?)")
        return

    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    if not manifest:
        print("  [?] Manifest is empty")
        return

    # Find most recent upload timestamp
    latest_time = None
    latest_file = None
    for path, info in manifest.items():
        ts = info.get("uploaded_at", "")
        if ts and (latest_time is None or ts > latest_time):
            latest_time = ts
            latest_file = path

    if latest_time:
        print(f"  Last upload: {latest_time}")
        print(f"  File: {latest_file}")
        print(f"  Total uploads tracked: {len(manifest)}")
    else:
        print("  [?] No timestamps in manifest")


def check_catalog_size() -> None:
    """Report catalog size from CSV and local library."""
    _header("Catalog Size")

    # CSV count
    if CATALOG_CSV.exists():
        with open(CATALOG_CSV, "r", encoding="utf-8") as f:
            lines = f.readlines()
        # Subtract header row
        csv_count = max(0, len(lines) - 1)
        csv_size = CATALOG_CSV.stat().st_size / 1024
        print(f"  Catalog CSV: {csv_count} books ({csv_size:.1f} KB)")
    else:
        print("  [!] catalog.csv not found in site/")

    # Local library count
    if LIBRARY_ROOT.exists():
        local_files = [
            p for p in LIBRARY_ROOT.rglob("*")
            if p.is_file() and p.suffix.lower() in AUDIOBOOK_EXTS
        ]
        # Count unique authors (top-level folders)
        authors = set()
        for p in local_files:
            try:
                rel = p.relative_to(LIBRARY_ROOT)
                if len(rel.parts) > 1:
                    authors.add(rel.parts[0])
            except ValueError:
                pass
        print(f"  Local library: {len(local_files)} files, {len(authors)} authors")
        total_size_gb = sum(p.stat().st_size for p in local_files) / (1024**3)
        print(f"  Total size: {total_size_gb:.1f} GB")
    else:
        print(f"  [!] Library root not found: {LIBRARY_ROOT}")


def check_drive_connectivity() -> None:
    """Test Google Drive API connectivity."""
    _header("Google Drive")

    try:
        from drive_auth import build_drive_service
        service = build_drive_service()
        if not service:
            print("  [FAIL] Could not authenticate (missing credentials?)")
            return

        # Quick API call to verify connectivity
        start = time.time()
        result = service.files().list(
            pageSize=1,
            fields="files(id)",
            q="trashed=false",
        ).execute()
        elapsed = time.time() - start

        print(f"  [OK] Connected ({elapsed:.2f}s response)")
        print(f"  Auth: Valid credentials")
    except ImportError:
        print("  [SKIP] Google API libraries not installed")
    except Exception as e:
        print(f"  [FAIL] {e}")


def check_discord_webhook() -> None:
    """Verify Discord webhook URL is reachable (GET returns method not allowed = working)."""
    _header("Discord Webhook")

    webhook_url = os.environ.get("DISCORD_WEBHOOK")
    if not webhook_url:
        print("  [SKIP] DISCORD_WEBHOOK not set in environment")
        print("  (Set in .env or pass as env var to test)")
        return

    try:
        import requests

        # HEAD/GET on a Discord webhook returns 405 Method Not Allowed — that confirms it's valid
        start = time.time()
        response = requests.get(webhook_url, timeout=10)
        elapsed = time.time() - start

        if response.status_code in (200, 401, 405):
            print(f"  [OK] Webhook reachable ({elapsed:.2f}s, HTTP {response.status_code})")
        elif response.status_code == 404:
            print(f"  [FAIL] Webhook not found (deleted?) — HTTP 404")
        else:
            print(f"  [WARN] Unexpected status: HTTP {response.status_code}")
    except ImportError:
        print("  [SKIP] requests library not installed")
    except Exception as e:
        print(f"  [FAIL] {e}")


def check_site_deployment() -> None:
    """Check if the GitHub Pages site is accessible."""
    _header("Site Deployment")

    site_url = "https://skymitch9.github.io/audiobook_catalog/"

    try:
        import requests

        start = time.time()
        response = requests.get(site_url, timeout=15)
        elapsed = time.time() - start

        if response.status_code == 200:
            size_kb = len(response.content) / 1024
            print(f"  [OK] Site is live ({elapsed:.2f}s, {size_kb:.0f} KB)")
            print(f"  URL: {site_url}")
        else:
            print(f"  [WARN] HTTP {response.status_code}")
    except ImportError:
        print("  [SKIP] requests library not installed")
    except Exception as e:
        print(f"  [FAIL] {e}")


def check_git_status() -> None:
    """Report git branch and pending changes."""
    _header("Git Status")

    import subprocess

    try:
        # Current branch
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        if branch.returncode == 0:
            print(f"  Branch: {branch.stdout.strip()}")

        # Pending changes
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        if status.returncode == 0:
            changes = [l for l in status.stdout.strip().split("\n") if l.strip()]
            if changes:
                print(f"  Pending changes: {len(changes)} file(s)")
            else:
                print("  Working tree: clean")

        # Last commit
        log = subprocess.run(
            ["git", "log", "-1", "--format=%h %s (%cr)"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        if log.returncode == 0 and log.stdout.strip():
            print(f"  Last commit: {log.stdout.strip()}")

    except Exception as e:
        print(f"  [ERROR] {e}")


def main() -> None:
    print("=" * 50)
    print("  Audiobook Catalog — Health Check")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    check_last_sync()
    check_catalog_size()
    check_drive_connectivity()
    check_discord_webhook()
    check_site_deployment()
    check_git_status()

    print(f"\n{'=' * 50}")
    print("  Health check complete.")
    print(f"{'=' * 50}\n")


if __name__ == "__main__":
    main()
