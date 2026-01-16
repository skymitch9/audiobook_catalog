# app/tools/book_sort.py
# Move audiobook files into subfolders named after the primary author.
# Uses ROOT_DIR and EXTS from app.config (set via .env).

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from mutagen.mp4 import MP4

# Reuse your configured root + extensions
from app.config import EXTS, ROOT_DIR

# iTunes atom for author
K_ARTIST = "\xa9ART"


def _bytes_to_str(b: bytes) -> str:
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            return b.decode(enc).strip()
        except Exception:
            pass
    return b.decode("utf-8", errors="ignore").strip()


def _first_str(val) -> Optional[str]:
    v = val[0] if isinstance(val, list) and val else val
    if v is None:
        return None
    if isinstance(v, bytes):
        return _bytes_to_str(v)
    return str(v).strip()


def get_author_name(file_path: Path) -> Optional[str]:
    """
    Returns a normalized primary author string from MP4/M4B tags.
    - Reads ©ART / \xa9ART (iTunes 'Artist' a.k.a. Author in many audiobook rips)
    - Uses the first author before a comma, normalized to Title Case.
    """
    try:
        audio = MP4(str(file_path))
        tags = audio.tags or {}
        author_field = tags.get(K_ARTIST)
        if not author_field:
            return None
        raw = _first_str(author_field)
        if not raw:
            return None
        # Take only the first author before comma
        first_author = raw.split(",")[0].strip()
        # Normalize casing (avoid shouting acronyms up to 5 chars)
        parts = first_author.split()
        if not parts:
            return None
        normalized_author = " ".join(p if (p.isupper() and len(p) <= 5) else p.capitalize() for p in parts)
        return normalized_author
    except Exception as e:
        print(f"[WARN] Metadata read failed: {file_path} - {e}")
        return None


def organize_by_author(root_dir: Path, exts: set[str], recursive: bool = True, dry_run: bool = False) -> None:
    """
    Moves files under root_dir into subfolders named after the detected author.
    - Only files with extensions in `exts` are processed.
    - If a file already resides in an 'Author' folder that matches the author, it is skipped.
    - Set dry_run=True to preview without moving.
    """
    if recursive:
        files = [p for p in root_dir.rglob("*") if p.is_file() and p.suffix.lower() in exts]
    else:
        files = [p for p in root_dir.iterdir() if p.is_file() and p.suffix.lower() in exts]

    if not files:
        print(f"No audiobook files found in: {root_dir}")
        return

    for f in files:
        author = get_author_name(f)
        if not author:
            print(f"Skipping (no author): {f.relative_to(root_dir)}")
            continue

        # Target folder directly under ROOT_DIR
        author_folder = root_dir / author

        # If already in the correct author folder, skip
        try:
            parent_rel = f.parent.relative_to(root_dir)
            if parent_rel.parts and parent_rel.parts[0].lower() == author.lower():
                # already inside the 'Author/' folder
                continue
        except Exception:
            # If file is not under root_dir (shouldn't happen), we still try to move
            pass

        author_folder.mkdir(parents=True, exist_ok=True)
        dest = author_folder / f.name

        if dest.exists():
            print(f"Exists → skip: {dest.relative_to(root_dir)}")
            continue

        print(f"Move: {f.relative_to(root_dir)}  →  {dest.relative_to(root_dir)}")
        if not dry_run:
            try:
                shutil.move(str(f), str(dest))
            except Exception as e:
                print(f"[ERROR] Move failed: {f} → {dest} ({e})")


def main():
    # Safety: ensure ROOT_DIR exists
    if not ROOT_DIR.exists():
        print(f"[ERROR] ROOT_DIR not found: {ROOT_DIR}")
        return

    # Default: recursive, real move. Flip to dry_run=True to preview.
    organize_by_author(ROOT_DIR, EXTS, recursive=True, dry_run=False)


if __name__ == "__main__":
    main()
