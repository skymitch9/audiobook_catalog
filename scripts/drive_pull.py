"""Pull books that exist on Drive but not locally — DRY-RUN by default.

The pipeline pushes local → Drive; nothing brings files the other way, so a book
someone drops into Drive never ingests or reaches the sites. This is that missing
step, built on the ``app.core.drive_pull`` matcher (all-format, copy-safe,
series-safe — see that module's header for the incident it prevents).

    python scripts/drive_pull.py            # dry-run: report only, downloads nothing
    python scripts/drive_pull.py --enforce  # actually pull the genuinely-new files

Drive I/O (auth, listing, download) is reused from ``scripts/audit_drive_vs_local``
so there is one Drive client, not two.
"""

from __future__ import annotations

import sys as _sys
try:
    _sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

# repo root (for ``app.*``) and this dir (for the sibling audit module).
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE))

import audit_drive_vs_local as A  # noqa: E402  (sibling Drive client)
from app.core.drive_pull import ALL_EXTS, plan_pull  # noqa: E402


def _local_names_all_formats(root: Path) -> list[str]:
    """Every book file on disk, ALL formats, recursive — the fix for the
    audio-only scan that flagged 170 present ebooks as missing."""
    if not root.exists():
        print(f"  [ERROR] Library root not found: {root}")
        return []
    return [p.name for p in root.rglob("*") if p.is_file() and p.suffix.lower() in ALL_EXTS]


def _local_folder_for(drive_folder: str) -> str:
    """Mirror the audit script's author-folder derivation so a pulled file lands
    where sort/ingest expect it."""
    return drive_folder.split("/")[0].split(" - ")[0].strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="Pull Drive-only books to local (dry-run by default)")
    ap.add_argument("--enforce", action="store_true", help="actually download (default: report only)")
    ap.add_argument("--limit", type=int, default=0, help="cap how many to pull under --enforce (0 = all)")
    ap.add_argument("--json-summary", action="store_true",
                    help="print one machine-readable line, 'PULL_JSON {...}', for the pipeline to parse")
    args = ap.parse_args()

    def _emit_summary(enforced: bool, pulled: int) -> None:
        """One machine-readable line the 8h pipeline parses for its step
        detail. Gated behind --json-summary so manual runs stay clean — same
        convention as drive_role_parity.py's PARITY_JSON."""
        if args.json_summary:
            print("PULL_JSON " + json.dumps({
                "enforced": enforced,
                "pulled": pulled,
                "toPull": len(plan.to_pull),
                "skippedCopies": len(plan.skipped_copies),
                "present": len(plan.skipped_present),
                "ignored": len(plan.ignored),
            }))

    service = A.build_drive_service()
    if not service:
        print("[ERROR] Drive auth failed")
        return 1

    drive = A.get_all_drive_files(service)
    drive_flat = [(folder, fi) for folder, files in drive.items() for fi in files]
    by_name = {fi["name"]: (folder, fi) for folder, fi in drive_flat}
    drive_names = [fi["name"] for _, fi in drive_flat]
    local_names = _local_names_all_formats(A.LIBRARY_ROOT)

    plan = plan_pull(drive_names, local_names)

    print("=" * 60)
    print("  Drive -> Local pull  " + ("[ENFORCE]" if args.enforce else "[DRY-RUN]"))
    print("=" * 60)
    print(f"  Drive files: {len(drive_names)}   Local (all formats): {len(local_names)}")
    print(f"  TO PULL (genuinely new): {len(plan.to_pull)}")
    print(f"  skipped — 'Copy of'/(N): {len(plan.skipped_copies)}")
    print(f"  skipped — already local: {len(plan.skipped_present)}")
    print(f"  ignored — not a book:    {len(plan.ignored)}")
    if plan.skipped_copies:
        print("\n  Copies that will NEVER be pulled (first 8):")
        for n in plan.skipped_copies[:8]:
            print(f"    x {n}")
    if plan.to_pull:
        print("\n  Would pull (first 60):")
        for n in plan.to_pull[:60]:
            folder = by_name[n][0]
            size = by_name[n][1].get("size", 0) / 1e6
            print(f"    + [{folder}] {n}  ({size:.0f} MB)")

    if not args.enforce:
        print("\n  DRY-RUN — nothing downloaded. Re-run with --enforce to pull.")
        _emit_summary(enforced=False, pulled=0)
        return 0

    # --- enforce: download to a staging temp, then atomic move into the library ---
    pulled = 0
    targets = plan.to_pull if args.limit <= 0 else plan.to_pull[: args.limit]
    print(f"\n  Pulling {len(targets)} file(s)...")
    for name in targets:
        folder, fi = by_name[name]
        dest_dir = A.LIBRARY_ROOT / _local_folder_for(folder)
        dest_dir.mkdir(parents=True, exist_ok=True)
        final_path = dest_dir / name
        if final_path.exists():  # belt-and-braces; the matcher already skipped these
            continue
        fd, tmp = tempfile.mkstemp(suffix=".part", dir=str(dest_dir))
        os.close(fd)
        tmp_path = Path(tmp)
        if A.download_file(service, fi["id"], tmp_path):
            os.replace(tmp_path, final_path)  # atomic: ingest never sees a partial
            pulled += 1
            print(f"    + {name}")
        else:
            tmp_path.unlink(missing_ok=True)
    print(f"\n  Pulled {pulled} file(s).")
    _emit_summary(enforced=True, pulled=pulled)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
