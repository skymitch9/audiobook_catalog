from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
OUTPUT_DIR: Path  = PROJECT_ROOT / "output_files"   
SITE_DIR: Path    = PROJECT_ROOT / "site"           

ROOT_DIR_ENV = os.getenv("ROOT_DIR")
DEFAULT_LIBRARY_DIR = PROJECT_ROOT / "library"
ROOT_DIR: Path = Path(ROOT_DIR_ENV if ROOT_DIR_ENV else DEFAULT_LIBRARY_DIR).expanduser().resolve()

DRIVE_FOLDER_URL: str | None = os.getenv("DRIVE_FOLDER_URL") or None

EXTS: set[str] = {".m4b", ".m4a", ".mp4"}

SITE_INDEX_NAME: str = "index.html"   
SITE_CSV_NAME: str   = "catalog.csv"  

