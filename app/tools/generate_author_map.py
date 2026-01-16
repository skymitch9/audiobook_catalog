# app/tools/generate_author_map.py
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Set

from app.config import EXTS, OUTPUT_DIR, ROOT_DIR
from app.metadata import extract_metadata, walk_library

DEFAULT_OUT = Path("author_drive_map.json").resolve()


def _split_authors(s: str) -> list[str]:
    if not s:
        return []
    # authors already normalized like "A, B, C"
    return [a.strip() for a in s.split(",") if a.strip()]


def _resolve_output_path() -> Path:
    # Output to root directory (parent of audiobook_catalog)
    p = Path(__file__).parent.parent.parent.parent / "author_drive_map.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def main() -> None:
    print("[author-map] ===== Generate Author Map =====")
    if not ROOT_DIR or not ROOT_DIR.exists():
        print(f"[author-map] ERROR: ROOT_DIR not set or does not exist: {ROOT_DIR!s}")
        print("[author-map] Tip: set ROOT_DIR in .env to your audiobooks folder (the one that contains author/book files).")
        return

    print(f"[author-map] Library root: {ROOT_DIR}")
    files = walk_library(ROOT_DIR, EXTS)
    print(f"[author-map] Found {len(files)} eligible audio files ({', '.join(sorted(EXTS))})")

    if not files:
        print("[author-map] No files found. Check ROOT_DIR and that the folder has .m4b/.m4a/.mp4 files.")
        return

    authors: Set[str] = set()
    for i, p in enumerate(files, 1):
        if i % 200 == 0:
            print(f"[author-map] Scanning… {i}/{len(files)}")
        try:
            row = extract_metadata(p)
        except Exception as e:
            print(f"[author-map][WARN] metadata failed for {p}: {e}")
            continue
        for a in _split_authors(row.get("author", "") or ""):
            authors.add(a)

    print(f"[author-map] Unique authors discovered: {len(authors)}")
    out_path = _resolve_output_path()

    payload: dict[str, str] = {}
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                payload.update(existing)
            else:
                print(f"[author-map][WARN] Existing map not a dict, replacing: {out_path}")
        except Exception as e:
            print(f"[author-map][WARN] Failed to read existing map ({out_path}): {e}")

    for a in sorted(authors):
        payload.setdefault(a, "")  # keep existing IDs, add missing authors as empty

    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[author-map] Wrote {len(payload)} entries → {out_path}")
    print("[author-map] Fill each value with the Drive folder ID (just the ID).")
    print("[author-map] Example: https://drive.google.com/drive/folders/<FOLDER_ID> → paste <FOLDER_ID>")


if __name__ == "__main__":
    main()
