"""
Set up a Windows Task Scheduler task to run the audiobook sync pipeline daily.

This script creates a scheduled task that runs sync_to_drive.py once per day.
Since the pipeline uses fuzzy matching with user confirmation prompts, the
scheduled task runs in --upload-only mode (skipping sort to avoid interactive
prompts for new authors).

For the full pipeline with sort + fuzzy match confirmation, run manually:
    python scripts/sync_to_drive.py

Usage:
    python scripts/setup_scheduler.py              # Install daily task (9:00 AM)
    python scripts/setup_scheduler.py --time 14:30 # Install daily task at 2:30 PM
    python scripts/setup_scheduler.py --remove     # Remove the scheduled task
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

TASK_NAME = "AudiobookSyncPipeline"
SCRIPTS_DIR = Path(__file__).resolve().parent
SYNC_SCRIPT = SCRIPTS_DIR / "sync_to_drive.py"


def find_python() -> str:
    """Get the path to the current Python interpreter."""
    return sys.executable


def create_task(run_time: str = "09:00") -> None:
    """Create a Windows Task Scheduler task for daily audiobook sync."""
    python_path = find_python()

    # The command to run. Uses --upload-only to avoid interactive prompts
    # during unattended execution. Sort can be run manually or we just
    # upload whatever is already in the library.
    command = f'"{python_path}" "{SYNC_SCRIPT}" --upload-only'

    # Build schtasks command
    # /SC DAILY = run once per day
    # /ST = start time
    # /RL HIGHEST = run with highest privileges (needed for file access)
    schtasks_cmd = [
        "schtasks",
        "/Create",
        "/TN", TASK_NAME,
        "/TR", command,
        "/SC", "DAILY",
        "/ST", run_time,
        "/RL", "HIGHEST",
        "/F",  # Force overwrite if exists
    ]

    print(f"Creating scheduled task: {TASK_NAME}")
    print(f"  Schedule: Daily at {run_time}")
    print(f"  Command: {command}")
    print()

    try:
        result = subprocess.run(
            schtasks_cmd,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print(f"  [OK] Task '{TASK_NAME}' created successfully.")
            print()
            print("  Notes:")
            print("  - The task runs --upload-only (no interactive sort prompts)")
            print("  - To include sorting, run the full pipeline manually:")
            print("    python scripts/sync_to_drive.py")
            print()
            print("  - To view the task: schtasks /Query /TN AudiobookSyncPipeline")
            print("  - To run it now:    schtasks /Run /TN AudiobookSyncPipeline")
            print("  - To remove it:     python scripts/setup_scheduler.py --remove")
        else:
            print(f"  [ERROR] Failed to create task:")
            print(f"  {result.stderr.strip()}")
            if "Access is denied" in result.stderr:
                print()
                print("  Try running this script as Administrator.")
    except FileNotFoundError:
        print("  [ERROR] schtasks.exe not found. Are you on Windows?")


def remove_task() -> None:
    """Remove the scheduled task."""
    schtasks_cmd = [
        "schtasks",
        "/Delete",
        "/TN", TASK_NAME,
        "/F",  # Don't prompt for confirmation
    ]

    print(f"Removing scheduled task: {TASK_NAME}")
    try:
        result = subprocess.run(
            schtasks_cmd,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print(f"  [OK] Task '{TASK_NAME}' removed.")
        else:
            print(f"  [ERROR] {result.stderr.strip()}")
    except FileNotFoundError:
        print("  [ERROR] schtasks.exe not found. Are you on Windows?")


def query_task() -> None:
    """Show current status of the scheduled task."""
    schtasks_cmd = [
        "schtasks",
        "/Query",
        "/TN", TASK_NAME,
        "/V",
        "/FO", "LIST",
    ]

    try:
        result = subprocess.run(
            schtasks_cmd,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print(result.stdout)
        else:
            print(f"  Task '{TASK_NAME}' not found. Not currently scheduled.")
    except FileNotFoundError:
        print("  [ERROR] schtasks.exe not found.")


def main():
    parser = argparse.ArgumentParser(
        description="Manage Windows Task Scheduler task for audiobook sync"
    )
    parser.add_argument(
        "--time",
        default="09:00",
        help="Time to run daily (HH:MM format, default: 09:00)",
    )
    parser.add_argument(
        "--remove",
        action="store_true",
        help="Remove the scheduled task",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show current task status",
    )
    args = parser.parse_args()

    if args.remove:
        remove_task()
    elif args.status:
        query_task()
    else:
        create_task(run_time=args.time)


if __name__ == "__main__":
    main()
